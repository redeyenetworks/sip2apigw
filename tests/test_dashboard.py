"""Unit tests for the dashboard."""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from sipgw.dashboard import create_dashboard
from sipgw.database import CallDatabase
from sipgw.config import DashboardConfig

SAMPLE_CALLS = [
    {
        "id": 1,
        "timestamp": "2026-02-19 10:30:00",
        "caller_id": "a730r201",
        "display_name": "Code Blue",
        "area_number": 730,
        "area_name": "1st Floor. E.D.",
        "room_number": 201,
        "tts_string": "Code Blue! 1st Floor. E.D. Room 201.",
        "fusion_status": 200,
        "response_time_ms": 150.5,
        "created_at": 1708345800.0,
        "state": "delivered",
        "attempts": 1,
        "last_error": None,
        "is_test": 0,
    },
    {
        "id": 2,
        "timestamp": "2026-02-19 10:25:00",
        "caller_id": "a731r400",
        "display_name": "RRT",
        "area_number": 731,
        "area_name": "4th Floor, I.C.U.",
        "room_number": 400,
        "tts_string": "Rapid Response Team! 4th Floor, I.C.U. Room 400.",
        "fusion_status": 500,
        "response_time_ms": 200.3,
        "created_at": 1708345500.0,
        "state": "failed",
        "attempts": 3,
        "last_error": "SIP-timeout-xyz",
        "is_test": 0,
    },
]


@pytest.fixture(autouse=True)
def _no_ambient_log_days(monkeypatch):
    """These tests exercise the today / get_calls_page call-table path.

    F1 routes the call table through get_calls_between whenever the display zone
    has log coverage (a selectable day). This suite constructs dashboards without
    an isolated log dir, so it would otherwise pick up the host's ambient
    /var/log/sipgw coverage and flip to the date-window path. Force "no log
    coverage" so selected_date is None and the classic today path is exercised;
    the date-picker/window behaviour is covered in test_log_viewer/test_dashboard_f1.
    """
    monkeypatch.setattr("sipgw.dashboard._available_log_days", lambda *a, **k: [])


@pytest.fixture
def mock_db():
    db = AsyncMock(spec=CallDatabase)
    db.get_recent_calls = AsyncMock(return_value=SAMPLE_CALLS)
    db.get_calls_page = AsyncMock(return_value=(SAMPLE_CALLS, 2, 1))
    db.get_today_stats = AsyncMock(return_value={"success": 50, "failed": 3, "pending": 7})
    # #13-P1: export_calls is the is_test=0-enforcing DB method; the mock returns
    # only REAL rows, mirroring the real query (test-row exclusion covered below).
    db.export_calls = AsyncMock(return_value=SAMPLE_CALLS)
    import time as _t
    db.read_heartbeat = AsyncMock(return_value=_t.time())   # #7 fresh heartbeat
    # #7 informational /health reads (additive; never affect the status code).
    db.read_fusion_check = AsyncMock(return_value=None)
    db.delivery_health_snapshot = AsyncMock(return_value={
        "backlog": 0, "last_delivered_at": None,
        "last_failed_at": None, "last_error": None})
    return db


@pytest.fixture
def dashboard_config():
    return DashboardConfig(port=8080, bind_ip="0.0.0.0", auto_refresh_seconds=30, page_size=20)


@pytest.fixture
def client(mock_db, dashboard_config):
    app = create_dashboard(mock_db, dashboard_config)
    return TestClient(app)


