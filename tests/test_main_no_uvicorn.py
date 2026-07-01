"""#14 The writer process (sipgw.main) no longer serves the dashboard.

uvicorn moved to sipgw.dashboard_app. main imports no uvicorn, the gateway has
no dashboard object, and run() starts NO uvicorn/dashboard task — only the SIP
listener, heartbeat, worker, and watchdog.
"""

import asyncio
import inspect

import pytest
from unittest.mock import AsyncMock

import sipgw.main
from sipgw.config import AppConfig
from sipgw.main import SIPGateway


def test_uvicorn_not_imported_in_main():
    assert "uvicorn" not in vars(sipgw.main)


def test_run_source_has_no_uvicorn_or_dashboard():
    src = inspect.getsource(SIPGateway.run)
    assert "uvicorn" not in src
    assert "create_dashboard" not in src
    assert "dashboard_task" not in src


def test_gateway_has_no_dashboard_attribute(tmp_path):
    cfg = AppConfig()
    cfg.fusion.dry_run = True
    cfg.database.path = str(tmp_path / "gw.db")
    gw = SIPGateway(cfg)
    assert not hasattr(gw, "dashboard")


class _BlockingSip:
    """Stands in for the SIP listener: signals it started, then blocks until
    cancelled (mirrors the real never-returning start())."""

    def __init__(self):
        self.started = asyncio.Event()
        self.stopped = False

    async def start(self):
        self.started.set()
        await asyncio.Event().wait()

    async def stop(self):
        self.stopped = True


@pytest.mark.asyncio
async def test_run_creates_no_dashboard_task(tmp_path):
    cfg = AppConfig()
    cfg.fusion.dry_run = True
    cfg.database.path = str(tmp_path / "gw.db")
    gw = SIPGateway(cfg)

    # Stub every long-running collaborator so run() reaches the shutdown wait.
    sip = _BlockingSip()
    gw.sip_server = sip
    gw.webhook = AsyncMock()
    gw.escalator = AsyncMock()
    gw.worker = AsyncMock()
    gw.worker.recover = AsyncMock(return_value=0)

    run_task = asyncio.create_task(gw.run())
    try:
        await asyncio.wait_for(sip.started.wait(), timeout=5)
        # Let the loop settle so any spurious server task would have appeared.
        await asyncio.sleep(0.05)

        tasks = asyncio.all_tasks()
        blob = " ".join(
            (t.get_coro().__qualname__ if hasattr(t.get_coro(), "__qualname__") else str(t.get_coro()))
            for t in tasks
        ).lower()
        assert "uvicorn" not in blob
        assert "server.serve" not in blob
    finally:
        gw.request_shutdown()
        await asyncio.wait_for(run_task, timeout=5)

    # Clean shutdown reached the SIP stop path (writer-only lifecycle).
    assert sip.stopped is True
