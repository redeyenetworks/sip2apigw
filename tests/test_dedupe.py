"""#5 clinical dedupe — SHADOW/DISABLED tests.

Covers the CLINICAL fingerprint (stable; differs by room/bed/purpose), the
shadow ``Deduper`` (window 0 => no-suppress and no DB touch; enforce=False never
suppresses even with a window; 'WOULD suppress' logged only when the window is
open), the validate_config rule (enforce=True is fatal), and the load-bearing
safety property: TWO Code Blues for the same room are BOTH delivered.
"""

import logging

import pytest

from sipgw.config import AppConfig, DedupeConfig, FusionConfig, ConfigError, validate_config
from sipgw.parser import CallerInfo, parse_caller
from sipgw.database import CallDatabase, DuplicateMatch, STATE_DELIVERED
from sipgw.dedupe import Deduper, compute_fingerprint
from sipgw.webhook import FusionWebhook
from sipgw.main import SIPGateway
from tests.mock_fusion import run_mock_fusion


def _caller(area="730", room="201", bed=None, display="Code Blue") -> CallerInfo:
    raw = f"a{area}r{room}" + (f"b{bed}" if bed else "")
    return CallerInfo(
        raw_user=raw, display_name=display, area_number=area,
        room_number=room, bed_number=bed, parse_success=True)


async def _db(tmp_path) -> CallDatabase:
    db = CallDatabase(str(tmp_path / "dedupe.db"))
    await db.initialize()
    return db


async def _pending(db, area="730", room="201", bed=None,
                   display="Code Blue", is_test=0, event_id=None) -> int:
    raw = f"a{area}r{room}" + (f"b{bed}" if bed else "")
    return await db.create_pending_call(
        caller_id=raw, display_name=display, area_number=area, area_name="",
        room_number=room, tts_string="x", sip_call_id="c", is_test=is_test,
        event_id=event_id)


# --------------------------------------------------------------- fingerprint
class TestFingerprint:
    def test_stable_across_calls(self):
        a, b = _caller(), _caller()
        assert compute_fingerprint(a) == compute_fingerprint(b)
        # Stable across repeated invocations of the same object too.
        assert compute_fingerprint(a) == compute_fingerprint(a)

    def test_has_clinical_prefix_distinct_from_invite(self):
        fp = compute_fingerprint(_caller())
        # #5 clinical identity uses 'cf-v1:'; #15's transaction identity uses
        # 'v1:'. They must never collide.
        assert fp.startswith("cf-v1:")
        assert not fp.startswith("v1:")

    def test_differs_by_room(self):
        assert compute_fingerprint(_caller(room="201")) != \
               compute_fingerprint(_caller(room="202"))

    def test_differs_by_bed(self):
        assert compute_fingerprint(_caller(bed=None)) != \
               compute_fingerprint(_caller(bed="2"))
        assert compute_fingerprint(_caller(bed="1")) != \
               compute_fingerprint(_caller(bed="2"))

    def test_leading_zeros_are_significant(self):
        assert compute_fingerprint(_caller(room="007")) != \
               compute_fingerprint(_caller(room="7"))

    def test_differs_by_purpose_code_blue_vs_rrt(self):
        # Same room, different clinical purpose -> different identity.
        assert compute_fingerprint(_caller(display="Code Blue")) != \
               compute_fingerprint(_caller(display="RRT"))

    def test_matches_purpose_synonym_same_room(self):
        # 'Code Blue' and 'Blue' both resolve to the 'Code Blue' purpose, so
        # they share the same clinical fingerprint for the same room.
        assert compute_fingerprint(_caller(display="Code Blue")) == \
               compute_fingerprint(_caller(display="Blue"))

    def test_never_raises_on_junk(self):
        # A degenerate caller-like object must still yield a stable string.
        class Bogus:
            pass
        fp = compute_fingerprint(Bogus())
        assert fp.startswith("cf-v1:")


