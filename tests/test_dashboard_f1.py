"""F1: the date picker drives the CALL TABLE too (not just the log viewer).

Dashboard-only, read-only, zero SIP-path impact. Selecting a date filters BOTH
the call table and the logs to that LOCAL day (display zone = logging.timezone),
reusing db.get_calls_between + _local_day_window as the single source of truth.
"""

import tarfile
from datetime import datetime, timezone
from pathlib import Path

from unittest.mock import AsyncMock
from fastapi.testclient import TestClient

from sipgw.config import DashboardConfig, LoggingConfig
from sipgw.database import CallDatabase
from sipgw.dashboard import create_dashboard, _local_day_window

NY = "America/New_York"


def _row(i, created_at, name="Code Blue"):
    return {
        "id": i, "caller_id": f"a730r{i:03d}", "display_name": name,
        "area_number": 730, "area_name": "1st Floor. E.D.", "room_number": i,
        "tts_string": f"Code Blue! Room {i}.", "fusion_status": 200,
        "response_time_ms": 100.0, "created_at": created_at,
        "state": "delivered", "attempts": 1, "last_error": None, "is_test": 0,
    }


def _write_rotated(log_dir, base, utc_date, content):
    member = f"{base}.{utc_date}"
    src = Path(log_dir) / member
    src.write_text(content)
    with tarfile.open(Path(log_dir) / f"{base}.{utc_date}.tgz", "w:gz") as t:
        t.add(str(src), arcname=member)
    src.unlink()


def _seed_logs(tmp_path):
    """Two UTC files -> local days 2026-07-01, 07-02, 07-03 in NY."""
    _write_rotated(str(tmp_path), "sipgw.log", "2026-07-02",
                   "2026-07-02T12:00:00.000Z DAYTWO\n")
    _write_rotated(str(tmp_path), "sipgw.log", "2026-07-03",
                   "2026-07-03T12:00:00.000Z DAYTHREE\n")


def _make(tmp_path, between_rows, page_rows=([], 0, 1)):
    db = AsyncMock(spec=CallDatabase)
    db.get_calls_page = AsyncMock(return_value=page_rows)
    db.get_calls_between = AsyncMock(return_value=between_rows)
    db.get_today_stats = AsyncMock(
        return_value={"success": 0, "failed": 0, "pending": 0})
    lc = LoggingConfig(log_dir=str(tmp_path), timezone=NY,
                       api_debug_log=False, sip_debug_log=False)
    app = create_dashboard(db, DashboardConfig(page_size=20), log_config=lc)
    return db, TestClient(app)


class TestF1CallTable:
    def test_selected_date_queries_that_local_day_window(self, tmp_path):
        _seed_logs(tmp_path)
        db, c = _make(tmp_path, between_rows=[_row(1, 1751470200.0)])
        r = c.get("/?logdate=2026-07-02")
        assert r.status_code == 200
        start, end = _local_day_window("2026-07-02", NY)
        # inclusive end -> we pass a hair under the next-day boundary.
        args = db.get_calls_between.await_args.args
        assert args[0] == start
        assert start < args[1] < end
        assert "a730r001" in r.text          # the between-row is in the table

    def test_selected_date_does_not_use_today_only_page(self, tmp_path):
        _seed_logs(tmp_path)
        db, c = _make(tmp_path, between_rows=[_row(1, 1751470200.0)])
        c.get("/?logdate=2026-07-02")
        db.get_calls_page.assert_not_awaited()

    def test_day_rows_paginate_in_python(self, tmp_path):
        _seed_logs(tmp_path)
        rows = [_row(i, 1751470200.0 + i) for i in range(50)]
        db, c = _make(tmp_path, between_rows=rows)
        r1 = c.get("/?logdate=2026-07-02&page=1")
        assert "Page 1 of 3" in r1.text
        assert "a730r000" in r1.text and "a730r019" in r1.text
        assert "a730r020" not in r1.text
        r2 = c.get("/?logdate=2026-07-02&page=2")
        assert "Page 2 of 3" in r2.text
        assert "a730r020" in r2.text and "a730r039" in r2.text
        assert "a730r000" not in r2.text

    def test_pagination_links_preserve_logdate(self, tmp_path):
        _seed_logs(tmp_path)
        rows = [_row(i, 1751470200.0 + i) for i in range(50)]
        db, c = _make(tmp_path, between_rows=rows)
        r = c.get("/?logdate=2026-07-02&page=2")
        assert "logdate=2026-07-02" in r.text
        # the Next/Prev links must keep the day so paging stays on that day
        assert "page=3" in r.text and "page=1" in r.text

    def test_header_names_the_day_being_shown(self, tmp_path):
        _seed_logs(tmp_path)
        db, c = _make(tmp_path, between_rows=[_row(1, 1751470200.0)])
        r = c.get("/?logdate=2026-07-02")
        assert "2026-07-02" in r.text

    def test_default_no_logs_falls_back_to_today_page(self, tmp_path):
        # empty dir -> no available dates -> preserve the original today-only path.
        db, c = _make(tmp_path, between_rows=[], page_rows=([_row(9, 1751470200.0)], 1, 1))
        r = c.get("/")
        assert r.status_code == 200
        db.get_calls_page.assert_awaited_once()
        db.get_calls_between.assert_not_awaited()
        assert "a730r009" in r.text

    def test_default_live_day_uses_between(self, tmp_path):
        # with logs present, default view = latest available day (today), table via between.
        _seed_logs(tmp_path)
        db, c = _make(tmp_path, between_rows=[_row(3, 1751556600.0)])
        r = c.get("/")
        assert r.status_code == 200
        db.get_calls_between.assert_awaited()
        start, end = _local_day_window("2026-07-03", NY)
        args = db.get_calls_between.await_args.args
        assert args[0] == start

    def test_between_failure_falls_back_no_500(self, tmp_path):
        _seed_logs(tmp_path)
        db, c = _make(tmp_path, between_rows=[])
        db.get_calls_between = AsyncMock(side_effect=RuntimeError("boom"))
        r = c.get("/?logdate=2026-07-02")
        assert r.status_code == 200
        db.get_calls_page.assert_awaited()
