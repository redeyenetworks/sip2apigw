"""#2 delivery worker drills against the local mock Fusion server.

dry_run is OFF and URLs point at 127.0.0.1, so these are real HTTP round-trips
to the mock — no real notification. A controllable clock makes backoff/expiry
deterministic.
"""

import time

import pytest

from sipgw.config import DeliveryConfig, FusionConfig
from sipgw.database import (
    CallDatabase, STATE_DELIVERED, STATE_FAILED, STATE_EXPIRED, STATE_PENDING,
)
from sipgw.delivery import DeliveryWorker
from sipgw.webhook import FusionWebhook
from tests.mock_fusion import run_mock_fusion


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _webhook(base_url: str) -> FusionWebhook:
    cfg = FusionConfig(
        base_url=base_url + "/api",
        token_url=base_url + "/api/token",
        audience="prov", scenario_id="scen-1",
        scenario_endpoint="/v1/scenario-notifications",
        variable_name="customTTS",
        scenario_field_id="mock-field-id",   # preset -> skip the field-id GET
        client_id="cid", client_secret="secret",
        dry_run=False,                        # real loopback round-trip to mock
    )
    return FusionWebhook(cfg)


async def _db(tmp_path) -> CallDatabase:
    db = CallDatabase(str(tmp_path / "d.db"))
    await db.initialize()
    return db


async def _pending(db, tts="Attention! Code Blue! ... TEST bay.") -> int:
    return await db.create_pending_call(
        caller_id="a730r201", display_name="Code Blue", area_number="730",
        area_name="1st Floor... E.D...", room_number="201", tts_string=tts,
        sip_call_id="callid-1", is_test=1)


class TestDeliverHappyPath:
    @pytest.mark.asyncio
    async def test_pending_gets_delivered(self, tmp_path):
        db = await _db(tmp_path)
        with run_mock_fusion("200") as (base, state):
            wh = _webhook(base); await wh.initialize()
            worker = DeliveryWorker(db, wh, DeliveryConfig(), time_func=Clock())
            cid = await _pending(db)
            acted = await worker.process_once()
            await wh.close()
        assert acted == 1
        row = await db.get_call(cid)
        assert row["state"] == STATE_DELIVERED
        assert row["fusion_status"] == 200 and row["attempts"] == 1
        assert state.count("POST", "scenario-notifications") == 1
        await db.close()


class TestRetry:
    @pytest.mark.asyncio
    async def test_retry_then_deliver(self, tmp_path):
        db = await _db(tmp_path)
        clock = Clock()
        with run_mock_fusion("500") as (base, state):
            wh = _webhook(base); await wh.initialize()
            worker = DeliveryWorker(db, wh, DeliveryConfig(base_backoff_seconds=2.0),
                                    time_func=clock)
            cid = await _pending(db)

            await worker.process_once()                 # attempt 1 -> 500 -> reschedule
            row = await db.get_call(cid)
            assert row["state"] == STATE_PENDING and row["attempts"] == 1

            # Still cooling down -> no new attempt.
            before = state.count("POST", "scenario-notifications")
            await worker.process_once()
            assert state.count("POST", "scenario-notifications") == before

            # Recover: flip mock to 200, advance past backoff -> delivered.
            state.mode = "200"
            clock.advance(10.0)
            await worker.process_once()
            row = await db.get_call(cid)
            assert row["state"] == STATE_DELIVERED and row["attempts"] == 2
            await wh.close()
        await db.close()

    @pytest.mark.asyncio
    async def test_retry_after_header_sets_backoff(self, tmp_path):
        db = await _db(tmp_path)
        clock = Clock()
        with run_mock_fusion("429", retry_after=5) as (base, _state):
            wh = _webhook(base); await wh.initialize()
            worker = DeliveryWorker(db, wh, DeliveryConfig(base_backoff_seconds=2.0),
                                    time_func=clock)
            cid = await _pending(db)
            await worker.process_once()                 # 429 + Retry-After: 5
            # Cooldown must honor Retry-After (5s), not the 2s base backoff.
            assert worker._next_before[cid] == pytest.approx(clock.t + 5.0)
            await wh.close()
        await db.close()


class TestExhaustionAndExpiry:
    @pytest.mark.asyncio
    async def test_exhausts_to_failed_and_escalates(self, tmp_path):
        db = await _db(tmp_path)
        clock = Clock()
        escalations = []

        async def on_escalate(reason, row):
            escalations.append((reason, row["id"]))

        with run_mock_fusion("500") as (base, _state):
            wh = _webhook(base); await wh.initialize()
            worker = DeliveryWorker(
                db, wh, DeliveryConfig(max_attempts=2, base_backoff_seconds=1.0),
                on_escalate=on_escalate, time_func=clock)
            cid = await _pending(db)

            await worker.process_once()      # attempt 1 -> reschedule
            clock.advance(100.0)
            await worker.process_once()      # attempt 2 -> exhausted -> failed

            row = await db.get_call(cid)
            assert row["state"] == STATE_FAILED and row["attempts"] == 2
            assert ("failed", cid) in escalations
            await wh.close()
        await db.close()

    @pytest.mark.asyncio
    async def test_expired_and_escalates(self, tmp_path):
        db = await _db(tmp_path)
        escalations = []

        async def on_escalate(reason, row):
            escalations.append((reason, row["id"]))

        # Clock far in the future so the fresh row is already older than max_age.
        clock = Clock(t=time.time() + 10_000.0)
        with run_mock_fusion("200") as (base, state):
            wh = _webhook(base); await wh.initialize()
            worker = DeliveryWorker(db, wh, DeliveryConfig(max_age_seconds=900.0),
                                    on_escalate=on_escalate, time_func=clock)
            cid = await _pending(db)
            await worker.process_once()
            row = await db.get_call(cid)
            assert row["state"] == STATE_EXPIRED
            assert ("expired", cid) in escalations
            # Expired without ever attempting a send.
            assert state.count("POST", "scenario-notifications") == 0
            await wh.close()
        await db.close()


class TestRecoveryAndLoop:
    @pytest.mark.asyncio
    async def test_recover_inflight_then_deliver(self, tmp_path):
        db = await _db(tmp_path)
        with run_mock_fusion("200") as (base, _state):
            wh = _webhook(base); await wh.initialize()
            worker = DeliveryWorker(db, wh, DeliveryConfig(), time_func=Clock())
            cid = await _pending(db)
            await db.mark_attempting(cid)                # simulate crash mid-send
            assert (await db.get_call(cid))["state"] == "delivering"
            assert (await worker.recover()) == 1
            assert (await db.get_call(cid))["state"] == STATE_PENDING
            await worker.process_once()
            assert (await db.get_call(cid))["state"] == STATE_DELIVERED
            await wh.close()
        await db.close()

    @pytest.mark.asyncio
    async def test_background_loop_delivers(self, tmp_path):
        import asyncio
        db = await _db(tmp_path)
        with run_mock_fusion("200") as (base, _state):
            wh = _webhook(base); await wh.initialize()
            worker = DeliveryWorker(db, wh,
                                    DeliveryConfig(poll_interval_seconds=0.01),
                                    time_func=Clock())
            cid = await _pending(db)
            await worker.start()
            try:
                async def wait_delivered():
                    while True:
                        r = await db.get_call(cid)
                        if r["state"] == STATE_DELIVERED:
                            return
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(wait_delivered(), timeout=3.0)
            finally:
                await worker.stop()
                await wh.close()
        await db.close()
