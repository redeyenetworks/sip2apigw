"""#5 ENFORCEMENT (enforce=true): suppression decision, DB transition, fail-safe,
and the validate_config guardrails. End-to-end (mock Fusion) is covered by the
M8 staging drill + the prod live-tester; these pin the code-level guarantees."""

import time

import pytest

from sipgw.config import AppConfig, DedupeConfig, validate_config
from sipgw.database import (CallDatabase, STATE_PENDING, STATE_DUPLICATE,
                            STATE_DELIVERED)
from sipgw.dedupe import Deduper
from sipgw.main import SIPGateway
from sipgw.parser import CallerInfo
from tests.mock_fusion import run_mock_fusion
from tests.test_dedupe import _mock_fusion

# The APPROVED prod config: enforce, 2s window, bed-level, purpose in the key.
ENF = DedupeConfig(enforce=True, window_seconds=2, match_bed=True, match_purpose=True)


def _caller(area="730", room="201", bed="1", display="Code Blue"):
    raw = f"a{area}r{room}" + (f"b{bed}" if bed else "")
    return CallerInfo(raw_user=raw, display_name=display, area_number=area,
                      room_number=room, bed_number=bed, parse_success=True)


async def _db(tmp_path):
    db = CallDatabase(str(tmp_path / "e.db"))
    await db.initialize()
    return db


async def _pending(db, area="730", room="201", bed="1", display="Code Blue"):
    raw = f"a{area}r{room}" + (f"b{bed}" if bed else "")
    return await db.create_pending_call(
        caller_id=raw, display_name=display, area_number=area, area_name="",
        room_number=room, tts_string="x", sip_call_id="c", is_test=0)


class TestSuppressionDecision:
    @pytest.mark.asyncio
    async def test_same_bed_purpose_within_window_suppresses(self, tmp_path):
        db = await _db(tmp_path)
        await _pending(db)                    # prior page (same bed+purpose)
        rid = await _pending(db)              # this page
        dec = await Deduper(ENF).evaluate(db, caller=_caller(), row_id=rid, is_test=0)
        assert dec.suppress is True and dec.duplicate_of is not None
        await db.close()

    @pytest.mark.asyncio
    async def test_gap_beyond_window_not_suppressed(self, tmp_path):
        # A re-page >2s later is treated as LEGITIMATE — never suppressed.
        db = await _db(tmp_path)
        await _pending(db)
        rid = await _pending(db)
        dec = await Deduper(ENF).evaluate(db, caller=_caller(), row_id=rid,
                                          is_test=0, now=time.time() + 3)
        assert dec.suppress is False
        await db.close()

    @pytest.mark.asyncio
    async def test_different_bed_not_suppressed(self, tmp_path):
        db = await _db(tmp_path)
        await _pending(db, bed="1")
        rid = await _pending(db, bed="2")
        dec = await Deduper(ENF).evaluate(db, caller=_caller(bed="2"),
                                          row_id=rid, is_test=0)
        assert dec.suppress is False          # two patients in a room never merge
        await db.close()

    @pytest.mark.asyncio
    async def test_rrt_vs_code_blue_same_bed_not_suppressed(self, tmp_path):
        db = await _db(tmp_path)
        await _pending(db, display="Code Blue")
        rid = await _pending(db, display="RRT")
        dec = await Deduper(ENF).evaluate(db, caller=_caller(display="RRT"),
                                          row_id=rid, is_test=0)
        assert dec.suppress is False          # purpose hard-guard
        await db.close()


