"""#2 migration + delivery-state model tests.

Hermetic: builds an OLD-schema (v1.5.x, 11-column) DB in a temp file, then
migrates it — mirroring what will happen to the production DB — and asserts no
data is lost and the state machine works.
"""

import sqlite3

import pytest

from sipgw.database import (
    CallDatabase,
    STATE_DELIVERED,
    STATE_FAILED,
    STATE_LEGACY,
    STATE_PENDING,
)

OLD_SCHEMA = """
CREATE TABLE calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    caller_id TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    area_number TEXT,
    area_name TEXT DEFAULT '',
    room_number TEXT,
    tts_string TEXT DEFAULT '',
    fusion_status INTEGER,
    response_time_ms REAL,
    created_at REAL NOT NULL
)
"""

# (timestamp, caller_id, display_name, area_number, area_name, room_number,
#  tts_string, fusion_status, response_time_ms, created_at)
_SAMPLE_OLD_ROWS = [
    ("2026-06-30 09:00:00", "a730r201", "Code Blue", "730", "1st Floor... E.D...",
     "201", "Attention! Code Blue! ...", 200, 640.0, 1751281200.0),
    ("2026-06-30 09:05:00", "a797r2201b1", "Code Blue", "797", "2nd Floor... Heart Center...",
     "2201", "Attention! Code Blue! ... Prepost 1.", 500, 610.0, 1751281500.0),
    ("2026-06-30 09:10:00", "a731r400", "RRT", "731", "4th Floor... I.C.U...",
     "400", "Attention! RRT! ...", None, None, 1751281800.0),
]


