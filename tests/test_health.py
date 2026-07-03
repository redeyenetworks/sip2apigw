"""#7 heartbeat-row /health tests + Fusion reachability keepalive.

Covers the original heartbeat-driven /health contract AND the additive #7
keepalive: a bounded, READ-ONLY reachability probe whose result is stamped to
the DB and surfaced in /health as INFORMATIONAL fields only. Load-bearing
invariants asserted here:

  * in effective dry-run the keepalive reaches NO real host (no-send guard);
  * a keepalive FAILURE is surfaced but NEVER flips the /health status code —
    that stays keyed solely on writer-heartbeat freshness.
"""

import time

import httpx
import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient

from sipgw.config import DashboardConfig, HealthConfig, FusionConfig
from sipgw.database import (
    CallDatabase, STATE_DELIVERED, STATE_FAILED,
)
from sipgw.dashboard import create_dashboard
from sipgw.webhook import FusionWebhook


class TestHeartbeatDB:
    @pytest.mark.asyncio
    async def test_write_then_read(self, tmp_path):
        db = CallDatabase(str(tmp_path / "h.db"))
        await db.initialize()
        assert await db.read_heartbeat("writer") is None    # never stamped
        t = await db.write_heartbeat("writer")
        back = await db.read_heartbeat("writer")
        assert abs(back - t) < 0.01
        # UPSERT: second write updates in place (single row).
        t2 = await db.write_heartbeat("writer")
        assert (await db.read_heartbeat("writer")) >= t
        await db.close()


class TestFusionCheckDB:
    @pytest.mark.asyncio
    async def test_write_then_read_ok_and_fail(self, tmp_path):
        db = CallDatabase(str(tmp_path / "f.db"))
        await db.initialize()
        assert await db.read_fusion_check() is None          # never stamped
        t = await db.write_fusion_check(True, "HTTP 200")
        fc = await db.read_fusion_check()
        assert fc["ok"] is True and fc["detail"] == "HTTP 200"
        assert abs(fc["checked_at"] - t) < 0.01
        # UPSERT in place: a later failure replaces the prior row.
        await db.write_fusion_check(False, "ConnectError: boom")
        fc = await db.read_fusion_check()
        assert fc["ok"] is False and fc["detail"] == "ConnectError: boom"
        await db.close()

    @pytest.mark.asyncio
    async def test_detail_is_truncated(self, tmp_path):
        db = CallDatabase(str(tmp_path / "f.db"))
        await db.initialize()
        await db.write_fusion_check(False, "x" * 500)
        fc = await db.read_fusion_check()
        assert len(fc["detail"]) == 200
        await db.close()

    @pytest.mark.asyncio
    async def test_reader_can_read_fusion_check_query_only(self, tmp_path):
        # /health (read-only dashboard process) must SELECT the stamped result
        # under query_only=ON exactly like it reads the heartbeat.
        p = str(tmp_path / "c.db")
        w = CallDatabase(p)
        await w.initialize()
        await w.write_fusion_check(True, "HTTP 200")
        await w.close()

        r = CallDatabase(p, read_only=True)
        await r.initialize()
        fc = await r.read_fusion_check()
        assert fc is not None and fc["ok"] is True
        await r.close()

    @pytest.mark.asyncio
    async def test_read_tolerates_missing_table(self, tmp_path):
        # An older writer that predates #7 has no fusion_check table; reading it
        # must degrade to None (never raise), so /health stays informational-safe.
        db = CallDatabase(str(tmp_path / "f.db"))
        await db.initialize()
        await db._db.execute("DROP TABLE fusion_check")
        await db._db.commit()
        assert await db.read_fusion_check() is None
        await db.close()

    @pytest.mark.asyncio
    async def test_delivery_snapshot(self, tmp_path):
        db = CallDatabase(str(tmp_path / "s.db"))
        await db.initialize()
        # One pending (backlog), one delivered, one failed REAL row.
        await db.create_pending_call(
            caller_id="a1", display_name="Code Blue", area_number="1",
            area_name="A", room_number="1", tts_string="x")
        did = await db.create_pending_call(
            caller_id="a2", display_name="Code Blue", area_number="1",
            area_name="A", room_number="2", tts_string="x")
        fid = await db.create_pending_call(
            caller_id="a3", display_name="Code Blue", area_number="1",
            area_name="A", room_number="3", tts_string="x")
        await db._db.execute(
            "UPDATE calls SET state=?, delivered_at=? WHERE id=?",
            (STATE_DELIVERED, time.time(), did))
        await db._db.execute(
            "UPDATE calls SET state=?, last_error=? WHERE id=?",
            (STATE_FAILED, "HTTP 503", fid))
        await db._db.commit()

        snap = await db.delivery_health_snapshot()
        assert snap["backlog"] == 1                     # the one pending row
        assert snap["last_delivered_at"] is not None
        assert snap["last_failed_at"] is not None
        assert snap["last_error"] == "HTTP 503"
        await db.close()


