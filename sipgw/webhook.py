"""Informacast Fusion webhook client.

Handles OAuth2 client credentials authentication and scenario triggering.
Tokens are cached and auto-refreshed before expiry.

Singlewire API pattern:
  Token:   POST {base_url}/token  (form: grant_type, client_id, client_secret, audience)
  Trigger: POST {base_url}/v1/scenario-notifications?scenarioId={id}
           Header: Authorization: Bearer {token}
           Body (JSON): {"fields": [{"fieldId": "<uuid>", "answer": "<tts text>"}]}
"""

import asyncio
import json
import time
import logging
import httpx
from typing import Optional, Tuple
from dataclasses import dataclass

from .config import FusionConfig

logger = logging.getLogger("sipgw.webhook")
api_debug = logging.getLogger("sipgw.api_debug")


@dataclass
class TokenInfo:
    """Cached OAuth2 token."""
    access_token: str
    expires_at: float  # unix timestamp


def _log_request(response: httpx.Response, label: str, mask_secrets: bool = True) -> None:
    """Log full details of an HTTP request and its response.

    Captures everything from the httpx Response object including the
    original Request, any redirect history, headers in both directions,
    and full bodies.
    """
    req = response.request

    api_debug.info("=" * 72)
    api_debug.info("%s", label)
    api_debug.info("=" * 72)

    # --- Request ---
    api_debug.info("REQUEST")
    api_debug.info("  Method:  %s", req.method)
    api_debug.info("  URL:     %s", req.url)

    # Request headers
    req_headers = dict(req.headers)
    if mask_secrets and "authorization" in req_headers:
        auth = req_headers["authorization"]
        if auth.lower().startswith("bearer ") and len(auth) > 27:
            req_headers["authorization"] = auth[:27] + "..."
    api_debug.info("  Headers:")
    for k, v in req_headers.items():
        api_debug.info("    %s: %s", k, v)

    # Request body
    if req.content:
        try:
            body_text = req.content.decode("utf-8", errors="replace")
        except Exception:
            body_text = repr(req.content[:500])
        # Mask client_secret in form-encoded bodies
        if mask_secrets and "client_secret=" in body_text:
            import re
            body_text = re.sub(
                r"(client_secret=)[^&]+",
                lambda m: m.group(1) + m.group(0)[len(m.group(1)):len(m.group(1))+8] + "***",
                body_text,
            )
        api_debug.info("  Body:    %s", body_text)
    else:
        api_debug.info("  Body:    (empty)")

    # --- Redirect history ---
    if response.history:
        api_debug.info("REDIRECTS (%d)", len(response.history))
        for i, redir in enumerate(response.history):
            api_debug.info("  [%d] %s %s -> %s %s",
                           i + 1, redir.request.method, redir.request.url,
                           redir.status_code, redir.headers.get("location", ""))

    # --- Response ---
    api_debug.info("RESPONSE")
    api_debug.info("  Status:  %s %s", response.status_code, response.reason_phrase)
    api_debug.info("  Headers:")
    for k, v in response.headers.items():
        api_debug.info("    %s: %s", k, v)

    # Response body
    resp_body = response.text
    if mask_secrets and resp_body:
        # Mask access tokens in JSON responses
        try:
            resp_json = json.loads(resp_body)
            if isinstance(resp_json, dict) and "access_token" in resp_json:
                resp_json["access_token"] = resp_json["access_token"][:20] + "..."
                resp_body = json.dumps(resp_json, indent=2)
        except (json.JSONDecodeError, TypeError):
            pass
    api_debug.info("  Body:    %s", resp_body if resp_body else "(empty)")

    api_debug.info("-" * 72)


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a Retry-After header.

    Only the delta-seconds form is honored. The HTTP-date form returns None so
    the delivery worker falls back to its exponential backoff (runbook note).
    """
    if not value:
        return None
    try:
        secs = float(value.strip())
    except (TypeError, ValueError):
        return None
    return secs if secs >= 0 else None


class FusionWebhook:
    """Client for Informacast Fusion Scenarios API with OAuth2 auth."""

    def __init__(self, config: FusionConfig):
        self.config = config
        self._token: Optional[TokenInfo] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._field_id: Optional[str] = config.scenario_field_id or None
        self._token_lock = asyncio.Lock()
        self._dry_run: bool = False
        self._transport = None  # NoSendGuardTransport when dry-run is active
        # Delta-seconds parsed from the last response's Retry-After header (or
        # None). Read by the #2 delivery worker to schedule the next attempt.
        self.last_retry_after: Optional[float] = None

    async def initialize(self) -> None:
        """Create the HTTP client.

        In effective dry-run, install the NoSendGuardTransport so no request can
        reach a non-127.0.0.1 host. This is the structural NO-SEND guarantee that
        covers every Fusion origin sharing this client.
        """
        from .safety import NoSendGuardTransport, effective_dry_run, DRY_RUN_BANNER

        self._dry_run = effective_dry_run(getattr(self.config, "dry_run", False))
        self._transport = NoSendGuardTransport() if self._dry_run else None

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            transport=self._transport,  # None -> httpx default transport
        )
        if self._dry_run:
            logger.critical(DRY_RUN_BANNER)
        logger.info(
            f"Webhook client initialized for {self.config.base_url}"
            f"{' [DRY-RUN: no-send guard active]' if self._dry_run else ''}"
        )

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _get_token(self) -> str:
        """Get a valid OAuth2 access token, refreshing if needed."""
        # Return cached token if still valid (with 60s buffer)
        if self._token and self._token.expires_at > time.time() + 60:
            api_debug.debug("Using cached token (expires in %ds)",
                            int(self._token.expires_at - time.time()))
            return self._token.access_token

        async with self._token_lock:
            # Re-check after acquiring lock (another coroutine may have refreshed)
            if self._token and self._token.expires_at > time.time() + 60:
                return self._token.access_token

            logger.info("Fetching new OAuth2 token")

            try:
                req_data = {
                    "grant_type": "client_credentials",
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                }
                if self.config.audience:
                    req_data["audience"] = self.config.audience

                response = await self._client.post(
                    self.config.token_url,
                    data=req_data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )

                _log_request(response, "OAUTH2 TOKEN EXCHANGE")

                response.raise_for_status()
                try:
                    data = response.json()
                except (json.JSONDecodeError, ValueError):
                    logger.error("Token response is not valid JSON: %s", response.text[:200])
                    raise ValueError(f"Token endpoint returned non-JSON response: {response.text[:200]}")

                if "access_token" not in data:
                    logger.error("Token response missing access_token: %s", data)
                    raise ValueError(f"Token response missing access_token key")

                self._token = TokenInfo(
                    access_token=data["access_token"],
                    expires_at=time.time() + data.get("expires_in", 3600),
                )
                logger.info(f"OAuth2 token acquired, expires in {data.get('expires_in', 3600)}s")
                return self._token.access_token

            except httpx.HTTPStatusError as e:
                body = e.response.text[:500] if e.response else ""
                logger.error(f"Failed to get OAuth2 token: HTTP {e.response.status_code} - {body[:200]}")
                raise
            except Exception as e:
                api_debug.error("TOKEN REQUEST EXCEPTION: %s", e, exc_info=True)
                logger.error(f"Failed to get OAuth2 token: {e}")
                raise

    async def _resolve_field_id(self) -> str:
        """Resolve the scenario field ID for the configured variable name.

        Fetches the scenario definition from the API to find the field UUID
        matching config.variable_name. Caches the result.
        """
        if self._field_id:
            return self._field_id

        token = await self._get_token()
        scenario_url = (
            self.config.base_url.rstrip("/")
            + "/v1/scenarios/"
            + self.config.scenario_id
        )

        logger.info(f"Resolving field ID for variable '{self.config.variable_name}'")
        response = await self._client.get(
            scenario_url,
            headers={"Authorization": f"Bearer {token}"},
        )

        _log_request(response, "SCENARIO FIELD LOOKUP")

        response.raise_for_status()
        scenario = response.json()

        for field in scenario.get("fields", []):
            if field.get("variable") == self.config.variable_name:
                self._field_id = field["id"]
                logger.info(
                    f"Resolved field '{self.config.variable_name}' -> {self._field_id}"
                )
                return self._field_id

        # In dry-run the scenario body is synthetic (from NoSendGuardTransport)
        # and will not contain the configured variable. Accept the synthetic
        # field id so the no-send path completes; this branch never runs in prod
        # (dry-run off, real scenario carries the real field).
        if self._dry_run:
            fields = scenario.get("fields") or [{}]
            self._field_id = fields[0].get("id", "DRYRUN.no-send.field-id")
            logger.info("[dry-run] using synthetic field id %s", self._field_id)
            return self._field_id

        raise ValueError(
            f"Scenario {self.config.scenario_id} has no field "
            f"with variable '{self.config.variable_name}'"
        )

    async def trigger_scenario(self, tts_text: str) -> Tuple[int, float]:
        """Trigger the Fusion scenario with the TTS text.

        Singlewire pattern: POST /v1/scenario-notifications?scenarioId={id}
        with Bearer token in header and field answers in JSON body.

        Args:
            tts_text: The announcement string to send.

        Returns:
            Tuple of (HTTP status code, response time in ms).
            On error, status code is -1.
        """
        if not self._client:
            await self.initialize()

        start_time = time.monotonic()
        self.last_retry_after = None
        url = self.config.base_url.rstrip("/") + self.config.scenario_endpoint

        try:
            token = await self._get_token()
            field_id = await self._resolve_field_id()

            # Singlewire: scenarioId as query param, field answers in JSON body
            params = {"scenarioId": self.config.scenario_id}
            payload = {"fields": [{"fieldId": field_id, "answer": tts_text}]}

            response = await self._client.post(
                url,
                params=params,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )

            elapsed_ms = (time.monotonic() - start_time) * 1000
            _log_request(response, f"SCENARIO TRIGGER (elapsed={elapsed_ms:.1f}ms)")

            logger.info(
                f"Fusion webhook response: status={response.status_code} "
                f"time={elapsed_ms:.1f}ms url={url}?scenarioId={self.config.scenario_id}"
            )
            if response.status_code >= 400:
                logger.warning(f"Fusion response body: {response.text[:500]}")

            if response.status_code == 401:
                # Token might have expired, clear cache and retry once
                logger.warning("Got 401, clearing token cache and retrying")
                api_debug.warning("Got 401 — clearing token cache, will re-authenticate and retry")
                self._token = None
                token = await self._get_token()

                response = await self._client.post(
                    url,
                    params=params,
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                )
                elapsed_ms = (time.monotonic() - start_time) * 1000
                _log_request(response, f"SCENARIO TRIGGER RETRY (elapsed={elapsed_ms:.1f}ms)")

                logger.info(f"Fusion webhook retry: status={response.status_code}")

            # Surface Retry-After (delta-seconds only) for the delivery worker.
            self.last_retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            return response.status_code, elapsed_ms

        except Exception as e:
            elapsed_ms = (time.monotonic() - start_time) * 1000
            api_debug.error("SCENARIO TRIGGER EXCEPTION (elapsed=%.1fms): %s", elapsed_ms, e, exc_info=True)
            logger.error(f"Fusion webhook error: {e} (elapsed={elapsed_ms:.1f}ms)")
            return -1, elapsed_ms
