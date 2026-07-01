"""#14 READ-ONLY database open path (the decoupled dashboard reader).

The dashboard runs in its own process and must NEVER write the shared DB. It
opens with ``read_only=True`` -> ``PRAGMA query_only=ON`` (not ``mode=ro``, so
the -wal/-shm sidecars can still build) and skips WAL/CREATE/migrate entirely.
The prod-DB barrier still runs on the read-only open.
"""

import sqlite3

import pytest

from sipgw.database import CallDatabase, STATE_PENDING
from sipgw.safety import ProdDatabaseBarrier

# An OLD (v1.5.x, 11-column) schema with NO delivery-state columns.
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


def _make_old_db(path: str) -> None:
    con = sqlite3.connect(path)
    con.execute(OLD_SCHEMA)
    con.execute(
        "INSERT INTO calls (timestamp, caller_id, created_at) VALUES (?,?,?)",
        ("2026-06-30 09:00:00", "a730r201", 1751281200.0),
    )
    con.commit()
    con.close()


async def _writer(tmp_path, name="c.db") -> CallDatabase:
    db = CallDatabase(str(tmp_path / name))
    await db.initialize()
    return db


async def _columns(db: CallDatabase) -> set:
    cur = await db._db.execute("PRAGMA table_info(calls)")
    return {row[1] for row in await cur.fetchall()}


class TestReadOnlyOpen:
    @pytest.mark.asyncio
    async def test_query_only_rejects_insert(self, tmp_path):
        # A writer first creates the schema.
        w = await _writer(tmp_path)
        await w.close()

        r = CallDatabase(str(tmp_path / "c.db"), read_only=True)
        await r.initialize()
        assert r.read_only is True
        # query_only=ON must refuse ANY write.
        with pytest.raises(sqlite3.OperationalError):
            await r._db.execute(
                "INSERT INTO calls (timestamp, caller_id, created_at, state) "
                "VALUES ('t','a',1.0,'pending')")
            await r._db.commit()
        # UPDATE is refused too.
        with pytest.raises(sqlite3.OperationalError):
            await r._db.execute("UPDATE calls SET caller_id='x'")
            await r._db.commit()
        await r.close()

    @pytest.mark.asyncio
    async def test_reader_does_not_migrate_pre_migration_copy(self, tmp_path):
        # A pre-migration (11-column) copy opened read-only must be left ALONE:
        # no ALTER TABLE, no new state/attempts/is_test columns.
        p = str(tmp_path / "old.db")
        _make_old_db(p)

        r = CallDatabase(p, read_only=True)
        await r.initialize()
        cols = await _columns(r)
        assert "state" not in cols
        assert "attempts" not in cols
        assert "is_test" not in cols
        await r.close()

        # The on-disk schema is unchanged after the read-only open.
        con = sqlite3.connect(p)
        disk_cols = {row[1] for row in con.execute("PRAGMA table_info(calls)").fetchall()}
        con.close()
        assert "state" not in disk_cols

    @pytest.mark.asyncio
    async def test_concurrent_writer_and_reader_no_database_locked(self, tmp_path):
        # WAL + busy_timeout: the writer can keep inserting while the reader pages
        # through the table, with NO "database is locked" error.
        p = str(tmp_path / "c.db")
        w = CallDatabase(p)
        await w.initialize()
        r = CallDatabase(p, read_only=True)
        await r.initialize()

        for i in range(25):
            await w.create_pending_call(
                caller_id=f"a730r{i}", display_name="Code Blue",
                area_number="730", area_name="1st Floor... E.D...",
                room_number=str(i), tts_string="Attention! Code Blue! ...")
            calls, total, pages = await r.get_calls_page(
                page=1, page_size=5, today_only=False)
            assert total >= 1  # reader sees the writer's committed rows

        # Reader observed a growing table without any lock error.
        _, total, _ = await r.get_calls_page(page=1, page_size=5, today_only=False)
        assert total == 25
        await r.close()
        await w.close()

    @pytest.mark.asyncio
    async def test_reader_excludes_test_rows(self, tmp_path):
        p = str(tmp_path / "c.db")
        w = CallDatabase(p)
        await w.initialize()
        await w.create_pending_call(
            caller_id="real", display_name="Code Blue", area_number="1",
            area_name="A", room_number="1", tts_string="x", is_test=0)
        await w.create_pending_call(
            caller_id="test", display_name="Code Blue", area_number="1",
            area_name="A", room_number="1", tts_string="x", is_test=1)
        await w.close()

        r = CallDatabase(p, read_only=True)
        await r.initialize()
        calls, total, pages = await r.get_calls_page(
            page=1, page_size=20, today_only=False)
        assert total == 1                      # test row excluded
        assert all(c["is_test"] == 0 for c in calls)
        assert calls[0]["caller_id"] == "real"
        # State-aware stats also exclude the test row.
        counts = await r.count_by_state()
        assert counts.get(STATE_PENDING) == 1
        await r.close()

    @pytest.mark.asyncio
    async def test_reader_can_read_heartbeat(self, tmp_path):
        # /health depends on the reader being able to SELECT the writer's beat.
        p = str(tmp_path / "c.db")
        w = CallDatabase(p)
        await w.initialize()
        beat = await w.write_heartbeat("writer")
        await w.close()

        r = CallDatabase(p, read_only=True)
        await r.initialize()
        back = await r.read_heartbeat("writer")
        assert back is not None and abs(back - beat) < 0.01
        await r.close()

    @pytest.mark.asyncio
    async def test_prod_db_barrier_still_runs_for_reader(self, tmp_path):
        # Safety invariant: the prod-DB barrier runs on EVERY open, including the
        # read-only one. dry-run + the prod path must be refused BEFORE any
        # connection (so the prod DB is never even touched).
        db = CallDatabase("/var/lib/sipgw/calls.db", read_only=True, dry_run=True)
        with pytest.raises(ProdDatabaseBarrier):
            await db.initialize()
        assert db._db is None  # never connected
