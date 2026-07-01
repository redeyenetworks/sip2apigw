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
        "fusion_status": 200,
        "response_time_ms": 200.3,
        "created_at": 1708345500.0,
    },
]


@pytest.fixture
def mock_db():
    db = AsyncMock(spec=CallDatabase)
    db.get_recent_calls = AsyncMock(return_value=SAMPLE_CALLS)
    db.get_calls_page = AsyncMock(return_value=(SAMPLE_CALLS, 2, 1))
    db.get_today_stats = AsyncMock(return_value={"success": 50, "failed": 3, "pending": 7})
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
