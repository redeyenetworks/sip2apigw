"""#7 heartbeat-row /health tests."""

import time

import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient

from sipgw.config import DashboardConfig, HealthConfig
from sipgw.database import CallDatabase
from sipgw.dashboard import create_dashboard


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


def _client(beat, stale_after=30.0):
    db = AsyncMock(spec=CallDatabase)
    db.read_heartbeat = AsyncMock(return_value=beat)
    app = create_dashboard(db, DashboardConfig(),
                           health_config=HealthConfig(stale_after_seconds=stale_after))
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
