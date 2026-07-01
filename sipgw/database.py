"""SQLite database for call records.

Stores call history for the dashboard. Uses aiosqlite for async access.
"""

import time
import logging
import aiosqlite
from pathlib import Path
from typing import Optional, List, Dict, Any

from .config import DatabaseConfig

logger = logging.getLogger("sipgw.database")

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS calls (
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

CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_calls_created_at ON calls(created_at DESC)
"""

CREATE_STATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_calls_state ON calls(state)
"""

# #2 durable-delivery columns, added idempotently to the existing `calls` table.
# ADD COLUMN fills pre-existing rows with the DEFAULT, so the 301 legacy prod
# rows become state='legacy', attempts=0, is_test=0 without data loss.
_NEW_COLUMNS = [
    ("state",        "TEXT NOT NULL DEFAULT 'legacy'"),   # pending|delivering|delivered|failed|expired|legacy
    ("attempts",     "INTEGER NOT NULL DEFAULT 0"),
    ("last_error",   "TEXT"),
    ("delivered_at", "REAL"),
    ("sip_call_id",  "TEXT"),
    ("duplicate_of", "INTEGER"),                          # #5 dedupe shadow (column now, use later)
    ("is_test",      "INTEGER NOT NULL DEFAULT 0"),
]

# Delivery states.
STATE_PENDING = "pending"
STATE_DELIVERING = "delivering"
STATE_DELIVERED = "delivered"
STATE_FAILED = "failed"
STATE_EXPIRED = "expired"
STATE_LEGACY = "legacy"


class CallDatabase:
    """Async SQLite database for storing call records."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        """Open the connection, apply durability pragmas, create + migrate."""
        db_dir = Path(self.db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row

        # Durability + concurrency: WAL lets the read-only dashboard (#14) read
        # while the writer commits; busy_timeout avoids spurious "locked" errors.
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("PRAGMA synchronous=NORMAL")

        await self._db.execute(CREATE_TABLE)
        await self._db.execute(CREATE_INDEX)
        await self._migrate()
        await self._db.commit()

        mode_row = await (await self._db.execute("PRAGMA journal_mode")).fetchone()
        logger.info(f"Database initialized at {self.db_path} (journal_mode={mode_row[0]})")

    async def _migrate(self) -> List[str]:
        """Idempotently add the #2 durable-delivery columns and state index.

        Safe to run repeatedly; only missing columns are added. Returns the list
        of columns added on this run (empty if the schema was already current).
        """
        cur = await self._db.execute("PRAGMA table_info(calls)")
        existing = {row[1] for row in await cur.fetchall()}

        added: List[str] = []
        for name, ddl in _NEW_COLUMNS:
            if name not in existing:
                await self._db.execute(f"ALTER TABLE calls ADD COLUMN {name} {ddl}")
                added.append(name)

        await self._db.execute(CREATE_STATE_INDEX)
        await self._db.commit()

        if added:
            logger.info("DB migration added columns: %s", ", ".join(added))
        else:
            logger.info("DB migration: schema already current")
        return added

    async def record_call(
        self,
        caller_id: str,
        display_name: str,
        area_number: Optional[str],
        area_name: str,
        room_number: Optional[str],
        tts_string: str,
        fusion_status: Optional[int],
        response_time_ms: Optional[float],
        is_test: int = 0,
    ) -> int:
        """Insert a completed call record. Returns the row ID.

        Legacy one-shot path (kept for compatibility). ``state`` is derived from
        the delivery outcome so rows are meaningful under the new schema. The
        durable path uses ``create_pending_call`` + the mark_* transitions.
        """
        now = time.time()
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        if fusion_status is not None and 200 <= fusion_status < 300:
            state = STATE_DELIVERED
            delivered_at = now
        elif fusion_status is not None:
            state = STATE_FAILED
            delivered_at = None
        else:
            state = STATE_PENDING
            delivered_at = None

        cursor = await self._db.execute(
            """INSERT INTO calls
               (timestamp, caller_id, display_name, area_number, area_name,
                room_number, tts_string, fusion_status, response_time_ms, created_at,
                state, attempts, is_test, delivered_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                timestamp, caller_id, display_name, area_number, area_name,
                room_number, tts_string, fusion_status, response_time_ms, now,
                state, 0, is_test, delivered_at,
            ),
        )
        await self._db.commit()
        logger.info(f"Recorded call {cursor.lastrowid}: {caller_id} -> {tts_string}")
        return cursor.lastrowid

    # ----------------------------------------------------------------- #2 outbox
    async def create_pending_call(
        self, *, caller_id: str, display_name: str,
        area_number: Optional[str], area_name: str, room_number: Optional[str],
        tts_string: str, sip_call_id: Optional[str] = None, is_test: int = 0,
    ) -> int:
        """Record-first: persist the intent as a PENDING row before delivery.

        This is what makes delivery durable — the page survives a crash or a
        Fusion outage between record and send, and the retry worker picks it up.
        """
        now = time.time()
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        cursor = await self._db.execute(
            """INSERT INTO calls
               (timestamp, caller_id, display_name, area_number, area_name,
                room_number, tts_string, fusion_status, response_time_ms, created_at,
                state, attempts, is_test, sip_call_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                timestamp, caller_id, display_name, area_number, area_name,
                room_number, tts_string, None, None, now,
                STATE_PENDING, 0, is_test, sip_call_id,
            ),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def get_deliverable(
        self, limit: int = 50, states: tuple = (STATE_PENDING,)
    ) -> List[Dict[str, Any]]:
        """Fetch oldest-first rows in the given delivery states."""
        placeholders = ",".join("?" for _ in states)
        cursor = await self._db.execute(
            f"SELECT * FROM calls WHERE state IN ({placeholders}) "
            f"ORDER BY created_at ASC LIMIT ?",
            (*states, limit),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def mark_attempting(self, call_id: int) -> int:
        """Move a row to 'delivering' and increment attempts. Returns attempts."""
        await self._db.execute(
            "UPDATE calls SET state=?, attempts=attempts+1 WHERE id=?",
            (STATE_DELIVERING, call_id),
        )
        await self._db.commit()
        cur = await self._db.execute("SELECT attempts FROM calls WHERE id=?", (call_id,))
        row = await cur.fetchone()
        return row[0] if row else 0

    async def mark_delivered(self, call_id: int, fusion_status: int,
                             response_time_ms: Optional[float]) -> None:
        await self._db.execute(
            "UPDATE calls SET state=?, fusion_status=?, response_time_ms=?, "
            "delivered_at=?, last_error=NULL WHERE id=?",
            (STATE_DELIVERED, fusion_status, response_time_ms, time.time(), call_id),
        )
        await self._db.commit()

    async def reschedule(self, call_id: int, last_error: str,
                         fusion_status: Optional[int] = None) -> None:
        """Return a row to 'pending' after a retryable failure."""
        await self._db.execute(
            "UPDATE calls SET state=?, last_error=?, fusion_status=? WHERE id=?",
            (STATE_PENDING, last_error, fusion_status, call_id),
        )
        await self._db.commit()

    async def mark_failed(self, call_id: int, last_error: str,
                          fusion_status: Optional[int] = None) -> None:
        await self._db.execute(
            "UPDATE calls SET state=?, last_error=?, fusion_status=? WHERE id=?",
            (STATE_FAILED, last_error, fusion_status, call_id),
        )
        await self._db.commit()

    async def mark_expired(self, call_id: int,
                           last_error: str = "max delivery age exceeded") -> None:
        await self._db.execute(
            "UPDATE calls SET state=?, last_error=? WHERE id=?",
            (STATE_EXPIRED, last_error, call_id),
        )
        await self._db.commit()

    async def recover_inflight(self) -> int:
        """On startup, return crash-orphaned 'delivering' rows to 'pending'.

        Returns the number of rows recovered.
        """
        cur = await self._db.execute(
            "UPDATE calls SET state=? WHERE state=?",
            (STATE_PENDING, STATE_DELIVERING),
        )
        await self._db.commit()
        return cur.rowcount

    async def get_call(self, call_id: int) -> Optional[Dict[str, Any]]:
        cur = await self._db.execute("SELECT * FROM calls WHERE id=?", (call_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def count_by_state(self, include_test: bool = False) -> Dict[str, int]:
        """Counts grouped by delivery state (excludes test rows by default)."""
        if include_test:
            cur = await self._db.execute(
                "SELECT state, COUNT(*) FROM calls GROUP BY state")
        else:
            cur = await self._db.execute(
                "SELECT state, COUNT(*) FROM calls WHERE is_test=0 GROUP BY state")
        return {row[0]: row[1] for row in await cur.fetchall()}

    async def get_recent_calls(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Retrieve the most recent call records."""
        cursor = await self._db.execute(
            "SELECT * FROM calls ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_calls_page(self, page: int = 1, page_size: int = 20, today_only: bool = True) -> tuple:
        """Retrieve a page of call records.

        Returns (calls_list, total_count, total_pages).
        If today_only, limits to calls from today (local time).
        """
        import time
        offset = (page - 1) * page_size

        if today_only:
            today_start = time.strftime("%Y-%m-%d 00:00:00", time.localtime())
            count_cursor = await self._db.execute(
                "SELECT COUNT(*) FROM calls WHERE timestamp >= ?",
                (today_start,),
            )
            total = (await count_cursor.fetchone())[0]

            cursor = await self._db.execute(
                "SELECT * FROM calls WHERE timestamp >= ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (today_start, page_size, offset),
            )
        else:
            count_cursor = await self._db.execute("SELECT COUNT(*) FROM calls")
            total = (await count_cursor.fetchone())[0]

            cursor = await self._db.execute(
                "SELECT * FROM calls ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (page_size, offset),
            )

        rows = await cursor.fetchall()
        total_pages = max(1, (total + page_size - 1) // page_size)
        return [dict(row) for row in rows], total, total_pages

    async def get_today_stats(self) -> dict:
        """Get success/failed counts for all of today's calls."""
        import time
        today_start = time.strftime("%Y-%m-%d 00:00:00", time.localtime())

        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM calls WHERE timestamp >= ? AND fusion_status >= 200 AND fusion_status < 300",
            (today_start,),
        )
        success = (await cursor.fetchone())[0]

        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM calls WHERE timestamp >= ? AND (fusion_status < 200 OR fusion_status >= 300)",
            (today_start,),
        )
        failed = (await cursor.fetchone())[0]

        return {"success": success, "failed": failed}

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None
            logger.info("Database closed")
