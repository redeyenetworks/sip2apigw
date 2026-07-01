"""#12 RFC3339-UTC timestamps: writer format, created_at bucketing (B4), DST."""

import glob
import re
import shutil
import time
from datetime import datetime, timezone

import pytest

from sipgw.database import (
    CallDatabase, _utc_rfc3339, _day_start_epoch, display_local,
)

_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
DAY = 86400.0


async def _db(tmp_path, tz="America/New_York") -> CallDatabase:
    db = CallDatabase(str(tmp_path / "t.db"), timezone=tz)
    await db.initialize()
    return db


async def _pending(db, cid, created_offset=0.0, is_test=0):
    rid = await db.create_pending_call(
        caller_id=cid, display_name="Code Blue", area_number="730",
        area_name="1st Floor... E.D...", room_number="201",
        tts_string="Attention! Code Blue! ...", is_test=is_test)
    if created_offset:
        await db._db.execute("UPDATE calls SET created_at=created_at+? WHERE id=?",
                             (created_offset, rid))
        await db._db.commit()
    return rid


class TestWriterFormat:
    @pytest.mark.asyncio
    async def test_utc_format_and_coherence(self, tmp_path):
        db = await _db(tmp_path)
        cid = await _pending(db, "a")
        row = await db.get_call(cid)
        assert _UTC_RE.match(row["timestamp"]), row["timestamp"]
        epoch = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).timestamp()
        assert abs(epoch - row["created_at"]) < 0.002   # ms-truncation only
        await db.close()

    def test_utc_helper_is_utc_not_local(self):
        # A known instant: 2026-07-01 21:02:44 UTC.
        e = datetime(2026, 7, 1, 21, 2, 44, 301000, tzinfo=timezone.utc).timestamp()
        assert _utc_rfc3339(e) == "2026-07-01T21:02:44.301Z"


class TestBucketingB4:
    @pytest.mark.asyncio
    async def test_mixed_old_and_new_format_bucket_by_created_at(self, tmp_path):
        db = await _db(tmp_path)
        now = time.time()
        # (a) LEGACY local-format row, created today.
        await db._db.execute(
            "INSERT INTO calls (timestamp,caller_id,created_at,state,fusion_status,is_test) "
            "VALUES (?,?,?,?,?,0)",
            (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)), "legacy_today",
             now, "legacy", 200))
        # (b) NEW UTC-format row, created today (via the writer).
        b = await _pending(db, "new_today"); await db.mark_attempting(b); await db.mark_delivered(b, 200, 1.0)
        # (c) A row from 2 days ago (NOT today) — must be excluded.
        await db._db.execute(
            "INSERT INTO calls (timestamp,caller_id,created_at,state,fusion_status,is_test) "
            "VALUES (?,?,?,?,?,0)",
            (_utc_rfc3339(now - 2 * DAY), "old_2d", now - 2 * DAY, "delivered", 200))
        await db._db.commit()

        stats = await db.get_today_stats()
        assert stats["success"] == 2           # legacy(200 today) + new delivered today
        calls, total, _p = await db.get_calls_page(today_only=True)
        ids = {c["caller_id"] for c in calls}
        assert total == 2 and ids == {"legacy_today", "new_today"}
        await db.close()


class TestZoneAndDst:
    def test_day_start_is_zone_aware_not_process_tz(self):
        # NY midnight and Tokyo midnight are different UTC instants.
        assert _day_start_epoch("America/New_York") != _day_start_epoch("Asia/Tokyo")

    def test_display_local_dst_correct(self):
        # Winter (EST, UTC-5): 12:00Z -> 07:00 local.
        est = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        assert display_local(est, "America/New_York").endswith("07:00:00")
        # Summer (EDT, UTC-4): 12:00Z -> 08:00 local.
        edt = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        assert display_local(edt, "America/New_York").endswith("08:00:00")


class TestProdCopyDrillDelta:
    @pytest.mark.asyncio
    async def test_migrated_prodcopy_today_count_rises_by_two(self, tmp_path):
        fixtures = glob.glob("/home/sipgw/sipgw-work/var/backups/calls-prodcopy-*.db")
        if not fixtures:
            pytest.skip("no prod-copy fixture present")
        scratch = str(tmp_path / "prodcopy.db")
        shutil.copy(sorted(fixtures)[-1], scratch)
        db = CallDatabase(scratch, timezone="America/New_York")
        await db.initialize()                  # migrates 301-row old schema
        before = await db.get_today_stats()
        # Insert two fresh delivered rows (created now = today).
        for cid in ("fresh1", "fresh2"):
            r = await _pending(db, cid)
            await db.mark_attempting(r); await db.mark_delivered(r, 200, 1.0)
        after = await db.get_today_stats()
        # Delta test (not "none of 301 are today" — the fixture DOES hold today rows).
        assert after["success"] - before["success"] == 2
        await db.close()
