"""#3 escalation tests: no-send gate, mock delivery, robustness, worker wiring."""

import logging

import httpx
import pytest

from sipgw.config import DeliveryConfig, EscalationConfig, FusionConfig
from sipgw.delivery import DeliveryWorker
from sipgw.database import CallDatabase, STATE_FAILED
from sipgw.escalation import Escalator
from sipgw.webhook import FusionWebhook
from tests.mock_fusion import run_mock_fusion

_ROW = {
    "id": 42, "caller_id": "a730r201", "area_number": "730",
    "room_number": "201", "tts_string": "Attention! Code Blue! ...",
    "attempts": 6, "fusion_status": 500, "last_error": "status 500",
}


class TestEscalatorUnit:
    @pytest.mark.asyncio
    async def test_empty_url_no_post_but_logs(self, caplog):
        esc = Escalator(EscalationConfig(webhook_url=""), dry_run=False)
        with caplog.at_level(logging.ERROR, logger="sipgw.escalation"):
            await esc.escalate("failed", _ROW)
        assert esc._client is None                    # never built a client
        assert "no webhook configured" in caplog.text
        await esc.close()

    @pytest.mark.asyncio
    async def test_posts_to_mock_escalation(self):
        with run_mock_fusion("200") as (base, state):
            esc = Escalator(EscalationConfig(webhook_url=base + "/escalation"),
                            dry_run=False)
            await esc.initialize()
            try:
                await esc.escalate("expired", _ROW)
            finally:
                await esc.close()
        assert state.count("POST", "escalation") == 1

    @pytest.mark.asyncio
    async def test_no_send_blocks_real_host(self):
        esc = Escalator(
            EscalationConfig(webhook_url="https://hooks.example.com/escalation"),
            dry_run=True)
        await esc.initialize()
        try:
            await esc.escalate("failed", _ROW)     # must NOT reach the real host
            assert esc._transport is not None
            assert esc._transport.forwarded == []
            assert any("escalation" in httpx.URL(u).path
                       for _m, u in esc._transport.blocked)
        finally:
            await esc.close()

    @pytest.mark.asyncio
    async def test_swallows_post_errors(self):
        # Dead port -> connection error -> logged, never raised.
        esc = Escalator(EscalationConfig(webhook_url="http://127.0.0.1:1/escalation"),
                        dry_run=False)
        await esc.initialize()
        try:
            await esc.escalate("failed", _ROW)     # no exception propagates
        finally:
            await esc.close()


class TestWorkerEscalationWiring:
    @pytest.mark.asyncio
    async def test_exhaustion_escalates_to_mock(self, tmp_path):
        db = CallDatabase(str(tmp_path / "e.db"))
        await db.initialize()
        with run_mock_fusion("500") as (base, state):
            wh = FusionWebhook(FusionConfig(
                base_url=base + "/api", token_url=base + "/api/token",
                scenario_id="s", scenario_field_id="mock-field-id",
                client_id="c", client_secret="x", dry_run=False))
            await wh.initialize()
            esc = Escalator(EscalationConfig(webhook_url=base + "/escalation"),
                            dry_run=False)
            await esc.initialize()

            class _Clock:
                t = 1_000_000.0
                def __call__(self): return self.t

            clock = _Clock()
            worker = DeliveryWorker(
                db, wh, DeliveryConfig(max_attempts=1, base_backoff_seconds=1.0),
                on_escalate=esc.escalate, time_func=clock)

            cid = await db.create_pending_call(
                caller_id="a730r201", display_name="Code Blue", area_number="730",
                area_name="1st Floor... E.D...", room_number="201",
                tts_string="Attention! Code Blue! ...", is_test=1)
            await worker.process_once()            # attempt 1 -> 500 -> exhausted -> escalate

            row = await db.get_call(cid)
            assert row["state"] == STATE_FAILED
            assert state.count("POST", "escalation") == 1
            await esc.close(); await wh.close()
        await db.close()