# ------------------------------------------------------------------ shadow
class TestDeduperShadow:
    @pytest.mark.asyncio
    async def test_window_zero_never_suppresses_and_no_lookup(self, tmp_path):
        db = await _db(tmp_path)
        r1 = await _pending(db)
        r2 = await _pending(db)
        dd = Deduper(DedupeConfig(enforce=False, window_seconds=0))
        dec = await dd.evaluate(db, caller=_caller(), row_id=r2, is_test=0)
        assert dec.duplicate_of is None
        assert dec.would_suppress is False
        assert dec.suppress is False
        await db.close()

    @pytest.mark.asyncio
    async def test_enforce_false_never_suppresses_even_with_window(self, tmp_path):
        db = await _db(tmp_path)
        r1 = await _pending(db)
        r2 = await _pending(db)
        dd = Deduper(DedupeConfig(enforce=False, window_seconds=300))
        dec = await dd.evaluate(db, caller=_caller(), row_id=r2, is_test=0)
        # The shadow lookup finds the prior row...
        assert dec.duplicate_of == r1
        assert dec.would_suppress is True
        # ...but the decision NEVER suppresses when enforce is False.
        assert dec.suppress is False
        await db.close()

    @pytest.mark.asyncio
    async def test_purpose_mismatch_is_not_a_duplicate(self, tmp_path):
        db = await _db(tmp_path)
        await _pending(db, display="Code Blue")
        r2 = await _pending(db, display="RRT")
        dd = Deduper(DedupeConfig(window_seconds=300))
        dec = await dd.evaluate(
            db, caller=_caller(display="RRT"), row_id=r2, is_test=0)
        assert dec.duplicate_of is None
        assert dec.would_suppress is False
        await db.close()

    @pytest.mark.asyncio
    async def test_bed_mismatch_is_not_a_duplicate(self, tmp_path):
        db = await _db(tmp_path)
        await _pending(db, bed=None)
        r2 = await _pending(db, bed="2")
        dd = Deduper(DedupeConfig(window_seconds=300))
        dec = await dd.evaluate(
            db, caller=_caller(bed="2"), row_id=r2, is_test=0)
        assert dec.duplicate_of is None
        await db.close()

    @pytest.mark.asyncio
    async def test_test_and_real_rows_do_not_cross_match(self, tmp_path):
        db = await _db(tmp_path)
        await _pending(db, is_test=1)          # a test page
        r2 = await _pending(db, is_test=0)     # a real page
        dd = Deduper(DedupeConfig(window_seconds=300))
        dec = await dd.evaluate(db, caller=_caller(), row_id=r2, is_test=0)
        assert dec.duplicate_of is None
        await db.close()

    @pytest.mark.asyncio
    async def test_would_suppress_logged_only_when_window_open(self, tmp_path, caplog):
        db = await _db(tmp_path)
        await _pending(db)
        r2 = await _pending(db)

        dd_closed = Deduper(DedupeConfig(window_seconds=0))
        with caplog.at_level(logging.WARNING, logger="sipgw.dedupe"):
            await dd_closed.evaluate(db, caller=_caller(), row_id=r2, is_test=0)
        assert not any("WOULD suppress" in r.getMessage() for r in caplog.records)

        caplog.clear()
        dd_open = Deduper(DedupeConfig(window_seconds=300))
        with caplog.at_level(logging.WARNING, logger="sipgw.dedupe"):
            await dd_open.evaluate(db, caller=_caller(), row_id=r2, is_test=0)
        assert any("WOULD suppress" in r.getMessage() for r in caplog.records)
        await db.close()

    @pytest.mark.asyncio
    async def test_lookup_failure_does_not_raise(self, tmp_path):
        # A broken db must not break evaluate — it logs and returns no-suppress.
        class BrokenDB:
            async def find_recent_duplicate(self, **_kw):
                raise RuntimeError("boom")
        dd = Deduper(DedupeConfig(window_seconds=300))
        dec = await dd.evaluate(BrokenDB(), caller=_caller(), row_id=1, is_test=0)
        assert dec.suppress is False and dec.duplicate_of is None

    @pytest.mark.asyncio
    async def test_would_suppress_line_has_gap_and_both_call_ids(
            self, tmp_path, caplog):
        # The enriched SHADOW audit trail must be greppable per-hit: it logs the
        # inter-page gap, the bed/purpose match flags, and BOTH pages' Call-IDs.
        db = await _db(tmp_path)
        r1 = await _pending(db)                 # prior page, sip_call_id="c"
        r2 = await _pending(db)                 # this page
        prior = await db.get_call(r1)
        later = prior["created_at"] + 12.0      # 12s after the first page
        dd = Deduper(DedupeConfig(enforce=False, window_seconds=300))
        with caplog.at_level(logging.WARNING, logger="sipgw.dedupe"):
            dec = await dd.evaluate(
                db, caller=_caller(), row_id=r2, is_test=0,
                sip_call_id="this-call", now=later)
        # Still purely telemetry — duplicate found, but never suppressed.
        assert dec.duplicate_of == r1
        assert dec.suppress is False
        msg = next(r.getMessage() for r in caplog.records
                   if "WOULD suppress" in r.getMessage())
        assert "gap=12.0s" in msg
        assert "this_call_id=this-call" in msg
        assert "dup_call_id=c" in msg
        assert "bed_match=" in msg and "purpose_match=" in msg
        await db.close()


