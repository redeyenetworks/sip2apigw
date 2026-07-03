"""#13: timezone-aware date-picker log viewer (local day across UTC rotated files)."""

import tarfile
from datetime import datetime, timezone
from pathlib import Path

from unittest.mock import AsyncMock
from fastapi.testclient import TestClient

from sipgw.config import DashboardConfig, LoggingConfig
from sipgw.database import CallDatabase
from sipgw.dashboard import (
    create_dashboard, _line_epoch, _local_day_window,
    _available_log_days, _read_log_for_day,
)

NY = "America/New_York"


def _write_rotated(log_dir, base, utc_date, content):
    """Mirror CompressingTimedRotatingFileHandler: tar.gz named by the UTC date."""
    member = f"{base}.{utc_date}"
    src = Path(log_dir) / member
    src.write_text(content)
    with tarfile.open(Path(log_dir) / f"{base}.{utc_date}.tgz", "w:gz") as t:
        t.add(str(src), arcname=member)
    src.unlink()


class TestPrimitives:
    def test_line_epoch_utc_and_legacy(self):
        z = _line_epoch("2026-07-03T04:05:06.789Z hello")
        legacy = _line_epoch("2026-07-03 04:05:06 hello")
        expect = datetime(2026, 7, 3, 4, 5, 6, tzinfo=timezone.utc).timestamp()
        assert z == expect and legacy == expect
        assert _line_epoch("   continuation line, no stamp") is None

    def test_local_day_window_edt(self):
        # 2026-07-02 in New York (EDT, UTC-4) = 04:00Z .. next 04:00Z.
        start, end = _local_day_window("2026-07-02", NY)
        assert start == datetime(2026, 7, 2, 4, tzinfo=timezone.utc).timestamp()
        assert end == datetime(2026, 7, 3, 4, tzinfo=timezone.utc).timestamp()

    def test_local_day_window_dst_spring_forward(self):
        # 2026-03-08 US spring-forward: the local day is only 23h long.
        start, end = _local_day_window("2026-03-08", NY)
        assert round((end - start) / 3600) == 23


class TestCrossFileWindowing:
    def test_local_day_spans_two_utc_files(self, tmp_path):
        d = str(tmp_path)
        _write_rotated(d, "sipgw.log", "2026-07-02",
                       "2026-07-02T03:00:00.000Z PREV_LOCAL_DAY\n"
                       "2026-07-02T12:00:00.000Z IN_WINDOW_A\n"
                       "  continuation-of-A\n")
        _write_rotated(d, "sipgw.log", "2026-07-03",
                       "2026-07-03T02:00:00.000Z IN_WINDOW_B\n"
                       "2026-07-03T10:00:00.000Z NEXT_LOCAL_DAY\n")
        lines = _read_log_for_day(d, "sipgw.log", "2026-07-02", NY)
        body = "\n".join(lines)
        assert "IN_WINDOW_A" in body            # from the 07-02 UTC file
        assert "continuation-of-A" in body      # multi-line entry kept as a unit
        assert "IN_WINDOW_B" in body            # from the 07-03 UTC file (!)
        assert "PREV_LOCAL_DAY" not in body     # before the local-day window
        assert "NEXT_LOCAL_DAY" not in body     # after the local-day window

    def test_available_days_are_local(self, tmp_path):
        d = str(tmp_path)
        _write_rotated(d, "sipgw.log", "2026-07-02", "2026-07-02T12:00:00.000Z x\n")
        _write_rotated(d, "sipgw.log", "2026-07-03", "2026-07-03T02:00:00.000Z y\n")
        # In NY these two UTC files touch local days 07-01, 07-02, 07-03.
        assert _available_log_days(d, ["sipgw.log"], NY) == ["2026-07-01", "2026-07-02", "2026-07-03"]


def _client(tmp_path, tz=NY):
    db = AsyncMock(spec=CallDatabase)
    db.get_calls_page = AsyncMock(return_value=([], 0, 1))
    db.get_today_stats = AsyncMock(return_value={"success": 0, "failed": 0, "pending": 0})
    lc = LoggingConfig(log_dir=str(tmp_path), timezone=tz,
                       api_debug_log=False, sip_debug_log=False)
    return TestClient(create_dashboard(db, DashboardConfig(), log_config=lc))


class TestRoute:
    def test_picker_labels_zone_and_windows(self, tmp_path):
        # Use dates far in the past so they are unambiguously historical (the
        # default view is now "today", so is_live depends on the real clock).
        _write_rotated(str(tmp_path), "sipgw.log", "2020-01-01",
                       "2020-01-01T12:00:00.000Z DAYONE\n")
        _write_rotated(str(tmp_path), "sipgw.log", "2020-01-02",
                       "2020-01-02T12:00:00.000Z DAYTWO\n")
        c = _client(tmp_path)
        r = c.get("/")
        assert r.status_code == 200
        assert 'type="date"' in r.text and "America/New_York" in r.text   # zone labelled
        # a definitely-historical local day (EST window excludes the next UTC day)
        r2 = c.get("/?logdate=2020-01-01")
        assert "DAYONE" in r2.text and "DAYTWO" not in r2.text and "(historical)" in r2.text
        # invalid date -> safe fallback, no 500
        assert c.get("/?logdate=1999-13-99").status_code == 200

    def test_defaults_to_today_not_last_logged_day(self, tmp_path):
        # The only logs are from 2020, but the picker must default to TODAY
        # (the current date), not the last day that happens to have logs.
        import datetime
        from sipgw.dashboard import _resolve_tz
        today = datetime.datetime.now(_resolve_tz(NY)).strftime("%Y-%m-%d")
        _write_rotated(str(tmp_path), "sipgw.log", "2020-01-01",
                       "2020-01-01T12:00:00.000Z OLD\n")
        r = _client(tmp_path).get("/")
        assert ('value="%s"' % today) in r.text     # picker input defaults to today
        assert "Today" in r.text                      # live badge, not the 2020 date
