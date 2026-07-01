"""#8 systemd Type=notify + watchdog integration (pure-python sd_notify).

Structurally inert when NOTIFY_SOCKET is unset — tests, dry-run, non-systemd
runs, and the rollback single-service topology all behave exactly as before.
Watchdog pings prove EVENT-LOOP liveness only (decoupled from DB writes), so
transient DB slowness never restarts the life-safety pager.
"""

import asyncio
import logging
import os
import socket
from typing import Optional

logger = logging.getLogger("sipgw.watchdog")


def _send(msg: str) -> bool:
    """Send an sd_notify datagram. No-op (returns False) without NOTIFY_SOCKET."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    # Linux abstract-namespace sockets are given as '@/org/...'.
    path = "\0" + addr[1:] if addr.startswith("@") else addr
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(path)
            s.sendall(msg.encode("utf-8"))
        return True
    except Exception as e:  # never let notify failures crash the service
        logger.warning("sd_notify failed (%s): %s", msg, e)
        return False


def notify_ready() -> bool:
    return _send("READY=1")


def notify_watchdog() -> bool:
    return _send("WATCHDOG=1")


def notify_stopping() -> bool:
    return _send("STOPPING=1")


def watchdog_interval_seconds() -> Optional[float]:
    """Half of systemd's WATCHDOG_USEC (the recommended ping cadence), or None."""
    usec = os.environ.get("WATCHDOG_USEC")
    if not usec:
        return None
    try:
        return max(1.0, (int(usec) / 1_000_000.0) / 2.0)
    except ValueError:
        return None


class WatchdogPinger:
    """Pings WATCHDOG=1 on a cadence to prove the event loop is alive.

    Completely inert if systemd did not arm a watchdog (WATCHDOG_USEC unset).
    """

    def __init__(self):
        self._task = None
        self._running = False

    async def start(self) -> None:
        interval = watchdog_interval_seconds()
        if interval is None:
            return  # no systemd watchdog -> stay inert
        self._running = True
        self._task = asyncio.create_task(self._loop(interval))
        logger.info("watchdog pinger started (every %.1fs)", interval)

    async def _loop(self, interval: float) -> None:
        while self._running:
            notify_watchdog()
            await asyncio.sleep(interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
