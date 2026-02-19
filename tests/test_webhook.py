"""Unit tests for Fusion webhook client."""

import pytest
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
from sipgw.webhook import FusionWebhook, TokenInfo
from sipgw.config import FusionConfig


@pytest.fixture
def fusion_config():
    return FusionConfig(
        base_url="https://admin.icmobile.singlewire.com",
        token_url="https://admin.icmobile.singlewire.com/api/oauth/token",
        scenario_id="test-scenario-id",
        scenario_endpoint="/api/scenarios/{scenario_id}/launch",
        variable_name="customTTS",
        client_id="test-client-id",
        client_secret="test-client-secret",
    )


@pytest.fixture
def webhook(fusion_config):
    wh = FusionWebhook(fusion_config)
    wh._field_id = "test-field-id"
    return wh


def _make_response(status_code=200, json_data=None):
    """Create a MagicMock httpx Response (sync json(), sync status_code)."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.raise_for_status = MagicMock()
    return resp


class TestFusionWebhook:
    @pytest.mark.asyncio
    async def test_trigger_scenario_success(self, webhook):
        """Test successful scenario trigger."""
        mock_client = AsyncMock()

        token_response = _make_response(200, {
            "access_token": "test-token",
            "expires_in": 3600,
        })
        trigger_response = _make_response(200)

        mock_client.post = AsyncMock(side_effect=[token_response, trigger_response])
        webhook._client = mock_client

        status, elapsed = await webhook.trigger_scenario("Code Blue! 1st Floor. E.D. Room 201.")

        assert status == 200
        assert elapsed > 0
        assert mock_client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_token_caching(self, webhook):
        """Test that tokens are cached and reused."""
        import time

        webhook._token = TokenInfo(
            access_token="cached-token",
            expires_at=time.time() + 3600,
        )

        mock_client = AsyncMock()
        trigger_response = _make_response(200)
        mock_client.post = AsyncMock(return_value=trigger_response)
        webhook._client = mock_client

        status, elapsed = await webhook.trigger_scenario("Test TTS")

        assert status == 200
        # Should only have 1 call (trigger), not 2 (token + trigger)
        assert mock_client.post.call_count == 1

    @pytest.mark.asyncio
    async def test_trigger_scenario_error(self, webhook):
        """Test handling of HTTP errors."""
        import time

        webhook._token = TokenInfo(
            access_token="test-token",
            expires_at=time.time() + 3600,
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("Connection refused"))
        webhook._client = mock_client

        status, elapsed = await webhook.trigger_scenario("Test TTS")

        assert status == -1
        assert elapsed > 0

    @pytest.mark.asyncio
    async def test_trigger_scenario_401_retry(self, webhook):
        """Test 401 retry with token refresh."""
        import time

        webhook._token = TokenInfo(
            access_token="expired-token",
            expires_at=time.time() + 3600,
        )

        mock_client = AsyncMock()

        first_response = _make_response(401)
        token_response = _make_response(200, {
            "access_token": "new-token",
            "expires_in": 3600,
        })
        retry_response = _make_response(200)

        mock_client.post = AsyncMock(
            side_effect=[first_response, token_response, retry_response]
        )
        webhook._client = mock_client

        status, elapsed = await webhook.trigger_scenario("Test TTS")

        assert status == 200
