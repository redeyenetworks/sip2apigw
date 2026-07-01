"""§2a NO-SEND leak tests.

These prove the structural guarantee: while dry-run is active, no request of any
method reaches a non-127.0.0.1 host, even though config points at the real
Fusion hosts and every Fusion origin is exercised.
"""

import httpx
import pytest

from sipgw.config import FusionConfig
from sipgw.safety import effective_dry_run
from sipgw.webhook import FusionWebhook


def _real_host_config(**overrides) -> FusionConfig:
    cfg = FusionConfig(
        base_url="https://api.icmobile.singlewire.com/api",
        token_url="https://api.icmobile.singlewire.com/api/token",
        audience="provider-uuid",
        scenario_id="scenario-uuid",
        scenario_endpoint="/v1/scenario-notifications",
        variable_name="customTTS",
        scenario_field_id="",          # force the field-id GET so it is exercised
        client_id="cid",
        client_secret="supersecret",
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class TestEffectiveDryRun:
    def test_config_flag_enables(self):
        assert effective_dry_run(True) is True
        assert effective_dry_run(False) is False

    def test_env_can_enable(self, monkeypatch):
        monkeypatch.setenv("SIPGW_DRY_RUN", "1")
        assert effective_dry_run(False) is True

    def test_env_cannot_disable(self, monkeypatch):
        # Even with the env var set to a falsey-looking value, a config that
        # enables dry-run stays enabled. Nothing can force real sending.
        monkeypatch.setenv("SIPGW_DRY_RUN", "0")
        assert effective_dry_run(True) is True
        monkeypatch.setenv("SIPGW_DRY_RUN", "false")
        assert effective_dry_run(True) is True

    def test_env_non_one_does_not_enable(self, monkeypatch):
        monkeypatch.setenv("SIPGW_DRY_RUN", "0")
        assert effective_dry_run(False) is False


class TestNoSendLeak:
    @pytest.mark.asyncio
    async def test_trigger_scenario_sends_nothing_real(self):
        cfg = _real_host_config(dry_run=True)
        wh = FusionWebhook(cfg)
        await wh.initialize()
        try:
            status, elapsed = await wh.trigger_scenario("Attention! Code Blue! TEST bay.")
            assert status == 200          # synthetic success, no real page
            assert elapsed >= 0

            t = wh._transport
            assert t is not None, "dry-run must install the guard transport"

            # THE load-bearing assertion: zero real sends to any non-local host.
            assert t.forwarded == [], f"real send leaked: {t.forwarded}"

            # All three Fusion origins were exercised and refused.
            blocked = [(m, httpx.URL(u).path) for m, u in t.blocked]
            assert any(m == "POST" and p.endswith("/token") for m, p in blocked), blocked
            assert any(m == "GET" and "/v1/scenarios/" in p for m, p in blocked), blocked
            assert any(m == "POST" and p.endswith("/scenario-notifications")
                       for m, p in blocked), blocked
        finally:
            await wh.close()

    @pytest.mark.asyncio
    async def test_env_forces_no_send_even_with_config_off(self, monkeypatch):
        monkeypatch.setenv("SIPGW_DRY_RUN", "1")
        cfg = _real_host_config(dry_run=False)   # config says send; env forbids it
        wh = FusionWebhook(cfg)
        await wh.initialize()
        try:
            assert wh._dry_run is True
            await wh.trigger_scenario("Attention! RRT! TEST bay.")
            assert wh._transport.forwarded == []
        finally:
            await wh.close()

    @pytest.mark.asyncio
    async def test_escalation_style_post_is_blocked(self):
        """A POST to an escalation-style URL is refused with 204, no real send."""
        from sipgw.safety import NoSendGuardTransport
        t = NoSendGuardTransport()
        async with httpx.AsyncClient(transport=t) as client:
            resp = await client.post("https://hooks.example.com/escalation",
                                     json={"text": "TEST"})
        assert resp.status_code == 204
        assert t.forwarded == []
        assert t.blocked and t.blocked[0][0] == "POST"

    @pytest.mark.asyncio
    async def test_localhost_is_forwarded_not_blocked(self):
        """127.0.0.1 is allowed through to the inner transport (mock drills)."""
        from sipgw.safety import NoSendGuardTransport
        t = NoSendGuardTransport()
        async with httpx.AsyncClient(transport=t) as client:
            # Nothing is listening; a real connection attempt (ConnectError)
            # proves we forwarded rather than synthesized.
            with pytest.raises(httpx.HTTPError):
                await client.get("http://127.0.0.1:9/never", timeout=0.5)
        assert t.blocked == []
        assert t.forwarded and httpx.URL(t.forwarded[0][1]).host == "127.0.0.1"

    @pytest.mark.asyncio
    async def test_dry_run_off_has_no_guard(self):
        cfg = _real_host_config(dry_run=False)
        wh = FusionWebhook(cfg)
        await wh.initialize()
        try:
            assert wh._dry_run is False
            assert wh._transport is None
        finally:
            await wh.close()