def _make_old_db(path: str, rows=_SAMPLE_OLD_ROWS) -> None:
    con = sqlite3.connect(path)
    con.execute(OLD_SCHEMA)
    con.executemany(
        "INSERT INTO calls (timestamp, caller_id, display_name, area_number, "
        "area_name, room_number, tts_string, fusion_status, response_time_ms, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()


def _columns(path: str) -> set:
    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(calls)").fetchall()}
    con.close()
    return cols


class TestMigration:
    @pytest.mark.asyncio
    async def test_adds_columns_backfills_legacy_no_data_loss(self, tmp_path):
        p = str(tmp_path / "old.db")
        _make_old_db(p)
        db = CallDatabase(p)
        await db.initialize()
        try:
            # New columns present.
            cols = _columns(p)
            for c in ("state", "attempts", "last_error", "delivered_at",
                      "sip_call_id", "duplicate_of", "is_test", "event_id"):
                assert c in cols, c
            # No rows lost; all pre-existing rows backfilled as legacy.
            rows = await db.get_recent_calls(limit=100)
            assert len(rows) == 3
            assert all(r["state"] == STATE_LEGACY for r in rows)
            assert all(r["attempts"] == 0 for r in rows)
            assert all(r["is_test"] == 0 for r in rows)
            # #15: event_id is a nullable ADD COLUMN — no backfill, legacy rows NULL.
            assert all(r["event_id"] is None for r in rows)
            # Original values preserved.
            byid = {r["caller_id"]: r for r in rows}
            assert byid["a797r2201b1"]["tts_string"].endswith("Prepost 1.")
            assert byid["a730r201"]["fusion_status"] == 200
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_migration_is_idempotent(self, tmp_path):
        p = str(tmp_path / "old.db")
        _make_old_db(p)
        db = CallDatabase(p)
        await db.initialize()
        added_second = await db._migrate()      # explicit second run
        assert added_second == []                # nothing to add
        rows = await db.get_recent_calls(limit=100)
        assert len(rows) == 3
        await db.close()
        # Re-open (full initialize again) — still fine, still 3 rows.
        db2 = CallDatabase(p)
        await db2.initialize()
        assert len(await db2.get_recent_calls(limit=100)) == 3
        await db2.close()

    @pytest.mark.asyncio
    async def test_wal_enabled(self, tmp_path):
        p = str(tmp_path / "fresh.db")
        db = CallDatabase(p)
        await db.initialize()
        cur = await db._db.execute("PRAGMA journal_mode")
        assert (await cur.fetchone())[0].lower() == "wal"
        await db.close()

    @pytest.mark.asyncio
    async def test_fresh_db_has_full_schema(self, tmp_path):
        p = str(tmp_path / "fresh.db")
        db = CallDatabase(p)
        await db.initialize()
        cols = _columns(p)
        assert {"state", "attempts", "is_test", "sip_call_id", "event_id"} <= cols
        await db.close()

    @pytest.mark.asyncio
    async def test_event_id_index_created_and_idempotent(self, tmp_path):
        # #15: idx_calls_event_id makes the column a usable merge/dedup key
        # (the #17 HA-Phase-2 prerequisite). Created after the ADD COLUMN.
        p = str(tmp_path / "old.db")
        _make_old_db(p)
        db = CallDatabase(p)
        await db.initialize()
        try:
            cur = await db._db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_calls_event_id'")
            assert (await cur.fetchone()) is not None
            # a second migrate must not error (CREATE INDEX IF NOT EXISTS)
            assert await db._migrate() == []
        finally:
            await db.close()


class TestDeliveryStateMachine:
    async def _fresh(self, tmp_path):
        db = CallDatabase(str(tmp_path / "d.db"))
        await db.initialize()
        return db

    @pytest.mark.asyncio
    async def test_create_pending_then_delivered(self, tmp_path):
        db = await self._fresh(tmp_path)
        cid = await db.create_pending_call(
            caller_id="a730r201", display_name="Code Blue", area_number="730",
            area_name="1st Floor... E.D...", room_number="201",
            tts_string="Attention! Code Blue! ...", sip_call_id="callid-1")
        row = await db.get_call(cid)
        assert row["state"] == STATE_PENDING and row["attempts"] == 0
        assert row["sip_call_id"] == "callid-1"

        n = await db.mark_attempting(cid)
        assert n == 1
        await db.mark_delivered(cid, 200, 512.0)
        row = await db.get_call(cid)
        assert row["state"] == STATE_DELIVERED
        assert row["fusion_status"] == 200 and row["delivered_at"] is not None
        await db.close()

    @pytest.mark.asyncio
    async def test_create_pending_persists_event_id(self, tmp_path):
        # #15: event_id threads through create_pending_call and reads back;
        # omitting it leaves the column NULL (no default backfill).
        db = await self._fresh(tmp_path)
        with_evt = await db.create_pending_call(
            caller_id="a730r201", display_name="Code Blue", area_number="730",
            area_name="E.D.", room_number="201", tts_string="Code Blue!",
            sip_call_id="A1-B1-EVT7788-C1-0-13c4-764@h", event_id="EVT7788")
        assert (await db.get_call(with_evt))["event_id"] == "EVT7788"

        without = await db.create_pending_call(
            caller_id="a731r400", display_name="Code Blue", area_number="731",
            area_name="I.C.U.", room_number="400", tts_string="Code Blue!")
        assert (await db.get_call(without))["event_id"] is None
        await db.close()

    @pytest.mark.asyncio
    async def test_get_deliverable_orders_and_filters(self, tmp_path):
        db = await self._fresh(tmp_path)
        a = await db.create_pending_call(caller_id="a", display_name="Code Blue",
            area_number="1", area_name="A", room_number="1", tts_string="x")
        b = await db.create_pending_call(caller_id="b", display_name="Code Blue",
            area_number="1", area_name="A", room_number="2", tts_string="y")
        await db.mark_attempting(a)
        await db.mark_delivered(a, 200, 1.0)     # a is done
        deliverable = await db.get_deliverable(limit=10)
        ids = [r["id"] for r in deliverable]
        assert ids == [b]                        # only pending, oldest-first
        await db.close()

    @pytest.mark.asyncio
    async def test_reschedule_then_fail(self, tmp_path):
        db = await self._fresh(tmp_path)
        cid = await db.create_pending_call(caller_id="a", display_name="Code Blue",
            area_number="1", area_name="A", room_number="1", tts_string="x")
        assert (await db.mark_attempting(cid)) == 1
        await db.reschedule(cid, last_error="HTTP 500")
        row = await db.get_call(cid)
        assert row["state"] == STATE_PENDING and row["last_error"] == "HTTP 500"
        assert row["attempts"] == 1              # attempts preserved across reschedule
        assert (await db.mark_attempting(cid)) == 2
        await db.mark_failed(cid, last_error="budget exhausted", fusion_status=-1)
        row = await db.get_call(cid)
        assert row["state"] == STATE_FAILED and row["attempts"] == 2
        await db.close()

    @pytest.mark.asyncio
    async def test_recover_inflight(self, tmp_path):
        db = await self._fresh(tmp_path)
        cid = await db.create_pending_call(caller_id="a", display_name="Code Blue",
            area_number="1", area_name="A", room_number="1", tts_string="x")
        await db.mark_attempting(cid)            # now 'delivering' (crash mid-send)
        assert (await db.get_call(cid))["state"] == "delivering"
        recovered = await db.recover_inflight()
        assert recovered == 1
        assert (await db.get_call(cid))["state"] == STATE_PENDING
        await db.close()

    @pytest.mark.asyncio
    async def test_count_by_state_excludes_test(self, tmp_path):
        db = await self._fresh(tmp_path)
        await db.create_pending_call(caller_id="real", display_name="Code Blue",
            area_number="1", area_name="A", room_number="1", tts_string="x")
        await db.create_pending_call(caller_id="test", display_name="Code Blue",
            area_number="1", area_name="A", room_number="1", tts_string="x", is_test=1)
        counts = await db.count_by_state()               # excludes test by default
        assert counts.get(STATE_PENDING) == 1
        counts_all = await db.count_by_state(include_test=True)
        assert counts_all.get(STATE_PENDING) == 2
        await db.close()
