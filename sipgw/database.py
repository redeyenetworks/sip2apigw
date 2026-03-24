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


class CallDatabase:
    """Async SQLite database for storing call records."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        """Open connection and create tables."""
        db_dir = Path(self.db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute(CREATE_TABLE)
        await self._db.execute(CREATE_INDEX)
        await self._db.commit()
        logger.info(f"Database initialized at {self.db_path}")

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
    ) -> int:
        """Insert a call record. Returns the row ID."""
        now = time.time()
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))

        cursor = await self._db.execute(
            """INSERT INTO calls
               (timestamp, caller_id, display_name, area_number, area_name,
                room_number, tts_string, fusion_status, response_time_ms, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                timestamp,
                caller_id,
                display_name,
                area_number,
                area_name,
                room_number,
                tts_string,
                fusion_status,
                response_time_ms,
                now,
            ),
        )
        await self._db.commit()
        logger.info(f"Recorded call {cursor.lastrowid}: {caller_id} -> {tts_string}")
        return cursor.lastrowid

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

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None
            logger.info("Database closed")
