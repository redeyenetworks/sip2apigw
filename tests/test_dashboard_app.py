"""#14 Decoupled dashboard process (sipgw.dashboard_app).

Builds the dashboard app against a STAGING DB opened read-only and drives it
over HTTP on the SAME event loop (httpx ASGITransport, no cross-loop aiosqlite
mixing), proving: /health 200 by READING the writer's heartbeat, the reader
excludes is_test=1 rows, and the read-only reader never writes.
"""

import time

import httpx
import pytest
from fastapi import FastAPI

import sipgw.dashboard_app as dashboard_app
from sipgw.config import AppConfig
from sipgw.database import CallDatabase


async def _seed_staging_db(path: str) -> None:
    """A writer creates a staging DB: one real page, one test page, a heartbeat."""
    w = CallDatabase(path)
    await w.initialize()
    await w.write_heartbeat("writer")
    await w.create_pending_call(
        caller_id="a730r201", display_name="Code Blue", area_number="730",
        area_name="1st Floor... E.D...", room_number="201",
        tts_string="Attention! Code Blue! ... Room 201.", is_test=0)
    await w.create_pending_call(
        caller_id="a999r999", display_name="Code Blue", area_number="999",
        area_name="TEST", room_number="999",
        tts_string="TEST page", is_test=1)
    await w.close()


def _staging_config(tmp_path) -> AppConfig:
    cfg = AppConfig()
    cfg.fusion.dry_run = True
    cfg.database.path = str(tmp_path / "staging.db")
    cfg.logging.log_dir = str(tmp_path)          # log tail reads (empty) safely
    cfg.dashboard.bind_ip = "127.0.0.1"
    cfg.dashboard.port = 0                        # never actually bound here
    return cfg


class TestBuildDashboard:
    @pytest.mark.asyncio
    async def test_build_dashboard_returns_readonly_db_and_app(self, tmp_path):
        cfg = _staging_config(tmp_path)
        await _seed_staging_db(cfg.database.path)

        db, app = await dashboard_app.build_dashboard(cfg, dry_run=True)
        try:
            assert isinstance(app, FastAPI)
            assert isinstance(db, CallDatabase)
            assert db.read_only is True
            # Reader can read the heartbeat and today's real (non-test) rows.
            assert await db.read_heartbeat("writer") is not None
            calls, total, _ = await db.get_calls_page(
                page=1, page_size=20, today_only=False)
            assert total == 1
            assert all(c["is_test"] == 0 for c in calls)
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_boots_and_serves_health_and_index_over_http(self, tmp_path):
        cfg = _staging_config(tmp_path)
        await _seed_staging_db(cfg.database.path)

        db, app = await dashboard_app.build_dashboard(cfg, dry_run=True)
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://dash") as client:
                # /health 200 by reading the fresh writer heartbeat.
                r = await client.get("/health")
                assert r.status_code == 200
                assert r.json()["status"] == "ok"
                # Index renders the real call and hides the test-only page.
                r2 = await client.get("/")
                assert r2.status_code == 200
                assert "a730r201" in r2.text
                assert "a999r999" not in r2.text
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_health_503_when_heartbeat_stale(self, tmp_path):
        cfg = _staging_config(tmp_path)
        # Seed a DB whose heartbeat is old, then a tiny stale window.
        w = CallDatabase(cfg.database.path)
        await w.initialize()
        await w._db.execute(
            "INSERT INTO heartbeat (name, beat_at) VALUES ('writer', ?)",
            (time.time() - 999,))
        await w._db.commit()
        await w.close()
        cfg.health.stale_after_seconds = 1.0

        db, app = await dashboard_app.build_dashboard(cfg, dry_run=True)
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://dash") as client:
                r = await client.get("/health")
                assert r.status_code == 503
                assert r.json()["status"] == "stale"
        finally:
            await db.close()


class TestModuleShape:
    def test_module_has_main_and_uses_uvicorn(self):
        # The process entry point exists; uvicorn lives HERE (not in main).
        assert callable(dashboard_app.main)
        assert "uvicorn" in vars(dashboard_app)

    def test_prod_db_barrier_refuses_dry_run_prod(self, tmp_path, monkeypatch):
        # dashboard_app.main() mirrors the writer bootstrap: dry-run + prod DB
        # path must be refused before serving.
        import textwrap
        from sipgw.safety import ProdDatabaseBarrier
        cfg = tmp_path / "config.yaml"
        cfg.write_text(textwrap.dedent("""
            fusion:
              dry_run: true
            database:
              path: "/var/lib/sipgw/calls.db"
            logging:
              log_dir: "%s"
        """ % tmp_path))
        monkeypatch.setattr("sys.argv", ["sipgw.dashboard_app", str(cfg)])
        with pytest.raises(ProdDatabaseBarrier):
            dashboard_app.main()
