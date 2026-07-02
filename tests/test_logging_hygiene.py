"""#11 logging hygiene: BYE Via transport, credential masking, exception types."""

import logging

import httpx
import pytest
from unittest.mock import AsyncMock

from sipgw.config import AppConfig
from sipgw.sip_server import SIPServer, ActiveCall
from sipgw.webhook import _log_request


def _server() -> SIPServer:
    return SIPServer(config=AppConfig(), on_call=AsyncMock())


def _call(protocol_type: str) -> ActiveCall:
    return ActiveCall(
        call_id="c1", from_tag="ft", to_tag="tt",
        from_header='"Code Blue" <sip:a730r201@10.0.0.9>;tag=ft',
        to_header="<sip:gw@10.0.0.1>", via_headers=["SIP/2.0/UDP 10.0.0.9"],
        remote_addr=("10.0.0.9", 5060), remote_rtp_addr=("10.0.0.9", 40000),
        local_rtp_port=10000, caller_user="a730r201",
        caller_display_name="Code Blue", transport=None, protocol_type=protocol_type,
    )


class TestByeViaTransport:
    def test_udp_call_bye_via_is_udp(self):
        bye = _server()._build_bye(_call("udp")).decode()
        via = [ln for ln in bye.split("\r\n") if ln.startswith("Via:")][0]
        assert "SIP/2.0/UDP" in via

    def test_tcp_call_bye_via_is_tcp(self):
        bye = _server()._build_bye(_call("tcp")).decode()
        via = [ln for ln in bye.split("\r\n") if ln.startswith("Via:")][0]
        assert "SIP/2.0/TCP" in via
        assert "SIP/2.0/UDP" not in via


class TestCredentialMasking:
    def test_token_body_masks_client_secret_and_id(self, caplog):
        # A token POST body carrying both credentials must be masked in api_debug.
        req = httpx.Request(
            "POST", "https://api.icmobile.singlewire.com/api/token",
            data={"grant_type": "client_credentials",
                  "client_id": "6ZBFCWBSJNTFSRNTXBQQ4BFQWOWD2IEB",
                  "client_secret": "OV7XUELSF3H5EBN23SFJRJVXTVR6PGOA24AVX"},
        )
        resp = httpx.Response(200, json={"ok": True}, request=req)
        with caplog.at_level(logging.INFO, logger="sipgw.api_debug"):
            _log_request(resp, "TOKEN TEST")
        text = caplog.text
        # Full secret and full client_id must NOT appear.
        assert "OV7XUELSF3H5EBN23SFJRJVXTVR6PGOA24AVX" not in text
        assert "6ZBFCWBSJNTFSRNTXBQQ4BFQWOWD2IEB" not in text
        # No portion of the credentials may survive: not even the 4-char prefix.
        assert "OV7X***" not in text
        assert "OV7X" not in text
        assert "6ZBF***" not in text
        assert "6ZBF" not in text
        # Credentials are fully redacted to the literal `=***` form.
        assert "client_secret=***" in text
        assert "client_id=***" in text
