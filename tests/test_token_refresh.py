"""#4 background token refresh tests (against the local mock)."""

import asyncio
import time

import pytest

from sipgw.config import FusionConfig
from sipgw.webhook import FusionWebhook
from tests.mock_fusion import run_mock_fusion


def _webhook(base_url: str, **kw) -> FusionWebhook:
    cfg = FusionConfig(
        base_url=base_url + "/api",
        token_url=base_url + "/api/token",
        audience="prov", scenario_id="scen-1",
        scenario_field_id="mock-field-id",
        client_id="cid", client_secret="secret",
        dry_run=False,
    )
    for k, v in kw.items():
        setattr(cfg, k, v)
    return FusionWebhook(cfg)


class TestMinRemaining:
    @pytest.mark.asyncio
    async def test_larger_margin_forces_refetch(self):
        with run_mock_fusion("200") as (base, state):
            wh = _webhook(base)
            await wh.initialize()
            try:
                await wh._get_token()                     # fetch #1, caches
                assert state.count("POST", "/token") == 1
                # Pretend the token has 100s left.
                wh._token.expires_at = time.time() + 100
                await wh._get_token(min_remaining=60)     # 100>60 -> cached, no fetch
                assert state.count("POST", "/token") == 1
                await wh._get_token(min_remaining=300)    # 100<300 -> refetch
                assert state.count("POST", "/token") == 2
            finally:
                await wh.close()


class TestBackgroundRefresh:
    @pytest.mark.asyncio
    async def test_start_warms_cache_then_stops_clean(self):
        with run_mock_fusion("200") as (base, state):
            wh = _webhook(base)
            await wh.initialize()
            try:
                await wh.start_token_refresh()
                # The loop should fetch a token promptly.
                async def wait_token():
                    while wh._token is None:
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(wait_token(), timeout=2.0)
                assert state.count("POST", "/token") >= 1
            finally:
                await wh.stop_token_refresh()
                assert wh._refresh_task is None
                await wh.close()

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self):
        with run_mock_fusion("200") as (base, _state):
            wh = _webhook(base)
            await wh.initialize()
            try:
                await wh.start_token_refresh()
                t1 = wh._refresh_task
                await wh.start_token_refresh()            # no second task
                assert wh._refresh_task is t1
            finally:
                await wh.close()                          # close() stops refresh
                assert wh._refresh_task is None

    @pytest.mark.asyncio
    async def test_stop_safe_when_never_started(self):
        wh = _webhook("http://127.0.0.1:1")
        # No initialize, no start: stop must be a no-op, not an error.
        await wh.stop_token_refresh()