# --------------------------------------------- #5 shadow event_id annotation
class TestEventIdShadowAnnotation:
    """event_id is ANNOTATION ONLY: it enriches the non-suppressing shadow
    audit (does the cf-v1 clinical match also share the upstream event id?),
    but it is never a match key, never suppresses, and never relaxes the
    purpose hard-guard. Every case below must still be delivered (no-suppress).
    """

    @pytest.mark.asyncio
    async def test_same_clinical_id_same_event_id_annotates_match(
            self, tmp_path, caplog):
        db = await _db(tmp_path)
        r1 = await _pending(db, event_id="EVT-42")
        r2 = await _pending(db, event_id="EVT-42")
        dd = Deduper(DedupeConfig(enforce=False, window_seconds=300))
        with caplog.at_level(logging.WARNING, logger="sipgw.dedupe"):
            dec = await dd.evaluate(
                db, caller=_caller(), row_id=r2, is_test=0, event_id="EVT-42")
        # cf-v1 clinical match, and the shared event id is annotated True...
        assert dec.duplicate_of == r1
        assert dec.event_id_match is True
        # ...yet the page is NEVER suppressed — record-first, delivered anyway.
        assert dec.suppress is False
        msg = next(r.getMessage() for r in caplog.records
                   if "WOULD suppress" in r.getMessage())
        assert "event_id_match=True" in msg
        assert "this_event_id=EVT-42" in msg and "dup_event_id=EVT-42" in msg
        await db.close()

    @pytest.mark.asyncio
    async def test_same_clinical_id_different_event_id_counter_ticked(
            self, tmp_path, caplog):
        # The 6/80 shape: same clinical identity but a DIFFERENT upstream event
        # id. Still a cf-v1 duplicate, event_id_match annotated False, no-suppress.
        db = await _db(tmp_path)
        r1 = await _pending(db, event_id="EVT-1")
        r2 = await _pending(db, event_id="EVT-2")
        dd = Deduper(DedupeConfig(enforce=False, window_seconds=300))
        with caplog.at_level(logging.WARNING, logger="sipgw.dedupe"):
            dec = await dd.evaluate(
                db, caller=_caller(), row_id=r2, is_test=0, event_id="EVT-2")
        assert dec.duplicate_of == r1          # cf-v1 clinical match stands
        assert dec.event_id_match is False     # different event id
        assert dec.suppress is False
        msg = next(r.getMessage() for r in caplog.records
                   if "WOULD suppress" in r.getMessage())
        assert "event_id_match=False" in msg
        await db.close()

    @pytest.mark.asyncio
    async def test_missing_event_id_either_side_annotates_false_no_crash(
            self, tmp_path):
        # Absent event_id on the prior row, on this page, or on both -> always
        # event_id_match=False, and evaluate never raises.
        db = await _db(tmp_path)
        # prior row has an event id, this page has none
        r1 = await _pending(db, event_id="EVT-9")
        r2 = await _pending(db, event_id="EVT-9")
        dd = Deduper(DedupeConfig(enforce=False, window_seconds=300))
        dec = await dd.evaluate(
            db, caller=_caller(), row_id=r2, is_test=0, event_id=None)
        assert dec.duplicate_of == r1 and dec.event_id_match is False
        assert dec.suppress is False

        # prior row has no event id, this page does
        r3 = await _pending(db, area="731", event_id=None)
        r4 = await _pending(db, area="731", event_id="EVT-7")
        dec2 = await dd.evaluate(
            db, caller=_caller(area="731"), row_id=r4, is_test=0,
            event_id="EVT-7")
        assert dec2.duplicate_of == r3 and dec2.event_id_match is False

        # both empty strings -> still no match, no crash
        r5 = await _pending(db, area="732", event_id="")
        r6 = await _pending(db, area="732", event_id="")
        dec3 = await dd.evaluate(
            db, caller=_caller(area="732"), row_id=r6, is_test=0, event_id="")
        assert dec3.duplicate_of == r5 and dec3.event_id_match is False
        await db.close()

    @pytest.mark.asyncio
    async def test_purpose_guard_beats_matching_event_id(self, tmp_path):
        # HARD SAFETY: even with an IDENTICAL upstream event id, RRT vs Code
        # Blue must NOT merge — the purpose hard-guard wins over event_id, so
        # there is no duplicate at all (and thus nothing to annotate).
        db = await _db(tmp_path)
        await _pending(db, display="Code Blue", event_id="EVT-SAME")
        r2 = await _pending(db, display="RRT", event_id="EVT-SAME")
        dd = Deduper(DedupeConfig(window_seconds=300))
        dec = await dd.evaluate(
            db, caller=_caller(display="RRT"), row_id=r2, is_test=0,
            event_id="EVT-SAME")
        assert dec.duplicate_of is None        # purpose guard: not a duplicate
        assert dec.would_suppress is False
        assert dec.event_id_match is False     # never annotated when no match
        assert dec.suppress is False
        await db.close()


