"""#8 systemd notify/watchdog tests (no real systemd required)."""

import asyncio
import os
import socket

import pytest

from sipgw import watchdog


class TestNotifyInert:
    def test_no_socket_is_noop(self, monkeypatch):
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        assert watchdog.notify_ready() is False
        assert watchdog.notify_watchdog() is False
        assert watchdog.notify_stopping() is False

    def test_interval_none_without_env(self, monkeypatch):
        monkeypatch.delenv("WATCHDOG_USEC", raising=False)
        assert watchdog.watchdog_interval_seconds() is None

    def test_interval_is_half_of_usec(self, monkeypatch):
        monkeypatch.setenv("WATCHDOG_USEC", str(30_000_000))   # 30s
        assert watchdog.watchdog_interval_seconds() == pytest.approx(15.0)

    def test_interval_bad_value(self, monkeypatch):
        monkeypatch.setenv("WATCHDOG_USEC", "not-a-number")
        assert watchdog.watchdog_interval_seconds() is None


class TestNotifySends:
    def test_ready_datagram_received(self, tmp_path, monkeypatch):
        sock_path = str(tmp_path / "notify.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        srv.bind(sock_path)
        srv.settimeout(2)
        try:
            monkeypatch.setenv("NOTIFY_SOCKET", sock_path)
            assert watchdog.notify_ready() is True
            data, _ = srv.recvfrom(64)
            assert data == b"READY=1"
        finally:
            srv.close()


class TestPinger:
    @pytest.mark.asyncio
    async def test_pinger_inert_without_watchdog(self, monkeypatch):
        monkeypatch.delenv("WATCHDOG_USEC", raising=False)
        p = watchdog.WatchdogPinger()
        await p.start()
        assert p._task is None          # nothing scheduled
        await p.stop()                  # safe no-op

    @pytest.mark.asyncio
    async def test_pinger_runs_and_stops(self, tmp_path, monkeypatch):
        sock_path = str(tmp_path / "wd.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        srv.bind(sock_path)
        srv.setblocking(False)
        try:
            monkeypatch.setenv("NOTIFY_SOCKET", sock_path)
            monkeypatch.setenv("WATCHDOG_USEC", str(200_000))   # 0.2s -> ping ~0.1s
            p = watchdog.WatchdogPinger()
            await p.start()
            assert p._task is not None
            await asyncio.sleep(0.25)
            await p.stop()
            assert p._task is None
            # At least one WATCHDOG=1 datagram should have been sent.
            got = b""
            try:
                while True:
                    got += srv.recv(64)
            except BlockingIOError:
                pass
            assert b"WATCHDOG=1" in got
        finally:
            srv.close()
