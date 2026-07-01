"""#10 state-aware, test-excluding dashboard stats."""

import time

import pytest

from sipgw.database import CallDatabase


async def _db(tmp_path) -> CallDatabase:
    db = CallDatabase(str(tmp_path / "s.db"))
    await db.initialize()
    return db


async def _pending(db, cid, is_test=0):
    return await db.create_pending_call(
        caller_id=cid, display_name="Code Blue", area_number="730",
        area_name="1st Floor... E.D...", room_number="201",
        tts_string="Attention! Code Blue! ...", is_test=is_test)


class TestStateAwareStats:
    @pytest.mark.asyncio
    async def test_stats_by_state_and_excludes_test(self, tmp_path):
        db = await _db(tmp_path)
        d = await _pending(db, "d"); await db.mark_attempting(d); await db.mark_delivered(d, 200, 1.0)
        f = await _pending(db, "f"); await db.mark_attempting(f); await db.mark_failed(f, "err", -1)
        e = await _pending(db, "e"); await db.mark_expired(e)
        await _pending(db, "p")                                   # stays pending
        t = await _pending(db, "t", is_test=1); await db.mark_attempting(t); await db.mark_delivered(t, 200, 1.0)

        stats = await db.get_today_stats()
        assert stats["success"] == 1          # delivered real only (test excluded)
        assert stats["failed"] == 2           # failed + expired
        assert stats["pending"] == 1
        assert stats["expired"] == 1

        calls, total, _pages = await db.get_calls_page(today_only=True)
        assert total == 4                     # is_test row excluded from the page
        assert all(c["is_test"] == 0 for c in calls)
        await db.close()

    @pytest.mark.asyncio
    async def test_legacy_rows_classified_by_fusion_status(self, tmp_path):
        db = await _db(tmp_path)
        now = time.time()
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        await db._db.execute(
            "INSERT INTO calls (timestamp,caller_id,created_at,state,fusion_status,is_test) "
            "VALUES (?,?,?,?,?,0)", (ts, "lok", now, "legacy", 200))
        await db._db.execute(
            "INSERT INTO calls (timestamp,caller_id,created_at,state,fusion_status,is_test) "
            "VALUES (?,?,?,?,?,0)", (ts, "lbad", now, "legacy", 500))
        await db._db.execute(
            "INSERT INTO calls (timestamp,caller_id,created_at,state,fusion_status,is_test) "
            "VALUES (?,?,?,?,?,0)", (ts, "lnull", now, "legacy", None))
        await db._db.commit()

        stats = await db.get_today_stats()
        assert stats["success"] == 1          # legacy 2xx
        assert stats["failed"] == 2           # legacy 5xx + legacy NULL
        await db.close()
