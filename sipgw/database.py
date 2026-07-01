"""SQLite database for call records.

Stores call history for the dashboard. Uses aiosqlite for async access.
"""

import time
import logging
import aiosqlite
from datetime import datetime, timezone as _tz
from pathlib import Path
from typing import Optional, List, Dict, Any

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

from .config import DatabaseConfig

logger = logging.getLogger("sipgw.database")


# --- #12 canonical time helpers -------------------------------------------
# Stored `timestamp` is canonical UTC RFC3339 millis-Z. Bucketing/ordering keys
# off the numeric `created_at` epoch (uniform across legacy + new rows), never
# the string — the only correct way to classify mixed old-local/new-UTC rows.

def _utc_rfc3339(epoch: float) -> str:
    dt = datetime.fromtimestamp(epoch, tz=_tz.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


_HOST_TZ_SENTINELS = {"", "host", "local", "system"}


def _resolve_tz(tzname: str):
    """Resolve the display / day-boundary timezone.

    Empty or 'host'/'local'/'system' -> the HOST's configured local timezone
    (we ask the host rather than assuming a zone; hosts are expected to be UTC).
    An explicit IANA name (e.g. 'America/New_York') overrides per-install.
    """
    name = (tzname or "").strip()
    if name.lower() not in _HOST_TZ_SENTINELS and ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo   # host local


def _day_start_epoch(tzname: str) -> float:
    """Epoch of local wall-clock midnight today in ``tzname`` (DST-correct)."""
    tz = _resolve_tz(tzname)
    now = datetime.now(tz)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def display_local(epoch: float, tzname: str) -> str:
    """Render an epoch as local wall-clock for humans (dashboard/CSV)."""
    if epoch is None:
        return ""
    return datetime.fromtimestamp(epoch, _resolve_tz(tzname)).strftime("%Y-%m-%d %H:%M:%S")

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

# #7 liveness heartbeat. The writer process stamps beat_at (epoch); the (soon
# decoupled, #14) dashboard reads it and reports /health stale if it ages out.
CREATE_HEARTBEAT = """
CREATE TABLE IF NOT EXISTS heartbeat (
    name TEXT PRIMARY KEY,
    beat_at REAL NOT NULL
)
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

    def __init__(self, db_path: str, timezone: str = ""):
        self.db_path = db_path
        self.timezone = timezone           # #12 day-boundary + display zone ("" = host)
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
        await self._db.execute(CREATE_HEARTBEAT)
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
        timestamp = _utc_rfc3339(now)
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
        timestamp = _utc_rfc3339(now)
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

        # Live dashboard shows only REAL calls (is_test=0).
        if today_only:
            today_start = _day_start_epoch(self.timezone)
            count_cursor = await self._db.execute(
                "SELECT COUNT(*) FROM calls WHERE created_at >= ? AND is_test=0",
                (today_start,),
            )
            total = (await count_cursor.fetchone())[0]

            cursor = await self._db.execute(
                "SELECT * FROM calls WHERE created_at >= ? AND is_test=0 "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (today_start, page_size, offset),
            )
        else:
            count_cursor = await self._db.execute(
                "SELECT COUNT(*) FROM calls WHERE is_test=0")
            total = (await count_cursor.fetchone())[0]

            cursor = await self._db.execute(
                "SELECT * FROM calls WHERE is_test=0 "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (page_size, offset),
            )

        rows = await cursor.fetchall()
        total_pages = max(1, (total + page_size - 1) // page_size)
        return [dict(row) for row in rows], total, total_pages

    async def get_today_stats(self) -> dict:
        """State-aware counts for today's REAL calls (is_test=0).

        success = delivered (+ legacy rows with a 2xx fusion_status)
        failed  = failed + expired (+ legacy rows with a non-2xx fusion_status)
        pending = pending + delivering
        Legacy rows predate the state machine, so they are classified by their
        stored fusion_status for continuity across the cutover boundary.
        """
        today_start = _day_start_epoch(self.timezone)

        cur = await self._db.execute(
            "SELECT state, COUNT(*) FROM calls "
            "WHERE created_at >= ? AND is_test=0 GROUP BY state",
            (today_start,),
        )
        by_state = {row[0]: row[1] for row in await cur.fetchall()}

        cur = await self._db.execute(
            "SELECT COUNT(*) FROM calls WHERE created_at >= ? AND is_test=0 "
            "AND state='legacy' AND fusion_status >= 200 AND fusion_status < 300",
            (today_start,),
        )
        legacy_ok = (await cur.fetchone())[0]
        cur = await self._db.execute(
            "SELECT COUNT(*) FROM calls WHERE created_at >= ? AND is_test=0 "
            "AND state='legacy' AND (fusion_status IS NULL OR fusion_status < 200 "
            "OR fusion_status >= 300)",
            (today_start,),
        )
        legacy_bad = (await cur.fetchone())[0]

        success = by_state.get(STATE_DELIVERED, 0) + legacy_ok
        failed = by_state.get(STATE_FAILED, 0) + by_state.get(STATE_EXPIRED, 0) + legacy_bad
        pending = by_state.get(STATE_PENDING, 0) + by_state.get(STATE_DELIVERING, 0)

        return {
            "success": success,
            "failed": failed,
            "pending": pending,
            "delivered": by_state.get(STATE_DELIVERED, 0),
            "expired": by_state.get(STATE_EXPIRED, 0),
            "by_state": by_state,
        }

    async def write_heartbeat(self, name: str = "writer") -> float:
        """Stamp the writer's liveness heartbeat (epoch). Returns the beat time."""
        now = time.time()
        await self._db.execute(
            "INSERT INTO heartbeat (name, beat_at) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET beat_at=excluded.beat_at",
            (name, now),
        )
        await self._db.commit()
        return now

    async def read_heartbeat(self, name: str = "writer") -> Optional[float]:
        """Read the last heartbeat epoch for ``name`` (None if never stamped)."""
        cur = await self._db.execute(
            "SELECT beat_at FROM heartbeat WHERE name=?", (name,))
        row = await cur.fetchone()
        return row[0] if row else None

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None
            logger.info("Database closed")
