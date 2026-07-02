"""SQLite database for call records.

Stores call history for the dashboard. Uses aiosqlite for async access.
"""

import time
import logging
import aiosqlite
from datetime import datetime, timezone as _tz
from pathlib import Path
from typing import Optional, List, Dict, Any, NamedTuple

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

from .config import DatabaseConfig

logger = logging.getLogger("sipgw.database")


class DuplicateMatch(NamedTuple):
    """#5 SHADOW telemetry: the prior clinical-duplicate row a lookup matched.

    Purely informational (window_seconds is 0 in prod so this is never even
    produced there). Carries the prior row's ``id`` (still the int the caller
    stores in ``duplicate_of``), plus its ``created_at`` epoch and
    ``sip_call_id`` so the audit line can log the inter-page gap and cross-
    reference both pages' Call-IDs. NEVER gates delivery.
    """
    id: int
    created_at: float
    sip_call_id: Optional[str]


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

# #7 Fusion reachability keepalive result. The writer stamps the outcome of a
# bounded, READ-ONLY reachability probe (ok + checked_at epoch + short detail);
# the read-only dashboard reads it for the /health INFORMATIONAL fields. This
# NEVER gates /health's status code — that stays heartbeat-driven.
CREATE_FUSION_CHECK = """
CREATE TABLE IF NOT EXISTS fusion_check (
    name TEXT PRIMARY KEY,
    ok INTEGER NOT NULL,
    checked_at REAL NOT NULL,
    detail TEXT
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

    def __init__(self, db_path: str, timezone: str = "",
                 read_only: bool = False, dry_run: bool = False):
        self.db_path = db_path
        self.timezone = timezone           # #12 day-boundary + display zone ("" = host)
        # #14 two-service split: the decoupled dashboard opens READ-ONLY so it
        # can never write the shared DB. dry_run feeds the prod-DB barrier, which
        # runs on EVERY open (writer AND read-only reader).
        self.read_only = read_only
        self.dry_run = dry_run
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self, read_only: Optional[bool] = None) -> None:
        """Open the connection.

        Writer path (default): apply durability pragmas (WAL), create + migrate.
        Read-only path (#14, dashboard): connect, set busy_timeout + query_only,
        and SKIP every write (no WAL pragma, no CREATE, no migrate) so the reader
        can never mutate the shared DB. ``query_only=ON`` (not ``mode=ro``) so the
        -shm/-wal sidecars can still build while logical writes are blocked.

        The prod-DB barrier runs on EVERY open — including the read-only reader —
        so dry-run/test can never attach to the production database.
        """
        if read_only is None:
            read_only = self.read_only
        self.read_only = read_only

        # §2b prod-DB hard barrier — runs on every open, including read-only.
        from .safety import assert_safe_database_path
        assert_safe_database_path(self.db_path, self.dry_run)

        if read_only:
            # Reader: do NOT create the data dir and do NOT write anything. If the
            # writer hasn't created the DB yet (dashboard raced ahead of the main
            # service at boot), fail fast rather than let aiosqlite.connect() create
            # an empty schema-less file at the prod path — systemd restarts us until
            # the writer is up.
            if not Path(self.db_path).exists():
                raise FileNotFoundError(
                    f"database {self.db_path} does not exist yet; the writer "
                    f"(sipgw.service) must create it first")
            self._db = await aiosqlite.connect(self.db_path)
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("PRAGMA busy_timeout=5000")
            await self._db.execute("PRAGMA query_only=ON")
            logger.info(f"Database opened READ-ONLY at {self.db_path} (query_only=ON)")
            return

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
        await self._db.execute(CREATE_FUSION_CHECK)   # #7 keepalive result row
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

    # ------------------------------------------------------------- #5 dedupe
    async def find_recent_duplicate(
        self, *, area_number: Optional[str], room_number: Optional[str],
        bed_number: Optional[str], purpose: str, is_test: int,
        since_epoch: float, exclude_id: Optional[int] = None,
        match_bed: bool = True, match_purpose: bool = True,
    ) -> Optional["DuplicateMatch"]:
        """#5 SHADOW clinical-dedupe lookup — TEST-ONLY path.

        Find the earliest PRIOR row with the same clinical identity created
        within the window (``created_at >= since_epoch`` — keyed off the numeric
        epoch per #12, NEVER the timestamp string), excluding the just-inserted
        row (``exclude_id``). Returns a :class:`DuplicateMatch` (the row's id
        plus its ``created_at`` epoch and ``sip_call_id`` for the audit line), or
        None when there is no prior duplicate.

        area/room are stored columns and matched in SQL (leading zeros preserved
        by exact string match). bed and purpose are not stored as columns, so
        the row's ``caller_id`` (which encodes the bed) and ``display_name``
        (from which the purpose derives) are re-parsed and compared in Python,
        honoring ``match_bed`` / ``match_purpose``.

        This is NON-suppressing telemetry: in prod ``window_seconds`` is 0 so it
        is never called, and even when it returns a match main.py never skips or
        delays delivery — a real second Code Blue is always sent.
        """
        from .parser import parse_caller_username
        from .lookups import get_call_purpose

        # A page with no parseable area/room has no clinical identity to dedupe on;
        # matching NULL IS NULL would falsely group every malformed page together.
        if area_number is None or room_number is None:
            return None

        sql = ("SELECT id, caller_id, display_name, created_at, sip_call_id "
               "FROM calls "
               "WHERE area_number IS ? AND room_number IS ? AND is_test=? "
               "AND created_at >= ?")
        params: list = [area_number, room_number, is_test, since_epoch]
        if exclude_id is not None:
            sql += " AND id != ?"
            params.append(exclude_id)
        sql += " ORDER BY id ASC"

        cur = await self._db.execute(sql, tuple(params))
        rows = await cur.fetchall()

        want_purpose = (purpose or "").strip().lower()
        for row in rows:
            if match_bed:
                _, _, cand_bed, _ = parse_caller_username(row["caller_id"] or "")
                if (cand_bed or None) != (bed_number or None):
                    continue
            if match_purpose:
                cand_purpose = get_call_purpose(row["display_name"] or "").strip().lower()
                if cand_purpose != want_purpose:
                    continue
            return DuplicateMatch(
                id=row["id"],
                created_at=row["created_at"],
                sip_call_id=row["sip_call_id"],
            )
        return None

    async def record_duplicate_of(self, call_id: int, duplicate_of: int) -> None:
        """#5 SHADOW telemetry: annotate a row with the id of a prior clinical
        duplicate. Purely informational — it does NOT change delivery ``state``
        and NEVER gates, delays, or skips delivery of this page.
        """
        await self._db.execute(
            "UPDATE calls SET duplicate_of=? WHERE id=?", (duplicate_of, call_id))
        await self._db.commit()

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

    async def export_calls(self, today_only: bool = True) -> list:
        """#13-P1: export REAL calls (is_test=0) as row dicts for CSV.

        Mirrors get_calls_page's WHERE clause and ALWAYS appends 'AND is_test=0'
        so dry-run/test rows never leak into an exported file. Returns every
        matching row (no pagination) newest-first.
        """
        if today_only:
            today_start = _day_start_epoch(self.timezone)
            cursor = await self._db.execute(
                "SELECT * FROM calls WHERE created_at >= ? AND is_test=0 "
                "ORDER BY created_at DESC",
                (today_start,),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM calls WHERE is_test=0 ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

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

    # ----------------------------------------------------------- #7 keepalive
    async def write_fusion_check(self, ok: bool, detail: str = "",
                                 name: str = "fusion") -> float:
        """#7 Stamp the result of the writer's Fusion reachability probe.

        Mirrors write_heartbeat's UPSERT (single row per ``name``). ``ok`` is the
        reachability outcome, ``detail`` a short human string (truncated). Returns
        the epoch it stamped. INFORMATIONAL only — never gates delivery or /health.
        """
        now = time.time()
        await self._db.execute(
            "INSERT INTO fusion_check (name, ok, checked_at, detail) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET ok=excluded.ok, "
            "checked_at=excluded.checked_at, detail=excluded.detail",
            (name, 1 if ok else 0, now, (detail or "")[:200]),
        )
        await self._db.commit()
        return now

    async def read_fusion_check(self, name: str = "fusion") -> Optional[Dict[str, Any]]:
        """#7 Read the last stamped Fusion reachability result.

        Returns ``{"ok": bool, "checked_at": float, "detail": str}`` or None if
        never stamped. Safe under the dashboard's ``query_only=ON`` connection
        (SELECT only). Tolerates the table not existing yet (older writer that
        predates #7 hasn't created it): treated as "never checked" (None).
        """
        try:
            cur = await self._db.execute(
                "SELECT ok, checked_at, detail FROM fusion_check WHERE name=?", (name,))
            row = await cur.fetchone()
        except aiosqlite.OperationalError:
            return None
        if not row:
            return None
        return {"ok": bool(row[0]), "checked_at": row[1], "detail": row[2]}

    async def delivery_health_snapshot(self) -> Dict[str, Any]:
        """#7 Read-only INFORMATIONAL metrics for the /health body.

        Returns backlog (pending+delivering REAL rows), the last successful
        delivery epoch, and the last failure epoch + its truncated last_error.
        SELECT-only, so safe under the dashboard's ``query_only=ON`` connection.
        Excludes test rows (is_test=0) so dry-run drills never pollute /health.
        """
        counts = await self.count_by_state(include_test=False)
        backlog = counts.get(STATE_PENDING, 0) + counts.get(STATE_DELIVERING, 0)

        cur = await self._db.execute(
            "SELECT MAX(delivered_at) FROM calls "
            "WHERE state=? AND is_test=0", (STATE_DELIVERED,))
        last_delivered_at = (await cur.fetchone())[0]

        cur = await self._db.execute(
            "SELECT created_at, last_error FROM calls "
            "WHERE state IN (?, ?) AND is_test=0 "
            "ORDER BY created_at DESC LIMIT 1", (STATE_FAILED, STATE_EXPIRED))
        frow = await cur.fetchone()
        last_failed_at = frow[0] if frow else None
        last_error = frow[1] if frow else None

        return {
            "backlog": backlog,
            "last_delivered_at": last_delivered_at,
            "last_failed_at": last_failed_at,
            "last_error": (last_error[:200] if last_error else None),
        }

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None
            logger.info("Database closed")
