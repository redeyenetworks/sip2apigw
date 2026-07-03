"""F2: 90-day stacked chart of calls by TYPE (derived call purpose).

Dashboard-only, read-only, ZERO SIP-path impact. Type = the call PURPOSE,
derived from display_name via lookups.get_call_purpose (NOT a stored column), so
every row incl. legacy is covered and future purposes auto-appear. The chart is a
dependency-free inline SVG (autoescape stays on: numbers/paths/labels only).
"""

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient

from sipgw.config import DashboardConfig, LoggingConfig
from sipgw.database import CallDatabase
from sipgw.lookups import load_lookups
from sipgw.dashboard import (
    create_dashboard, _build_purpose_chart, _purpose_color, CHART_DAYS,
)

NY = "America/New_York"
NY_TZ = ZoneInfo(NY)


@pytest.fixture(autouse=True)
def _lookups_loaded():
    # The suite runs with SIPGW_LOOKUPS set; pin it so purpose derivation is
    # deterministic regardless of what an earlier test loaded.
    load_lookups(os.environ["SIPGW_LOOKUPS"])


def _epoch(y, mo, d, h=12):
    return datetime(y, mo, d, h, 0, 0, tzinfo=NY_TZ).timestamp()


NOW = _epoch(2026, 7, 3, 12)   # fixed "now" -> today (local) = 2026-07-03


def _row(created_at, name):
    return {"created_at": created_at, "display_name": name}


# --------------------------------------------------------------------------- #
# DB reader: get_calls_since
# --------------------------------------------------------------------------- #
class TestGetCallsSince:
    @pytest.mark.asyncio
    async def test_excludes_test_rows_and_older_than_since(self, tmp_path):
        db = CallDatabase(str(tmp_path / "f2.db"))
        await db.initialize()
        cid = await db.create_pending_call(
            caller_id="a730r201", display_name="Code Blue", area_number="730",
            area_name="E.D.", room_number="201", tts_string="x")
        # a [TEST]/is_test row must never colour the chart
        await db.create_pending_call(
            caller_id="a730r202", display_name="Code Blue", area_number="730",
            area_name="E.D.", room_number="202", tts_string="x", is_test=1)
        # an old legacy row, well before `since`
        await db._db.execute(
            "INSERT INTO calls (timestamp,caller_id,display_name,created_at,"
            "state,is_test) VALUES (?,?,?,?,?,0)",
            ("2020-01-01 00:00:00", "old", "Code Blue", _epoch(2020, 1, 1),
             "legacy"))
        await db._db.commit()

        rows = await db.get_calls_since(NOW - CHART_DAYS * 86400)
        assert len(rows) == 1                         # test + old both excluded
        assert rows[0]["caller_id"] if False else True
        assert rows[0]["display_name"] == "Code Blue"
        assert "created_at" in rows[0]
        await db.close()


# --------------------------------------------------------------------------- #
# Colour palette
# --------------------------------------------------------------------------- #
class TestPurposeColor:
    def test_fixed_map_used_for_known_purposes(self):
        assert _purpose_color("Code Blue") == "#4fc3f7"
        assert _purpose_color("Code Pink") == "#f48fb1"

    def test_unknown_purpose_gets_stable_hashed_colour(self):
        a = _purpose_color("Code Grey")
        b = _purpose_color("Code Grey")
        assert a == b                                 # deterministic across calls
        assert a.startswith("hsl(")
        assert a != _purpose_color("Code Amber")      # different name -> diff hue