# ------------------------------------------------- find_recent_duplicate shape
class TestFindRecentDuplicateReturn:
    @pytest.mark.asyncio
    async def test_match_returns_id_created_at_and_call_id(self, tmp_path):
        # The richer return carries the prior row's created_at + sip_call_id so
        # the caller can compute the inter-page gap and cross-reference Call-IDs.
        db = await _db(tmp_path)
        r1 = await _pending(db, event_id="EVT-carry")   # sip_call_id="c"
        r2 = await _pending(db)
        m = await db.find_recent_duplicate(
            area_number="730", room_number="201", bed_number=None,
            purpose="Code Blue", is_test=0, since_epoch=0.0, exclude_id=r2)
        assert isinstance(m, DuplicateMatch)
        assert m.id == r1
        assert m.sip_call_id == "c"
        # #5 shadow: the prior row's #15 event_id is carried back for annotation.
        assert m.event_id == "EVT-carry"
        assert isinstance(m.created_at, float) and m.created_at > 0
        await db.close()

    @pytest.mark.asyncio
    async def test_no_match_returns_none(self, tmp_path):
        # No prior page in-window -> None (unchanged contract).
        db = await _db(tmp_path)
        r1 = await _pending(db)
        m = await db.find_recent_duplicate(
            area_number="730", room_number="999", bed_number=None,
            purpose="Code Blue", is_test=0, since_epoch=0.0, exclude_id=r1)
        assert m is None
        await db.close()


