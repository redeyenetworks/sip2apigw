"""F3: group the day-summary into a "Today view" + real last-call lookback.

Dashboard-only, read-only, ZERO SIP-path impact.

F3a — the stat cards (Calls / Successful / Failed / Pending + a new
"Suppressed (dup)" card fed by #5 enforcement's state='duplicate' rows) live in a
labelled <section class="today-view"> whose header is "Today" when live else the
selected date, and whose numbers FOLLOW the selected day.

F3b — "Last call from Rauland" is its OWN banner (outside the stat grid), driven
by the durable db.get_last_call() (most recent is_test=0 row), rendered client-
side in the viewer's browser TZ (data-epoch) with a server display_local no-JS
fallback + a relative age, or "No calls on record." when empty.
"""

import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient

from sipgw.config import DashboardConfig, LoggingConfig
from sipgw.database import CallDatabase, display_local
from sipgw.dashboard import create_dashboard, _stats_from_rows, _relative_age

NY = "America/New_York"

# Unambiguously historical (never == "today") -> is_live deterministically False.
HIST = "2020-06-15"


def _row(i, created_at, state="delivered", name="Code Blue", fusion_status=200):
    return {
        "id": i, "caller_id": f"a730r{i:03d}", "display_name": name,
        "area_number": 730, "area_name": "1st Floor. E.D.", "room_number": i,
        "tts_string": f"Code Blue! Room {i}.", "fusion_status": fusion_status,
        "response_time_ms": 100.0, "created_at": created_at,
        "state": state, "attempts": 1, "last_error": None, "is_test": 0,
    }


def _client(tmp_path, *, today_stats=None, between_rows=None,
            page_rows=([], 0, 1), last_call=None):
    db = AsyncMock(spec=CallDatabase)
    db.get_calls_page = AsyncMock(return_value=page_rows)
    db.get_calls_between = AsyncMock(return_value=(between_rows or []))
    db.get_today_stats = AsyncMock(return_value=(today_stats or {
        "success": 0, "failed": 0, "pending": 0, "suppressed": 0}))
    db.get_last_call = AsyncMock(return_value=last_call)
    lc = LoggingConfig(log_dir=str(tmp_path), timezone=NY,
                       api_debug_log=False, sip_debug_log=False)
    app = create_dashboard(db, DashboardConfig(page_size=20), log_config=lc)
    return db, TestClient(app)


# --------------------------------------------------------------------------- #
# F3a — Today view section + suppressed card
# --------------------------------------------------------------------------- #
class TestTodayView:
    def test_today_header_and_suppressed_count_render(self, tmp_path):
        db, c = _client(tmp_path, today_stats={
            "success": 12, "failed": 1, "pending": 2, "suppressed": 4})
        r = c.get("/")
        assert r.status_code == 200
        html = r.text
        # The cards are grouped in a labelled Today section...
        assert 'class="today-view"' in html
        # ...whose header reads "Today" on the live view.
        assert 'class="today-title">Today' in html
        # ...and the new Suppressed (dup) card shows the count (tied to its card
        # by the card's distinctive value colour).
        assert "Suppressed (dup)" in html
        assert '#9c8cff;">4<' in html

    def test_suppressed_defaults_to_zero_when_absent(self, tmp_path):
        # A stats dict without "suppressed" (older shape) must not 500; card -> 0.
        db, c = _client(tmp_path, today_stats={
            "success": 1, "failed": 0, "pending": 0})
        r = c.get("/")
        assert r.status_code == 200
        assert '#9c8cff;">0<' in r.text

    def test_historical_header_is_the_selected_date(self, tmp_path):
        db, c = _client(tmp_path, between_rows=[_row(1, 1592000000.0)])
        r = c.get("/?logdate=" + HIST)
        assert r.status_code == 200
        # Header names the selected day (not "Today") on a historical pick.
        assert ("class=\"today-title\">" + HIST) in r.text

    def test_historical_stats_follow_the_selected_day_rows(self, tmp_path):
        # day_rows: 1 delivered, 1 failed, 1 duplicate(suppressed) -> the cards
        # must reflect THESE rows, NOT get_today_stats (which is not consulted).
        rows = [
            _row(1, 1592000001.0, state="delivered"),
            _row(2, 1592000002.0, state="failed", fusion_status=500),
            _row(3, 1592000003.0, state="duplicate"),
        ]
        db, c = _client(tmp_path, between_rows=rows, today_stats={
            "success": 999, "failed": 999, "pending": 999, "suppressed": 999})
        r = c.get("/?logdate=" + HIST)
        html = r.text
        assert r.status_code == 200
        # historical path classifies day_rows in Python; today stats untouched
        db.get_today_stats.assert_not_awaited()
        assert '#9c8cff;">1<' in html            # 1 suppressed duplicate
        assert 'color: #4caf50;">1<' in html     # 1 successful
        assert 'color: #f44336;">1<' in html     # 1 failed


