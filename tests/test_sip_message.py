"""Unit tests for SIP message parser."""

import pytest
from sipgw.sip_message import (
    parse_sip_message,
    build_response,
    parse_sdp_connection,
    parse_sdp_media_port,
)


SAMPLE_INVITE = (
    b"INVITE sip:gateway@10.0.0.1:5060 SIP/2.0\r\n"
    b"Via: SIP/2.0/UDP 172.16.1.100:5060;branch=z9hG4bK776asdhds\r\n"
    b'From: "Code Blue" <sip:a710r201@172.16.1.100>;tag=1928301774\r\n'
    b"To: <sip:gateway@10.0.0.1:5060>\r\n"
    b"Call-ID: a84b4c76e66710@172.16.1.100\r\n"
    b"CSeq: 314159 INVITE\r\n"
    b"Contact: <sip:a710r201@172.16.1.100>\r\n"
    b"Content-Type: application/sdp\r\n"
    b"Content-Length: 142\r\n"
    b"\r\n"
    b"v=0\r\n"
    b"o=- 12345 12345 IN IP4 172.16.1.100\r\n"
    b"s=-\r\n"
    b"c=IN IP4 172.16.1.100\r\n"
    b"t=0 0\r\n"
    b"m=audio 40000 RTP/AVP 0\r\n"
    b"a=rtpmap:0 PCMU/8000\r\n"
    b"a=sendrecv\r\n"
)

SAMPLE_BYE = (
    b"BYE sip:gateway@10.0.0.1:5060 SIP/2.0\r\n"
    b"Via: SIP/2.0/UDP 172.16.1.100:5060;branch=z9hG4bK776xyz\r\n"
    b'From: "Code Blue" <sip:a710r201@172.16.1.100>;tag=1928301774\r\n'
    b"To: <sip:gateway@10.0.0.1:5060>;tag=sipgw-123456\r\n"
    b"Call-ID: a84b4c76e66710@172.16.1.100\r\n"
    b"CSeq: 314160 BYE\r\n"
    b"Content-Length: 0\r\n"
    b"\r\n"
)


class TestParseSIPMessage:
    def test_parse_invite(self):
        msg = parse_sip_message(SAMPLE_INVITE)
        assert msg.is_request is True
        assert msg.method == "INVITE"
        assert msg.request_uri == "sip:gateway@10.0.0.1:5060"

    def test_invite_headers(self):
        msg = parse_sip_message(SAMPLE_INVITE)
        assert msg.get_call_id() == "a84b4c76e66710@172.16.1.100"
        assert msg.get_cseq() == "314159 INVITE"
        assert "Code Blue" in msg.get_from()
        assert "a710r201" in msg.get_from()

    def test_invite_via(self):
        msg = parse_sip_message(SAMPLE_INVITE)
        vias = msg.get_headers("Via")
        assert len(vias) == 1
        assert "172.16.1.100" in vias[0]

    def test_invite_sdp_body(self):
        msg = parse_sip_message(SAMPLE_INVITE)
        assert "m=audio 40000" in msg.body
        assert "c=IN IP4 172.16.1.100" in msg.body

    def test_parse_bye(self):
        msg = parse_sip_message(SAMPLE_BYE)
        assert msg.is_request is True
        assert msg.method == "BYE"

    def test_parse_response(self):
        data = b"SIP/2.0 200 OK\r\nContent-Length: 0\r\n\r\n"
        msg = parse_sip_message(data)
        assert msg.is_request is False
        assert msg.status_code == 200
        assert msg.reason_phrase == "OK"


class TestBuildResponse:
    def test_200_ok(self):
        msg = parse_sip_message(SAMPLE_INVITE)
        response = build_response(msg, 200, "OK", to_tag="sipgw-12345")
        text = response.decode("utf-8")
        assert text.startswith("SIP/2.0 200 OK\r\n")
        assert "Via:" in text
        assert "Call-ID: a84b4c76e66710@172.16.1.100" in text
        assert "CSeq: 314159 INVITE" in text
        assert "tag=sipgw-12345" in text

    def test_100_trying(self):
        msg = parse_sip_message(SAMPLE_INVITE)
        response = build_response(msg, 100, "Trying")
        text = response.decode("utf-8")
        assert "SIP/2.0 100 Trying" in text

    def test_response_with_body(self):
        msg = parse_sip_message(SAMPLE_INVITE)
        sdp = "v=0\r\nm=audio 10000 RTP/AVP 0\r\n"
        response = build_response(msg, 200, "OK", body=sdp, to_tag="test")
        text = response.decode("utf-8")
        assert "Content-Type: application/sdp" in text
        assert f"Content-Length: {len(sdp)}" in text
        assert text.endswith(sdp)


class TestSDPParsing:
    def test_parse_connection(self):
        sdp = "v=0\r\nc=IN IP4 192.168.1.100\r\nm=audio 5000 RTP/AVP 0\r\n"
        assert parse_sdp_connection(sdp) == "192.168.1.100"

    def test_parse_media_port(self):
        sdp = "v=0\r\nc=IN IP4 192.168.1.100\r\nm=audio 40000 RTP/AVP 0\r\n"
        assert parse_sdp_media_port(sdp) == 40000

    def test_parse_no_connection(self):
        sdp = "v=0\r\nm=audio 5000 RTP/AVP 0\r\n"
        assert parse_sdp_connection(sdp) is None

    def test_parse_no_media(self):
        sdp = "v=0\r\nc=IN IP4 192.168.1.100\r\n"
        assert parse_sdp_media_port(sdp) is None
