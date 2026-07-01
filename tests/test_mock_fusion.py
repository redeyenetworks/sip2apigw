"""§2c smoke drills: prove the mock Fusion server + real-path wiring work.

These run with dry_run OFF on purpose — the URLs point at 127.0.0.1, so this is
a real HTTP round-trip to the mock and NOT a real notification. This is the
substrate the #2 delivery/retry drills build on.
"""

import httpx
import pytest

from sipgw.config import FusionConfig
from sipgw.webhook import FusionWebhook
from tests.mock_fusion import run_mock_fusion


def _cfg(base_url: str, **kw) -> FusionConfig:
    cfg = FusionConfig(
        base_url=base_url + "/api",
        token_url=base_url + "/api/token",
        audience="prov",
        scenario_id="scen-1",
        scenario_endpoint="/v1/scenario-notifications",
        variable_name="customTTS",
        scenario_field_id="",   # exercise the scenario GET against the mock
        client_id="cid",
        client_secret="secret",
        dry_run=False,          # real round-trip to 127.0.0.1 mock
    )
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


class TestMockHappyPath:
    @pytest.mark.asyncio
    async def test_full_trigger_returns_200(self):
        with run_mock_fusion("200") as (base, state):
            wh = FusionWebhook(_cfg(base))
            await wh.initialize()
            try:
                status, elapsed = await wh.trigger_scenario("Attention! Code Blue! TEST.")
            finally:
                await wh.close()
        assert status == 200
        # All three origins really hit the mock over the loopback.
        assert state.count("POST", "/token") == 1
        assert state.count("GET", "/v1/scenarios/") == 1
        assert state.count("POST", "scenario-notifications") == 1


class TestMockFailureModes:
    @pytest.mark.asyncio
    async def test_500_is_surfaced(self):
        with run_mock_fusion("500") as (base, _state):
            wh = FusionWebhook(_cfg(base, scenario_field_id="mock-field-id"))
            await wh.initialize()
            try:
                status, _ = await wh.trigger_scenario("Attention! RRT! TEST.")
            finally:
                await wh.close()
        assert status == 500

    @pytest.mark.asyncio
    async def test_429_carries_retry_after(self):
        # Direct client call so we can inspect the Retry-After header the
        # #2 retry worker will consume later.
        with run_mock_fusion("429", retry_after=7) as (base, _state):
            async with httpx.AsyncClient() as c:
                r = await c.post(base + "/api/v1/scenario-notifications", json={"x": 1})
        assert r.status_code == 429
        assert r.headers.get("Retry-After") == "7"

    @pytest.mark.asyncio
    async def test_401_then_200_token(self):
        with run_mock_fusion("401_then_200") as (base, _state):
            async with httpx.AsyncClient() as c:
                first = await c.post(base + "/api/token", data={"grant_type": "x"})
                second = await c.post(base + "/api/token", data={"grant_type": "x"})
        assert first.status_code == 401
        assert second.status_code == 200
        assert second.json()["access_token"] == "mock-token"


class TestMockEscalationSink:
    @pytest.mark.asyncio
    async def test_escalation_returns_204(self):
        with run_mock_fusion("200") as (base, state):
            async with httpx.AsyncClient() as c:
                r = await c.post(base + "/escalation", json={"text": "TEST escalation"})
        assert r.status_code == 204
        assert state.count("POST", "escalation") == 1