class TestDashboard:
    def test_index_returns_html(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_index_contains_call_data(self, client):
        response = client.get("/")
        html = response.text
        assert "Code Blue" in html
        assert "a730r201" in html
        assert "Room 201" in html

    def test_index_contains_rrt(self, client):
        response = client.get("/")
        html = response.text
        assert "RRT" in html
        assert "a731r400" in html

    def test_index_auto_refresh_off_by_default(self, client):
        response = client.get("/")
        # Auto-refresh checkbox should not be checked by default
        assert "OFF" in response.text

    def test_index_auto_refresh_on(self, client):
        response = client.get("/?auto=1&refresh=30")
        assert "ON" in response.text

    def test_api_calls_endpoint(self, client):
        response = client.get("/api/calls")
        assert response.status_code == 200
        data = response.json()
        assert "calls" in data
        assert len(data["calls"]) == 2

    def test_health_endpoint(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    def test_stats_from_db_not_page(self, mock_db, dashboard_config):
        """Stats cards should reflect all today's calls, not just the current page."""
        mock_db.get_calls_page = AsyncMock(return_value=(SAMPLE_CALLS, 100, 5))
        mock_db.get_today_stats = AsyncMock(
            return_value={"success": 85, "failed": 15, "pending": 4})
        app = create_dashboard(mock_db, dashboard_config)
        client = TestClient(app)
        response = client.get("/")
        # Should show DB-wide stats (85/15/4), not page-level (2/0)
        assert ">85<" in response.text
        assert ">15<" in response.text

    def test_pending_card_rendered(self, client):
        response = client.get("/")
        assert "Pending" in response.text
        assert ">7<" in response.text        # pending count from the mock

    def test_local_time_column(self, client):
        # #12: dashboard shows local wall time, not the raw stored UTC string.
        response = client.get("/")
        assert "Time (local)" in response.text

    def test_empty_dashboard(self, mock_db, dashboard_config):
        mock_db.get_calls_page = AsyncMock(return_value=([], 0, 1))
        mock_db.get_today_stats = AsyncMock(return_value={"success": 0, "failed": 0})
        app = create_dashboard(mock_db, dashboard_config)
        client = TestClient(app)
        response = client.get("/")
        assert response.status_code == 200
        assert "No calls recorded yet" in response.text

    def test_pagination_params(self, mock_db, dashboard_config):
        mock_db.get_calls_page = AsyncMock(return_value=(SAMPLE_CALLS, 50, 3))
        app = create_dashboard(mock_db, dashboard_config)
        client = TestClient(app)
        response = client.get("/?page=2")
        assert response.status_code == 200
        assert "Page 2 of 3" in response.text


class TestViewToggle:
    def test_summary_hides_advanced_columns(self, client):
        response = client.get("/?view=summary")
        assert response.status_code == 200
        html = response.text
        assert "Attempts" not in html
        assert "Last Error" not in html
        # the failed-row error string must not appear in summary
        assert "SIP-timeout-xyz" not in html

    def test_default_view_is_summary(self, client):
        # No view param -> summary (advanced columns hidden).
        html = client.get("/").text
        assert "Attempts" not in html
        assert "Last Error" not in html

    def test_advanced_shows_and_populates_columns(self, client):
        response = client.get("/?view=advanced")
        assert response.status_code == 200
        html = response.text
        # headers present
        assert "Attempts" in html
        assert "Last Error" in html
        # populated from the row dicts
        assert "SIP-timeout-xyz" in html      # last_error
        assert "failed" in html               # state
        assert "delivered" in html            # state of the other row

    def test_bogus_view_falls_back_to_summary_no_500(self, client):
        response = client.get("/?view=bogus")
        assert response.status_code == 200
        assert "Attempts" not in response.text

    def test_pagination_preserves_view(self, mock_db, dashboard_config):
        mock_db.get_calls_page = AsyncMock(return_value=(SAMPLE_CALLS, 50, 3))
        app = create_dashboard(mock_db, dashboard_config)
        client = TestClient(app)
        response = client.get("/?page=1&view=advanced")
        assert response.status_code == 200
        # pagination links carry the current view so toggling pages doesn't reset it
        assert "view=advanced" in response.text


class TestCsvExport:
    def test_export_returns_csv(self, client):
        response = client.get("/export.csv")
        assert response.status_code == 200
        assert "text/csv" in response.headers["content-type"]

    def test_export_has_attachment_with_date(self, client):
        response = client.get("/export.csv")
        cd = response.headers["content-disposition"]
        assert "attachment" in cd
        assert ".csv" in cd

    def test_export_has_header_row(self, client):
        response = client.get("/export.csv")
        lines = response.text.splitlines()
        assert lines[0].startswith("Time (local),Caller ID,Area,Room,TTS String,State,Fusion Status")

    def test_export_contains_call_rows(self, client):
        body = client.get("/export.csv").text
        assert "a730r201" in body
        assert "a731r400" in body

    def test_export_excludes_test_rows(self, mock_db, dashboard_config):
        # The dashboard trusts db.export_calls to exclude is_test=1 rows. Seed the
        # mock with only a REAL row and assert a would-be test row never appears.
        real_row = dict(SAMPLE_CALLS[0], caller_id="REAL-CALLER")
        mock_db.export_calls = AsyncMock(return_value=[real_row])
        app = create_dashboard(mock_db, dashboard_config)
        client = TestClient(app)
        body = client.get("/export.csv").text
        assert "REAL-CALLER" in body
        assert "TESTLEAK" not in body
        # export must be called for today's window only
        mock_db.export_calls.assert_awaited_once_with(today_only=True)


    def test_export_has_utc_and_fusion_result_columns(self, client):
        # #13-P1: header carries both a local and a UTC timestamp column plus a
        # friendly Fusion Result column.
        lines = client.get("/export.csv").text.splitlines()
        header = lines[0]
        assert "Time (local)" in header
        assert "Time (UTC)" in header
        assert "Fusion Result" in header

    def test_export_friendly_fusion_result_delivered_and_rejected(self, client):
        # SAMPLE_CALLS: row1 200 -> delivered, row2 500 -> FAILED (HTTP 500).
        body = client.get("/export.csv").text
        assert "delivered" in body
        assert "FAILED (HTTP 500)" in body

    def test_export_delivery_exception_minus_one(self, mock_db, dashboard_config):
        row = dict(SAMPLE_CALLS[0], caller_id="excrow", fusion_status=-1)
        mock_db.export_calls = AsyncMock(return_value=[row])
        client = TestClient(create_dashboard(mock_db, dashboard_config))
        body = client.get("/export.csv").text
        assert "FAILED (delivery exception)" in body

    def test_export_pending_fusion_result(self, mock_db, dashboard_config):
        row = dict(SAMPLE_CALLS[0], caller_id="pendrow", fusion_status=None)
        mock_db.export_calls = AsyncMock(return_value=[row])
        client = TestClient(create_dashboard(mock_db, dashboard_config))
        body = client.get("/export.csv").text
        assert "pending" in body

    def test_export_utf8_opens_clean(self, client):
        # bytes decode as utf-8 without error
        client.get("/export.csv").content.decode("utf-8")

    def test_export_formula_injection_neutralized(self, mock_db, dashboard_config):
        row = dict(SAMPLE_CALLS[0], caller_id="=cmd|'/c calc'!A0",
                   tts_string="+SUM(A1)")
        mock_db.export_calls = AsyncMock(return_value=[row])
        client = TestClient(create_dashboard(mock_db, dashboard_config))
        body = client.get("/export.csv").text
        # leading = / + neutralized with a leading apostrophe
        assert "'=cmd" in body
        assert "'+SUM(A1)" in body

    def test_export_range_over_cap_is_400(self, client):
        # end - start > 400 days must 400 BEFORE any query.
        r = client.get("/export.csv?scope=range&start=0&end=99999999999")
        assert r.status_code == 400

    def test_export_range_missing_params_is_400(self, client):
        assert client.get("/export.csv?scope=range").status_code == 400

    def test_export_range_end_before_start_is_400(self, client):
        r = client.get("/export.csv?scope=range&start=1000&end=500")
        assert r.status_code == 400

    def test_export_bogus_scope_falls_back_no_500(self, client):
        r = client.get("/export.csv?scope=bananas")
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]

    def test_export_valid_range_uses_get_calls_between(self, mock_db, dashboard_config):
        row = dict(SAMPLE_CALLS[0], caller_id="RANGEROW")
        mock_db.get_calls_between = AsyncMock(return_value=[row])
        client = TestClient(create_dashboard(mock_db, dashboard_config))
        r = client.get("/export.csv?scope=range&start=1000&end=2000")
        assert r.status_code == 200
        assert "RANGEROW" in r.text
        mock_db.get_calls_between.assert_awaited_once_with(1000.0, 2000.0)


class TestPlainStatus:
    def test_status_mapping_unit(self):
        from sipgw.dashboard import _plain_status
        for code, expected in [(200, "Delivered"), (204, "Delivered"),
                               (-1, "NOT SENT - delivery failed"),
                               (404, "NOT SENT - rejected"),
                               (503, "NOT SENT - rejected"),
                               (None, "Pending")]:
            glyph, text, css = _plain_status(code)
            assert text == expected
            assert glyph            # non-empty glyph
            assert css              # non-empty css class

    def test_status_rendered_with_aria_label(self, client):
        html = client.get("/").text
        # 200 row -> Delivered with an aria-label (no colour-only signalling)
        assert "Delivered" in html
        assert 'aria-label="Delivery status: Delivered"' in html
        # 500 row -> rejected
        assert "NOT SENT - rejected" in html

    def test_pending_status_when_null(self, mock_db, dashboard_config):
        row = dict(SAMPLE_CALLS[0], fusion_status=None)
        mock_db.get_calls_page = AsyncMock(return_value=([row], 1, 1))
        client = TestClient(create_dashboard(mock_db, dashboard_config))
        html = client.get("/").text
        assert 'aria-label="Delivery status: Pending"' in html

    def test_delivery_exception_status_minus_one(self, mock_db, dashboard_config):
        row = dict(SAMPLE_CALLS[0], fusion_status=-1)
        mock_db.get_calls_page = AsyncMock(return_value=([row], 1, 1))
        client = TestClient(create_dashboard(mock_db, dashboard_config))
        html = client.get("/").text
        assert "NOT SENT - delivery failed" in html


class TestTimeToggle:
    def test_time_cell_has_data_epoch(self, client):
        html = client.get("/").text
        # data-epoch equals the row created_at (JSON-encoded numeric)
        assert 'data-epoch="1708345800.0"' in html

    def test_time_ctx_blob_parses_as_json(self, client):
        import re, json
        html = client.get("/").text
        m = re.search(
            r'<script id="sipgw-time-ctx" type="application/json">(.*?)</script>',
            html, re.S)
        assert m, "time-ctx blob missing"
        ctx = json.loads(m.group(1))
        assert "server_tz" in ctx
        assert "ts_format" in ctx

    def test_server_rendered_time_is_nojs_fallback(self, client):
        # display_time (server string) remains as the no-JS fallback inside the cell
        html = client.get("/").text
        assert "2024-02" in html   # local render of the fixed epoch (1708345800)


class TestXssSafety:
    def test_script_in_fields_rendered_inert(self, mock_db, dashboard_config):
        evil = dict(
            SAMPLE_CALLS[0],
            tts_string="<script>alert('tts')</script>",
            display_name="<script>alert('name')</script>",
        )
        mock_db.get_calls_page = AsyncMock(return_value=([evil], 1, 1))
        for view in ("summary", "advanced"):
            client = TestClient(create_dashboard(mock_db, dashboard_config))
            html = client.get(f"/?view={view}").text
            assert "<script>alert('tts')</script>" not in html
            assert "<script>alert('name')</script>" not in html
            assert "&lt;script&gt;alert(&#39;tts&#39;)&lt;/script&gt;" in html


class TestEventId:
    def test_advanced_renders_event_id(self, mock_db, dashboard_config):
        row = dict(SAMPLE_CALLS[0], event_id="EVT7788")
        mock_db.get_calls_page = AsyncMock(return_value=([row], 1, 1))
        client = TestClient(create_dashboard(mock_db, dashboard_config))
        html = client.get("/?view=advanced").text
        assert "Event ID" in html      # column header
        assert "EVT7788" in html       # value from the row

    def test_event_id_hidden_in_summary(self, mock_db, dashboard_config):
        row = dict(SAMPLE_CALLS[0], event_id="EVT7788")
        mock_db.get_calls_page = AsyncMock(return_value=([row], 1, 1))
        client = TestClient(create_dashboard(mock_db, dashboard_config))
        html = client.get("/?view=summary").text
        assert "Event ID" not in html
        assert "EVT7788" not in html

    def test_event_id_autoescaped(self, mock_db, dashboard_config):
        # event_id originates from an attacker-influenceable SIP Call-ID segment;
        # it must be rendered inert (autoescape=True), never as live markup.
        row = dict(SAMPLE_CALLS[0], event_id="<script>alert('e')</script>")
        mock_db.get_calls_page = AsyncMock(return_value=([row], 1, 1))
        client = TestClient(create_dashboard(mock_db, dashboard_config))
        html = client.get("/?view=advanced").text
        assert "<script>alert('e')</script>" not in html
        assert "&lt;script&gt;" in html

    def test_missing_event_id_key_no_crash(self, client):
        # SAMPLE_CALLS predate the migration (no event_id key). The advanced
        # view must still render (Jinja Undefined -> '-'), never 500 — the
        # dashboard is decoupled and may read a not-yet-migrated DB.
        r = client.get("/?view=advanced")
        assert r.status_code == 200
        assert "a730r201" in r.text

    def test_export_has_event_id_column(self, client):
        header = client.get("/export.csv").text.splitlines()[0]
        assert "Event ID" in header

    def test_export_event_id_value(self, mock_db, dashboard_config):
        row = dict(SAMPLE_CALLS[0], event_id="EVT7788")
        mock_db.export_calls = AsyncMock(return_value=[row])
        client = TestClient(create_dashboard(mock_db, dashboard_config))
        assert "EVT7788" in client.get("/export.csv").text


@pytest.mark.asyncio
async def test_get_calls_between_real_db(tmp_path):
    """#13-P1: get_calls_between returns only is_test=0 rows within [start,end]."""
    from sipgw.database import CallDatabase
    db = CallDatabase(str(tmp_path / "r.db"))
    await db.initialize()
    import time as _t
    now = _t.time()
    # seed: one real row, one test row (both "now"); assert test row excluded
    await db.create_pending_call(
        caller_id="realrange", display_name="Code Blue", area_number="730",
        area_name="E.D.", room_number="201", tts_string="Code Blue!", is_test=0)
    await db.create_pending_call(
        caller_id="testrange", display_name="Code Blue", area_number="730",
        area_name="E.D.", room_number="202", tts_string="Code Blue!", is_test=1)
    rows = await db.get_calls_between(now - 3600, now + 3600)
    callers = {r["caller_id"] for r in rows}
    assert "realrange" in callers
    assert "testrange" not in callers
    assert all(r["is_test"] == 0 for r in rows)
    # a window that excludes the rows returns nothing
    empty = await db.get_calls_between(0, 1000)
    assert empty == []
    await db.close()


@pytest.mark.asyncio
async def test_export_calls_db_excludes_is_test(tmp_path):
    """#13-P1: the real export_calls DB method enforces AND is_test=0."""
    from sipgw.database import CallDatabase
    db = CallDatabase(str(tmp_path / "e.db"))
    await db.initialize()
    real = await db.create_pending_call(
        caller_id="real", display_name="Code Blue", area_number="730",
        area_name="E.D.", room_number="201", tts_string="Code Blue!", is_test=0)
    test = await db.create_pending_call(
        caller_id="testrow", display_name="Code Blue", area_number="730",
        area_name="E.D.", room_number="202", tts_string="Code Blue!", is_test=1)
    rows = await db.export_calls(today_only=True)
    callers = {r["caller_id"] for r in rows}
    assert "real" in callers
    assert "testrow" not in callers
    assert all(r["is_test"] == 0 for r in rows)
    await db.close()