# --------------------------------------------------------------- validate_config
class TestValidateConfigEnforce:
    def _prod_ok(self) -> AppConfig:
        c = AppConfig()
        c.fusion = FusionConfig(
            base_url="https://api.icmobile.singlewire.com/api",
            token_url="https://api.icmobile.singlewire.com/api/token",
            audience="prov", scenario_id="scen", scenario_field_id="field",
            client_id="cid", client_secret="secret")
        c.escalation.webhook_url = "https://hooks.example.com/escalation"
        return c

    def test_enforce_true_warns_active_not_fatal(self):
        # Enforcement is clinically signed off — enforce=True is no longer fatal;
        # it emits a loud SUPPRESSION ACTIVE warning instead (never a ConfigError).
        c = self._prod_ok()
        c.dedupe.enforce = True
        c.dedupe.window_seconds = 2
        w = validate_config(c, dry_run=False)
        assert any("SUPPRESSION ACTIVE" in x for x in w)

    def test_enforce_true_dry_run_warns_not_fatal(self):
        c = AppConfig()
        c.fusion.dry_run = True
        c.dedupe.enforce = True
        c.dedupe.window_seconds = 2
        w = validate_config(c, dry_run=True)
        assert any("SUPPRESSION ACTIVE" in x for x in w)

    def test_enforce_false_is_ok(self):
        c = self._prod_ok()
        c.dedupe.enforce = False
        c.dedupe.window_seconds = 300
        assert validate_config(c, dry_run=False) == []


# --------------------------------------------------------------- integration
def _mock_fusion(base_url: str) -> FusionConfig:
    return FusionConfig(
        base_url=base_url + "/api",
        token_url=base_url + "/api/token",
        audience="prov", scenario_id="scen-1",
        scenario_endpoint="/v1/scenario-notifications",
        variable_name="customTTS", scenario_field_id="mock-field-id",
        client_id="cid", client_secret="secret",
        dry_run=False,   # real loopback round-trip to the mock — no real send
    )


class TestTwoCodeBluesBothDelivered:
    @pytest.mark.asyncio
    async def test_two_code_blues_same_room_both_delivered(self, tmp_path):
        """The load-bearing #5 invariant: even with the shadow window WIDE OPEN
        (so the second page IS flagged a clinical duplicate), BOTH Code Blues
        for the same room are delivered. Dedupe never drops a real page.
        """
        with run_mock_fusion("200") as (base, state):
            config = AppConfig()
            config.fusion = _mock_fusion(base)
            config.database.path = str(tmp_path / "gw.db")
            # Window open on purpose: prove telemetry fires yet both deliver.
            config.dedupe = DedupeConfig(enforce=False, window_seconds=300)

            gw = SIPGateway(config)
            await gw.db.initialize()
            await gw.webhook.initialize()
            try:
                fh1 = '"Code Blue" <sip:a730r201@127.0.0.1>;tag=t1'
                fh2 = '"Code Blue" <sip:a730r201@127.0.0.1>;tag=t2'
                await gw.on_call("call-1", "a730r201", "Code Blue", fh1)
                await gw.on_call("call-2", "a730r201", "Code Blue", fh2)

                # Both rows are PENDING; drain them through the real worker.
                for _ in range(5):
                    if await gw.worker.process_once() == 0:
                        break

                rows = await gw.db.get_recent_calls(limit=10)
                assert len(rows) == 2
                # BOTH delivered — no suppression, ever.
                assert all(r["state"] == STATE_DELIVERED for r in rows)
                # The mock received TWO real scenario notifications.
                assert state.count("POST", "scenario-notifications") == 2

                # Telemetry still recorded: the newer row points at the older.
                by_id = {r["id"]: r for r in rows}
                older, newer = min(by_id), max(by_id)
                assert by_id[newer]["duplicate_of"] == older
                # The first page is not itself a duplicate of anything.
                assert by_id[older]["duplicate_of"] is None
            finally:
                await gw.webhook.close()
                await gw.db.close()