class TestSuppressionDB:
    @pytest.mark.asyncio
    async def test_mark_suppressed_pending_to_duplicate_recorded(self, tmp_path):
        db = await _db(tmp_path)
        rid = await _pending(db)
        n = await db.mark_suppressed(rid, duplicate_of=1)
        row = await db.get_call(rid)
        # record-first preserved: the suppressed page is still a durable row.
        assert n == 1 and row["state"] == STATE_DUPLICATE and row["duplicate_of"] == 1
        await db.close()

    @pytest.mark.asyncio
    async def test_mark_suppressed_noop_when_already_in_flight(self, tmp_path):
        # Fail-safe: if the worker already grabbed it, suppression is a no-op and
        # the page is delivered rather than dropped.
        db = await _db(tmp_path)
        rid = await _pending(db)
        await db.mark_attempting(rid)
        n = await db.mark_suppressed(rid, duplicate_of=1)
        assert n == 0 and (await db.get_call(rid))["state"] != STATE_DUPLICATE
        await db.close()

    @pytest.mark.asyncio
    async def test_suppressed_row_not_deliverable(self, tmp_path):
        db = await _db(tmp_path)
        rid = await _pending(db)
        await db.mark_suppressed(rid, duplicate_of=1)
        deliverable = await db.get_deliverable(states=(STATE_PENDING,))
        assert all(r["id"] != rid for r in deliverable)
        await db.close()


class TestSuppressionEndToEnd:
    @pytest.mark.asyncio
    async def test_second_same_bed_page_within_window_not_delivered(self, tmp_path):
        """The load-bearing enforcement proof: two same bed+purpose pages within
        the 2s window -> the FIRST is delivered, the SECOND is suppressed (state
        'duplicate', never sent), and Fusion receives EXACTLY ONE notification.
        The suppressed page is still a durable audit row pointing at the first.
        """
        with run_mock_fusion("200") as (base, state):
            config = AppConfig()
            config.fusion = _mock_fusion(base)
            config.database.path = str(tmp_path / "gw.db")
            config.dedupe = DedupeConfig(enforce=True, window_seconds=2,
                                         match_bed=True, match_purpose=True)
            gw = SIPGateway(config)
            await gw.db.initialize()
            await gw.webhook.initialize()
            try:
                fh = '"Code Blue" <sip:a730r201b1@127.0.0.1>;tag=t{}'
                await gw.on_call("call-1", "a730r201b1", "Code Blue", fh.format(1))
                await gw.on_call("call-2", "a730r201b1", "Code Blue", fh.format(2))
                for _ in range(5):
                    if await gw.worker.process_once() == 0:
                        break
                rows = {r["id"]: r for r in await gw.db.get_recent_calls(limit=10)}
                older, newer = min(rows), max(rows)
                assert rows[older]["state"] == STATE_DELIVERED      # 1st delivered
                assert rows[newer]["state"] == STATE_DUPLICATE      # 2nd SUPPRESSED
                assert rows[newer]["duplicate_of"] == older         # durable audit
                # Exactly ONE real notification reached Fusion.
                assert state.count("POST", "scenario-notifications") == 1
            finally:
                await gw.webhook.close()
                await gw.db.close()


class TestValidateConfigGuardrails:
    def _cfg(self, **dd):
        c = AppConfig()
        c.fusion.client_id = "c"; c.fusion.client_secret = "s"
        c.fusion.audience = "a"; c.fusion.scenario_id = "s"
        c.fusion.scenario_field_id = "f"
        for k, v in dd.items():
            setattr(c.dedupe, k, v)
        return c

    def test_enforce_is_not_fatal(self):
        # No ConfigError raised (was fatal before enforcement was signed off).
        validate_config(self._cfg(enforce=True, window_seconds=2), dry_run=False)

    def test_enforce_window_zero_warns_inert(self):
        w = validate_config(self._cfg(enforce=True, window_seconds=0), dry_run=False)
        assert any("INERT" in x for x in w)

    def test_enforce_active_warns(self):
        w = validate_config(self._cfg(enforce=True, window_seconds=2, match_bed=True),
                            dry_run=False)
        assert any("SUPPRESSION ACTIVE" in x for x in w)

    def test_enforce_wide_window_extra_warning(self):
        w = validate_config(self._cfg(enforce=True, window_seconds=20), dry_run=False)
        assert any("wide" in x for x in w)