def _client(beat, stale_after=30.0, fusion=None, snapshot=None,
            fail_on_fusion_unreachable=False,
            fusion_unreachable_max_age_seconds=0.0,
            keepalive_interval_seconds=300.0):
    db = AsyncMock(spec=CallDatabase)
    db.read_heartbeat = AsyncMock(return_value=beat)
    db.read_fusion_check = AsyncMock(return_value=fusion)
    db.delivery_health_snapshot = AsyncMock(return_value=snapshot or {
        "backlog": 0, "last_delivered_at": None,
        "last_failed_at": None, "last_error": None,
    })
    app = create_dashboard(db, DashboardConfig(),
                           health_config=HealthConfig(
                               stale_after_seconds=stale_after,
                               keepalive_interval_seconds=keepalive_interval_seconds,
                               fail_on_fusion_unreachable=fail_on_fusion_unreachable,
                               fusion_unreachable_max_age_seconds=fusion_unreachable_max_age_seconds))
    return TestClient(app)


class TestHealthEndpoint:
    def test_fresh_is_200_ok(self):
        r = _client(time.time()).get("/health")
        assert r.status_code == 200 and r.json()["status"] == "ok"

    def test_stale_is_503(self):
        r = _client(time.time() - 999).get("/health")
        assert r.status_code == 503 and r.json()["status"] == "stale"

    def test_no_heartbeat_is_503(self):
        r = _client(None).get("/health")
        assert r.status_code == 503 and r.json()["status"] == "no-heartbeat"

    def test_informational_fields_surfaced_on_healthy(self):
        fusion = {"ok": True, "checked_at": time.time() - 5, "detail": "HTTP 200"}
        snap = {"backlog": 3, "last_delivered_at": 1751000000.0,
                "last_failed_at": None, "last_error": None}
        r = _client(time.time(), fusion=fusion, snapshot=snap).get("/health")
        j = r.json()
        assert r.status_code == 200 and j["status"] == "ok"
        assert j["fusion_reachable"] is True
        assert j["fusion_detail"] == "HTTP 200"
        assert j["fusion_checked_age_s"] >= 0
        assert j["backlog"] == 3
        assert j["last_delivered_at"] == 1751000000.0

    def test_keepalive_failure_does_not_flip_status(self):
        # A Fusion-unreachable keepalive result MUST NOT change the 200 status
        # while the heartbeat is fresh — the status code is heartbeat-only.
        fusion = {"ok": False, "checked_at": time.time() - 5,
                  "detail": "ConnectError: no route"}
        r = _client(time.time(), fusion=fusion).get("/health")
        j = r.json()
        assert r.status_code == 200 and j["status"] == "ok"
        assert j["fusion_reachable"] is False
        assert "ConnectError" in j["fusion_detail"]

    def test_missing_fusion_check_reports_none(self):
        r = _client(time.time(), fusion=None).get("/health")
        j = r.json()
        assert r.status_code == 200
        assert j["fusion_reachable"] is None


class TestFusionUnreachableDegrade:
    """#7 opt-in, config-gated /health degrade (default OFF)."""

    def test_default_off_fresh_fail_stays_200(self):
        # Guards test_keepalive_failure_does_not_flip_status: with the flag OFF,
        # a fresh ok=False probe MUST NOT change the 200 status.
        fusion = {"ok": False, "checked_at": time.time() - 5,
                  "detail": "ConnectError: no route"}
        r = _client(time.time(), fusion=fusion).get("/health")
        j = r.json()
        assert r.status_code == 200 and j["status"] == "ok"
        assert j["fusion_reachable"] is False

    def test_on_fresh_fail_is_503_fusion_unreachable(self):
        fusion = {"ok": False, "checked_at": time.time() - 5,
                  "detail": "ConnectError: no route"}
        r = _client(time.time(), fusion=fusion,
                    fail_on_fusion_unreachable=True).get("/health")
        j = r.json()
        assert r.status_code == 503 and j["status"] == "fusion-unreachable"
        assert "ConnectError" in j["fusion_detail"]
        assert j["fusion_checked_age_s"] >= 0

    def test_on_none_check_stays_200(self):
        # Never stamped / older writer (read_fusion_check -> None): unknown is
        # NOT unreachable, so /health stays 200.
        r = _client(time.time(), fusion=None,
                    fail_on_fusion_unreachable=True).get("/health")
        j = r.json()
        assert r.status_code == 200 and j["status"] == "ok"
        assert j["fusion_reachable"] is None

    def test_on_stale_fail_stays_200(self):
        # A stuck/old ok=False probe beyond the freshness bound must NOT degrade.
        # Auto bound = keepalive*2 + stale_after = 60*2 + 30 = 150s; use 10000s.
        fusion = {"ok": False, "checked_at": time.time() - 10000,
                  "detail": "ConnectError: no route"}
        r = _client(time.time(), fusion=fusion,
                    fail_on_fusion_unreachable=True,
                    keepalive_interval_seconds=60.0).get("/health")
        j = r.json()
        assert r.status_code == 200 and j["status"] == "ok"

    def test_on_fresh_fail_within_explicit_max_age_is_503(self):
        fusion = {"ok": False, "checked_at": time.time() - 40,
                  "detail": "boom"}
        r = _client(time.time(), fusion=fusion,
                    fail_on_fusion_unreachable=True,
                    fusion_unreachable_max_age_seconds=60.0).get("/health")
        assert r.status_code == 503 and r.json()["status"] == "fusion-unreachable"

    def test_on_ok_check_stays_200(self):
        fusion = {"ok": True, "checked_at": time.time() - 5, "detail": "HTTP 200"}
        r = _client(time.time(), fusion=fusion,
                    fail_on_fusion_unreachable=True).get("/health")
        j = r.json()
        assert r.status_code == 200 and j["status"] == "ok"
        assert j["fusion_reachable"] is True

    def test_heartbeat_stale_still_wins_with_flag_on(self):
        # Heartbeat gate stays authoritative: a dead writer reports 'stale', not
        # masked as a Fusion problem, even with a fresh ok=False probe present.
        fusion = {"ok": False, "checked_at": time.time() - 5, "detail": "boom"}
        r = _client(time.time() - 999, fusion=fusion,
                    fail_on_fusion_unreachable=True).get("/health")
        assert r.status_code == 503 and r.json()["status"] == "stale"

    def test_no_heartbeat_still_wins_with_flag_on(self):
        r = _client(None, fail_on_fusion_unreachable=True).get("/health")
        assert r.status_code == 503 and r.json()["status"] == "no-heartbeat"