# --------------------------------------------------------------------------- #
# F3b — last-call-from-Rauland banner (render)
# --------------------------------------------------------------------------- #
class TestLastCallBanner:
    LC = {"created_at": 1708345800.0, "caller_id": "a730r201",
          "display_name": "Code Blue"}

    def test_banner_carries_data_epoch_and_no_js_fallback(self, tmp_path):
        db, c = _client(tmp_path, last_call=self.LC)
        html = c.get("/").text
        assert "Last call from Rauland" in html
        # client-side rendering hook: the numeric epoch on the banner element
        assert 'data-epoch="1708345800.0"' in html
        # no-JS fallback = the server-side display_local string for that epoch
        assert display_local(1708345800.0, NY) in html
        # ...plus a spoken relative age (this epoch is years in the past)
        assert "days ago)" in html
        # and it is NOT inside the stat grid (it is its own banner element)
        assert 'id="lastCallBanner"' in html

    def test_no_calls_on_record_when_empty(self, tmp_path):
        db, c = _client(tmp_path, last_call=None)
        html = c.get("/").text
        assert "No calls on record." in html
        assert 'class="last-call-banner empty"' in html
        # nothing to render client-side, so no epoch hook on the empty banner
        assert 'class="last-call-time" data-epoch' not in html

    def test_non_dict_last_call_degrades_gracefully(self, tmp_path):
        # A mocked/garbage read (non-dict) must not 500 -> treated as "no calls".
        db, c = _client(tmp_path, last_call=object())
        r = c.get("/")
        assert r.status_code == 200
        assert "No calls on record." in r.text

    def test_last_call_read_failure_does_not_500(self, tmp_path):
        db, c = _client(tmp_path)
        db.get_last_call = AsyncMock(side_effect=RuntimeError("boom"))
        r = c.get("/")
        assert r.status_code == 200
        assert "No calls on record." in r.text


# --------------------------------------------------------------------------- #
# _stats_from_rows / _relative_age unit coverage
# --------------------------------------------------------------------------- #
class TestStatsFromRows:
    def test_classification_mirrors_get_today_stats(self):
        rows = [
            _row(1, 1.0, state="delivered"),
            _row(2, 2.0, state="failed"),
            _row(3, 3.0, state="expired"),
            _row(4, 4.0, state="pending"),
            _row(5, 5.0, state="delivering"),
            _row(6, 6.0, state="duplicate"),
            _row(7, 7.0, state="legacy", fusion_status=200),   # legacy OK
            _row(8, 8.0, state="legacy", fusion_status=500),   # legacy bad
            _row(9, 9.0, state="legacy", fusion_status=None),  # legacy bad
        ]
        s = _stats_from_rows(rows)
        assert s["success"] == 2      # delivered + 1 legacy-2xx
        assert s["failed"] == 4       # failed + expired + 2 legacy-bad (500, NULL)
        assert s["pending"] == 2      # pending + delivering
        assert s["suppressed"] == 1   # duplicate

    def test_empty_rows(self):
        s = _stats_from_rows([])
        assert s == {"success": 0, "failed": 0, "pending": 0,
                     "suppressed": 0, "by_state": {}}


class TestRelativeAge:
    @pytest.mark.parametrize("secs,expected", [
        (0, "just now"),
        (10, "just now"),
        (60, "1 minute ago"),
        (180, "3 minutes ago"),
        (3600, "1 hour ago"),
        (7200, "2 hours ago"),
        (86400 * 2, "2 days ago"),
        (86400 * 5, "5 days ago"),
    ])
    def test_shapes(self, secs, expected):
        assert _relative_age(secs) == expected


# --------------------------------------------------------------------------- #
# get_last_call / get_today_stats — real DB (read-only SELECT)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_last_call_returns_newest_real_row_and_ignores_test(tmp_path):
    db = CallDatabase(str(tmp_path / "lc.db"))
    await db.initialize()
    older = await db.create_pending_call(
        caller_id="older", display_name="Code Blue", area_number="730",
        area_name="E.D.", room_number="201", tts_string="Code Blue!", is_test=0)
    newer = await db.create_pending_call(
        caller_id="newer", display_name="RRT", area_number="731",
        area_name="I.C.U.", room_number="400", tts_string="RRT!", is_test=0)
    test_newest = await db.create_pending_call(
        caller_id="testrow", display_name="Code Blue", area_number="730",
        area_name="E.D.", room_number="202", tts_string="Code Blue!", is_test=1)
    # Pin created_at so the NEWEST row on record is the test row (must be ignored).
    await db._db.execute("UPDATE calls SET created_at=? WHERE id=?", (1000.0, older))
    await db._db.execute("UPDATE calls SET created_at=? WHERE id=?", (2000.0, newer))
    await db._db.execute(
        "UPDATE calls SET created_at=? WHERE id=?", (3000.0, test_newest))
    await db._db.commit()

    lc = await db.get_last_call()
    assert lc is not None
    assert lc["caller_id"] == "newer"          # newest REAL row, not the test row
    assert lc["created_at"] == 2000.0
    assert lc["display_name"] == "RRT"
    await db.close()


@pytest.mark.asyncio
async def test_get_last_call_none_when_no_calls(tmp_path):
    db = CallDatabase(str(tmp_path / "empty.db"))
    await db.initialize()
    assert await db.get_last_call() is None
    await db.close()


@pytest.mark.asyncio
async def test_get_today_stats_counts_suppressed_duplicates(tmp_path):
    db = CallDatabase(str(tmp_path / "sup.db"))
    await db.initialize()
    first = await db.create_pending_call(
        caller_id="a730r201", display_name="Code Blue", area_number="730",
        area_name="E.D.", room_number="201", tts_string="Code Blue!", is_test=0)
    dup = await db.create_pending_call(
        caller_id="a730r201", display_name="Code Blue", area_number="730",
        area_name="E.D.", room_number="201", tts_string="Code Blue!", is_test=0)
    # #5 enforcement: transition the duplicate PENDING page to state='duplicate'.
    assert await db.mark_suppressed(dup, duplicate_of=first) == 1
    # a suppressed TEST row must NOT be counted (is_test discipline).
    test_dup = await db.create_pending_call(
        caller_id="a730r201", display_name="Code Blue", area_number="730",
        area_name="E.D.", room_number="201", tts_string="Code Blue!", is_test=1)
    await db.mark_suppressed(test_dup, duplicate_of=first)

    stats = await db.get_today_stats()
    assert stats["suppressed"] == 1
    await db.close()
