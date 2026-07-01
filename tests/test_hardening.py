"""Post-review hardenings from the #5/#14/#13-P1 adversarial verify lenses."""

import time

import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient

from sipgw.config import DashboardConfig
from sipgw.database import CallDatabase
from sipgw.dashboard import create_dashboard


class TestReadOnlyExistenceGuard:
    @pytest.mark.asyncio
    async def test_missing_db_raises_and_does_not_create(self, tmp_path):
        # #14: a dashboard that boots before the writer must NOT create the prod DB.
        p = tmp_path / "nope.db"
        db = CallDatabase(str(p), read_only=True)
        with pytest.raises(FileNotFoundError):
            await db.initialize()
        assert not p.exists()


class TestFindRecentDuplicateNullGuard:
    @pytest.mark.asyncio
    async def test_null_area_room_returns_none(self, tmp_path):
        # #5: a malformed page (NULL area/room) must not shadow-match other NULLs.
        db = CallDatabase(str(tmp_path / "d.db"))
        await db.initialize()
        await db._db.execute(
            "INSERT INTO calls (timestamp,caller_id,area_number,room_number,"
            "created_at,state,is_test) VALUES (?,?,?,?,?,?,0)",
            ("2026-07-01T00:00:00.000Z", "x", None, None, time.time(), "delivered"))
        await db._db.commit()
        r = await db.find_recent_duplicate(
            area_number=None, room_number=None, bed_number=None,
            purpose="Code Blue", is_test=0, since_epoch=0.0)
        assert r is None
        await db.close()


class TestCsvInjectionGuard:
    def test_leading_formula_char_neutralized(self):
        # #13-P1: spreadsheet formula-injection guard on user-controlled cells.
        db = AsyncMock(spec=CallDatabase)
        row = {"created_at": time.time(), "caller_id": "=cmd|calc",
               "area_name": "A", "area_number": "730", "room_number": "201",
               "tts_string": "=HYPERLINK(1)", "state": "delivered", "fusion_status": 200}
        db.export_calls = AsyncMock(return_value=[row])
        client = TestClient(create_dashboard(db, DashboardConfig()))
        r = client.get("/export.csv")
        assert r.status_code == 200
        assert "'=cmd|calc" in r.text
        assert "'=HYPERLINK(1)" in r.text
