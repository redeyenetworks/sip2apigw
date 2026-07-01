"""Safety mechanisms for sipgw dry-run / test mode (reliability release §2).

This module is the load-bearing NO-SEND guarantee. In effective dry-run, the
shared httpx client is built with ``NoSendGuardTransport``, which refuses to
forward ANY request whose host is not 127.0.0.1 — it records the attempt and
returns a synthetic response instead. Because every Fusion origin
(``_get_token``, ``_resolve_field_id``, ``trigger_scenario``, and any future
keepalive) and the escalation POST share this client, none of them can reach a
real host during development or testing. The guarantee is structural, enforced
at the lowest layer, not by discipline at each call site.

Effective dry-run = ``config.dry_run`` OR env ``SIPGW_DRY_RUN == "1"``.
Per the runbook, the environment may only ENABLE dry-run, never disable it.
"""

import os
import logging
from typing import List, Optional, Tuple

import httpx

logger = logging.getLogger("sipgw.safety")
api_debug = logging.getLogger("sipgw.api_debug")

# The ONLY host that may receive real network traffic while dry-run is active.
# The local mock server (§2c) binds here; everything else is refused.
ALLOWED_HOSTS = frozenset({"127.0.0.1"})

DRY_RUN_BANNER = "*** DRY-RUN: NO NOTIFICATIONS WILL BE SENT ***"


def effective_dry_run(config_dry_run: bool) -> bool:
    """Return True if dry-run is active.

    Dry-run is active when the config flag enables it OR when the environment
    variable ``SIPGW_DRY_RUN`` equals ``"1"``. The environment can only turn
    dry-run ON — no value of the env var can force real sending when the config
    has it enabled. There is deliberately no code path that lets an env var
    disable dry-run.
    """
    return bool(config_dry_run) or os.environ.get("SIPGW_DRY_RUN", "") == "1"


class NoSendGuardTransport(httpx.AsyncBaseTransport):
    """An httpx transport that blocks all traffic to non-127.0.0.1 hosts.

    Requests to an allowed host (127.0.0.1) are forwarded to a real inner
    transport so local mock-server drills work. Every other request is refused:
    the attempt is recorded in ``blocked`` and a synthetic response is returned.
    The network is never touched for a refused request.

    ``forwarded`` and ``blocked`` are exposed so tests can assert, directly and
    independently of logging, that zero requests of any method reached a real
    host.
    """

    def __init__(self, inner: Optional[httpx.AsyncBaseTransport] = None):
        self._inner = inner if inner is not None else httpx.AsyncHTTPTransport()
        self.blocked: List[Tuple[str, str]] = []    # (method, url) refused
        self.forwarded: List[Tuple[str, str]] = []   # (method, url) really sent

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        method = request.method
        url = str(request.url)

        if host in ALLOWED_HOSTS:
            self.forwarded.append((method, url))
            return await self._inner.handle_async_request(request)

        # Refuse. Record, log, and synthesize — never touch the network.
        self.blocked.append((method, url))
        api_debug.info("DRY-RUN blocked %s %s", method, url)
        return self._synthetic(request)

    @staticmethod
    def _synthetic(request: httpx.Request) -> httpx.Response:
        """Return a benign synthetic response so the call site completes.

        Shapes are chosen so existing Fusion code paths parse successfully:
        token -> 200 with an access_token; scenario GET -> 200 with a field the
        dry-run resolver accepts; escalation -> 204; scenario trigger / anything
        else -> 200 with a minimal notification body. Every synthetic response
        carries ``x-sipgw-dryrun: 1`` so log formatting can stay terse.
        """
        path = request.url.path
        headers = {"x-sipgw-dryrun": "1"}

        if "escalation" in path:
            return httpx.Response(204, headers=headers, request=request)

        if path.endswith("/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": "DRYRUN.no-send.token",
                    "token_type": "bearer",
                    "expires_in": 3600,
                    "scope": "urn:singlewire:scenario-notifications:write",
                },
                headers=headers,
                request=request,
            )

        if "/v1/scenarios/" in path and request.method == "GET":
            scenario_id = path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "id": scenario_id,
                    "fields": [{
                        "variable": "__DRYRUN__",
                        "name": "__DRYRUN__",
                        "id": "DRYRUN.no-send.field-id",
                    }],
                },
                headers=headers,
                request=request,
            )

        return httpx.Response(
            200,
            json={"events": [{"notification": {"id": "DRYRUN.no-send.notification"}}]},
            headers=headers,
            request=request,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


# ---------------------------------------------------------------------------
# §2b  [TEST] log marker  +  production-DB hard barrier
# ---------------------------------------------------------------------------

# Loggers whose handlers must carry the [TEST] marker so all three streams
# (console/file, api_debug file, sip_debug file) are marked in dry-run/test.
_MARKED_LOGGERS = ("sipgw", "sipgw.api_debug", "sipgw.sip_debug")

# The production database. In dry-run/test we refuse to start if configured to
# use it, so no test artifact can ever land in the prod DB.
PROD_DB_PATH = "/var/lib/sipgw/calls.db"


class ProdDatabaseBarrier(RuntimeError):
    """Raised to abort startup when dry-run/test would use the production DB."""


class TestMarkerFilter(logging.Filter):
    """Prefix every record with ``[TEST] `` exactly once.

    Installed on the sipgw loggers and their handlers while dry-run/test is
    active so no test-produced log line is mistaken for a real one. Idempotent
    per record via a guard attribute, so a record passing a logger filter and a
    handler filter is only marked once.
    """

    PREFIX = "[TEST] "
    __test__ = False  # not a pytest test class despite the name

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "_sipgw_test_marked", False):
            record.msg = f"{self.PREFIX}{record.msg}"
            record._sipgw_test_marked = True
        return True


def _attach_marker_once(target) -> None:
    for existing in getattr(target, "filters", []):
        if isinstance(existing, TestMarkerFilter):
            return
    target.addFilter(TestMarkerFilter())


def install_test_marker(*logger_names: str) -> None:
    """Attach the [TEST] marker to the given loggers and their handlers.

    Defaults to the three sipgw log streams. Attaching to both the logger and
    its handlers covers records logged directly and records propagated from
    child loggers (e.g. ``sipgw.webhook`` -> ``sipgw`` handlers).
    """
    names = logger_names or _MARKED_LOGGERS
    for name in names:
        lg = logging.getLogger(name)
        _attach_marker_once(lg)
        for handler in lg.handlers:
            _attach_marker_once(handler)


def assert_safe_database_path(db_path: str, dry_run: bool) -> None:
    """Hard barrier: never let dry-run/test touch the production database.

    If dry-run/test is active and ``db_path`` resolves to the production DB,
    abort startup. Staging config MUST set a staging-only ``database.path``.
    A no-op when dry-run is off (production running normally uses the prod path).
    """
    if not dry_run:
        return
    real = os.path.realpath(os.path.abspath(db_path))
    prod = os.path.realpath(PROD_DB_PATH)
    if real == prod:
        raise ProdDatabaseBarrier(
            "Refusing to start: dry-run/test mode is active but database.path "
            f"resolves to the PRODUCTION database ({prod}). Set a staging-only "
            "database.path in the staging config."
        )