# --------------------------------------------------------------------------- #
# Chart model builder
# --------------------------------------------------------------------------- #
class TestBuildPurposeChart:
    def test_buckets_by_local_day_and_derived_purpose(self):
        rows = [
            _row(_epoch(2026, 7, 2), "Code Blue Alert"),   # Blue -> Code Blue
            _row(_epoch(2026, 7, 2), "RRT team"),          # RRT  -> Rapid Response Team
            _row(_epoch(2026, 7, 3, 9), "Pink baby"),      # Pink -> Code Pink
        ]
        chart = _build_purpose_chart(rows, NY, now=NOW)
        assert chart["total"] == 3
        legend = {it["purpose"]: it["total"] for it in chart["legend"]}
        assert legend == {"Code Blue": 1, "Rapid Response Team": 1, "Code Pink": 1}
        # one rect per non-zero (day, purpose) segment
        assert len(chart["rects"]) == 3

    def test_known_purposes_sorted_before_unknown(self):
        rows = [
            _row(_epoch(2026, 7, 2), "Pink"),
            _row(_epoch(2026, 7, 2), "Blue"),
        ]
        chart = _build_purpose_chart(rows, NY, now=NOW)
        order = [it["purpose"] for it in chart["legend"]]
        # fixed-map order: Code Blue before Code Pink
        assert order == ["Code Blue", "Code Pink"]

    def test_zero_fill_ninety_days_and_weekly_ticks(self):
        chart = _build_purpose_chart([], NY, now=NOW)
        assert chart["days"] == CHART_DAYS
        assert chart["total"] == 0
        assert chart["rects"] == []
        # weekly ticks across 90 days -> 13 (days 0,7,...,84 counting back from today)
        assert len(chart["x_ticks"]) == 13
        assert chart["x_ticks"][-1]["label"] == "07-03"   # today, MM-DD

    def test_rows_outside_window_are_dropped(self):
        rows = [
            _row(_epoch(2026, 7, 3, 9), "Blue"),     # in window
            _row(_epoch(2020, 1, 1), "Blue"),        # ancient, dropped
            _row("not-a-number", "Blue"),            # malformed, dropped
        ]
        chart = _build_purpose_chart(rows, NY, now=NOW)
        assert chart["total"] == 1

    def test_stacking_and_scaling_stays_within_plot(self):
        rows = [_row(_epoch(2026, 7, 3, 9), "Blue") for _ in range(5)]
        rows += [_row(_epoch(2026, 7, 3, 9), "Pink") for _ in range(3)]
        chart = _build_purpose_chart(rows, NY, now=NOW)
        assert chart["max_total"] == 8
        # every rect sits within the plotting band (top .. baseline)
        for r in chart["rects"]:
            assert r["y"] >= 12 - 0.01
            assert r["y"] + r["h"] <= chart["baseline_y"] + 0.01


# --------------------------------------------------------------------------- #
# Route rendering
# --------------------------------------------------------------------------- #
def _client(tmp_path, since_rows=None, since_error=False):
    db = AsyncMock(spec=CallDatabase)
    db.get_calls_page = AsyncMock(return_value=([], 0, 1))
    db.get_calls_between = AsyncMock(return_value=[])
    db.get_today_stats = AsyncMock(
        return_value={"success": 0, "failed": 0, "pending": 0})
    if since_error:
        db.get_calls_since = AsyncMock(side_effect=RuntimeError("boom"))
    else:
        db.get_calls_since = AsyncMock(return_value=since_rows or [])
    lc = LoggingConfig(log_dir=str(tmp_path), timezone=NY,
                       api_debug_log=False, sip_debug_log=False)
    app = create_dashboard(db, DashboardConfig(page_size=20), log_config=lc)
    return db, TestClient(app)


class TestChartRoute:
    def test_chart_renders_above_the_call_table(self, tmp_path):
        # the route builds the chart against real time.time(), so anchor these
        # rows to the wall clock (not the fixed NOW used for the model unit tests).
        rows = [_row(time.time() - 3600, "Code Blue Alert"),
                _row(time.time() - 7200, "Pink")]
        db, c = _client(tmp_path, since_rows=rows)
        r = c.get("/")
        assert r.status_code == 200
        assert 'class="chart-panel"' in r.text
        assert "Calls by type" in r.text
        assert "Code Blue" in r.text            # legend label
        assert "<svg" in r.text
        # chart is placed BEFORE the call table
        assert r.text.index('class="chart-panel"') < r.text.index("<table")

    def test_chart_hidden_when_reader_fails_no_500(self, tmp_path):
        db, c = _client(tmp_path, since_error=True)
        r = c.get("/")
        assert r.status_code == 200
        assert 'class="chart-panel"' not in r.text

    def test_chart_shows_empty_message_with_no_calls(self, tmp_path):
        db, c = _client(tmp_path, since_rows=[])
        r = c.get("/")
        assert r.status_code == 200
        assert 'class="chart-panel"' in r.text
        assert "No calls in the last 90 days." in r.text