def _real_host_config(**overrides) -> FusionConfig:
    cfg = FusionConfig(
        base_url="https://api.icmobile.singlewire.com/api",
        token_url="https://api.icmobile.singlewire.com/api/token",
        audience="provider-uuid",
        scenario_id="scenario-uuid",
        scenario_endpoint="/v1/scenario-notifications",
        variable_name="customTTS",
        scenario_field_id="preset-field",
        client_id="cid",
        client_secret="supersecret",
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class TestKeepaliveProbe:
    @pytest.mark.asyncio
    async def test_dry_run_reaches_no_real_host(self):
        # THE no-send invariant for the keepalive: in dry-run the reachability
        # GET is refused by the guard (recorded in blocked), nothing forwarded.
        wh = FusionWebhook(_real_host_config(dry_run=True))
        await wh.initialize()
        try:
            ok, detail = await wh.check_reachable()
            assert ok is True                    # synthetic 200 from the guard
            assert detail == "HTTP 200"
            t = wh._transport
            assert t is not None, "dry-run must install the guard transport"
            assert t.forwarded == [], f"real send leaked: {t.forwarded}"
            # The reachability GET of the scenario was exercised and refused.
            blocked = [(m, httpx.URL(u).path) for m, u in t.blocked]
            assert any(m == "GET" and "/v1/scenarios/" in p for m, p in blocked), blocked
            # It is a READ-ONLY probe: it must NOT POST to the trigger endpoint.
            assert not any(m == "POST" and p.endswith("/scenario-notifications")
                           for m, p in blocked), blocked
        finally:
            await wh.close()

    @pytest.mark.asyncio
    async def test_success_stamps_db_and_surfaces_in_health(self, tmp_path):
        wh = FusionWebhook(_real_host_config(dry_run=True))
        await wh.initialize()
        db = CallDatabase(str(tmp_path / "k.db"))
        await db.initialize()
        try:
            ok, detail = await wh.check_reachable()
            await db.write_fusion_check(ok, detail)
        finally:
            await wh.close()

        fc = await db.read_fusion_check()
        assert fc["ok"] is True and fc["detail"] == "HTTP 200"

        # Surfaced in /health via the read-only reader path.
        app = create_dashboard(db, DashboardConfig(), health_config=HealthConfig())
        await db.write_heartbeat("writer")
        j = TestClient(app).get("/health").json()
        assert j["fusion_reachable"] is True
        await db.close()

    @pytest.mark.asyncio
    async def test_failure_stamps_db_and_status_stays_200(self, tmp_path):
        # A transport error becomes (False, detail); stamped and surfaced, but
        # /health stays 200 while the heartbeat is fresh.
        cfg = _real_host_config(dry_run=False)   # no guard: real GET will fail
        cfg.base_url = "http://127.0.0.1:9/api"  # nothing listening -> error
        cfg.token_url = "http://127.0.0.1:9/api/token"
        wh = FusionWebhook(cfg)
        await wh.initialize()
        db = CallDatabase(str(tmp_path / "k.db"))
        await db.initialize()
        try:
            ok, detail = await wh.check_reachable(timeout=0.5)
            assert ok is False and detail
            await db.write_fusion_check(ok, detail)
        finally:
            await wh.close()

        await db.write_heartbeat("writer")
        app = create_dashboard(db, DashboardConfig(), health_config=HealthConfig())
        r = TestClient(app).get("/health")
        assert r.status_code == 200          # heartbeat fresh -> still healthy
        assert r.json()["fusion_reachable"] is False
        await db.close()
