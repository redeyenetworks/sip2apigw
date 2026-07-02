"""#15 INVITE fingerprint tests."""

import asyncio
import re

import pytest
from unittest.mock import AsyncMock

from sipgw.config import AppConfig
from sipgw.lookups import get_call_purpose, load_lookups
from sipgw.sip_message import (
    invite_fingerprint,
    invite_fingerprint_line,
    extract_event_id,
    parse_sdp_session_id,
    parse_sdp_media_port,
    via_hosts,
    parse_sip_message,
)
from sipgw.sip_server import SIPServer

_FP_RE = re.compile(r"^v1:[0-9a-f]{16}$")


def _invite(call_id="c1@h", from_user="a730r201", from_tag="tag1",
            cseq="1 INVITE", via_branch="z9hG4bK-aaa", contact="a730r201@h:5061") -> bytes:
    sdp = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\n"
           "t=0 0\r\nm=audio 40000 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n")
    msg = (
        f"INVITE sip:gw@127.0.0.1:5060 SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP 127.0.0.1:5061;branch={via_branch}\r\n"
        f'From: "Code Blue" <sip:{from_user}@127.0.0.1>;tag={from_tag}\r\n'
        f"To: <sip:gw@127.0.0.1:5060>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq}\r\n"
        f"Contact: <sip:{contact}>\r\n"
        f"Content-Type: application/sdp\r\nContent-Length: {len(sdp)}\r\n\r\n{sdp}"
    )
    return msg.encode()


def _fp(**kw) -> str:
    return invite_fingerprint(parse_sip_message(_invite(**kw)))


class TestFingerprint:
    def test_format_contract(self):
        assert _FP_RE.match(_fp())

    def test_deterministic_same_bytes(self):
        data = _invite()
        assert invite_fingerprint(parse_sip_message(data)) == \
               invite_fingerprint(parse_sip_message(data))

    def test_retransmit_stable(self):
        # Identical INVITE bytes -> identical fingerprint.
        assert _fp() == _fp()

    def test_call_id_changes_fp(self):
        assert _fp(call_id="c1@h") != _fp(call_id="c2@h")

    def test_from_user_changes_fp(self):
        assert _fp(from_user="a730r201") != _fp(from_user="a731r400")

    def test_from_tag_changes_fp(self):
        assert _fp(from_tag="tag1") != _fp(from_tag="tag2")

    def test_via_and_contact_excluded(self):
        # Changing ONLY Via/branch or Contact must NOT change the fingerprint.
        base = _fp()
        assert _fp(via_branch="z9hG4bK-different") == base
        assert _fp(contact="a730r201@other-host:9999") == base

    def test_robust_on_missing_fields(self):
        # An INVITE missing Call-ID/From/CSeq still returns a stable string.
        raw = ("INVITE sip:gw SIP/2.0\r\nVia: SIP/2.0/UDP h;branch=x\r\n"
               "To: <sip:gw>\r\nContent-Length: 0\r\n\r\n").encode()
        fp = invite_fingerprint(parse_sip_message(raw))
        assert _FP_RE.match(fp)


# --------------------------------------------------------------------------- #
# #15 structured INVITE fingerprint line + upstream event-id extraction.
# --------------------------------------------------------------------------- #

_LINE_PREFIX = "INVITE fingerprint: "
_TOKEN_RE = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S*)')


def _rauland_invite(call_id, from_tag="tag1", display="Code Blue",
                    user="MedW_3404", rtp_port=40000,
                    vias=("172.20.9.176", "172.20.9.170"),
                    sess_id="998877", remote_ip="172.20.9.170",
                    server_ip="172.20.9.176", server_port=5060) -> bytes:
    """A Rauland-shaped inbound INVITE with an explicit Via chain (top-to-bottom
    on the wire) and an SDP o=/m= pair."""
    sdp = (
        f"v=0\r\no=- {sess_id} 2 IN IP4 {remote_ip}\r\ns=-\r\n"
        f"c=IN IP4 {remote_ip}\r\nt=0 0\r\n"
        f"m=audio {rtp_port} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n"
    )
    via_lines = "".join(
        f"Via: SIP/2.0/UDP {h};branch=z9hG4bK-{i}\r\n" for i, h in enumerate(vias)
    )
    msg = (
        f"INVITE sip:gw@{server_ip}:{server_port} SIP/2.0\r\n"
        f"{via_lines}"
        f'From: "{display}" <sip:{user}@{remote_ip}>;tag={from_tag}\r\n'
        f"To: <sip:gw@{server_ip}:{server_port}>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 1 INVITE\r\n"
        f"Contact: <sip:{user}@{remote_ip}:5061>\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {len(sdp)}\r\n\r\n{sdp}"
    )
    return msg.encode("utf-8")


def _build_line(raw: bytes, addr=("172.20.9.170", 5060)) -> str:
    msg = parse_sip_message(raw)
    port = parse_sdp_media_port(msg.body) or 0
    sess = parse_sdp_session_id(msg.body)
    return invite_fingerprint_line(msg, addr, port, sess)


def _fields(line: str) -> dict:
    """Parse a 'INVITE fingerprint: k=v ...' logfmt line into a dict."""
    assert line.startswith(_LINE_PREFIX), line
    body = line[len(_LINE_PREFIX):]
    out = {}
    for k, v in _TOKEN_RE.findall(body):
        if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
            v = v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        out[k] = v
    return out


class TestEventIdExtraction:
    def test_real_rauland_call_id(self):
        # <a>-<b>-<event>-<d>-0-13c4-764 -> segment 3 is the event id.
        assert extract_event_id("A1-B1-EVT7788-C1-0-13c4-764@host") == "EVT7788"

    def test_short_call_id_is_empty(self):
        assert extract_event_id("shortid") == ""
        assert extract_event_id("a-b") == ""

    def test_never_raises_on_garbage(self):
        for bad in (None, "", "---", "-", 12345):
            # extract_event_id tolerates non-str / degenerate input.
            assert isinstance(extract_event_id(bad), str)  # type: ignore[arg-type]


class TestFingerprintLine:
    def test_duplicate_pair_same_event_distinct_identity(self):
        # (1) 2026-07-01 MedW_3404 duplicate pair: same upstream event id, but
        # distinct Call-ID and From-tag, RTP ports 4 apart, Via origin>..>us.
        raw1 = _rauland_invite(call_id="A1-B1-EVT7788-C1-0-13c4-764@h",
                               from_tag="tagAAAA", rtp_port=40000)
        raw2 = _rauland_invite(call_id="A2-B2-EVT7788-C2-0-13c4-764@h",
                               from_tag="tagBBBB", rtp_port=40004)
        f1, f2 = _fields(_build_line(raw1)), _fields(_build_line(raw2))

        assert f1["event_id"] == f2["event_id"] == "EVT7788"
        assert f1["call_id"] != f2["call_id"]
        assert f1["from_tag"] != f2["from_tag"]
        assert int(f2["rtp_port"]) - int(f1["rtp_port"]) == 4
        assert f1["via"] == f2["via"] == "172.20.9.170>172.20.9.176"

    def test_escalation_pair_differs_and_no_false_merge(self):
        # (2) 2026-06-12 room-4706 RRT -> Code Blue escalation: different event
        # id AND different derived purpose -> must never be merged as a dup.
        load_lookups()  # reads SIPGW_LOOKUPS; purpose map for get_call_purpose
        rrt = _rauland_invite(call_id="R1-X-EVT1000-Y-0-a-b",
                              display="RRT Alert", user="room4706")
        blue = _rauland_invite(call_id="R2-X-EVT2000-Y-0-a-b",
                               display="Code Blue", user="room4706")
        f_rrt, f_blue = _fields(_build_line(rrt)), _fields(_build_line(blue))

        assert f_rrt["event_id"] == "EVT1000"
        assert f_blue["event_id"] == "EVT2000"
        assert f_rrt["event_id"] != f_blue["event_id"]
        assert get_call_purpose(f_rrt["display"]) == "Rapid Response Team"
        assert get_call_purpose(f_blue["display"]) == "Code Blue"
        assert get_call_purpose(f_rrt["display"]) != get_call_purpose(f_blue["display"])

    def test_short_call_id_line_still_built_event_empty(self):
        # (3) malformed/short Call-ID -> event_id empty, no exception, full line.
        line = _build_line(_rauland_invite(call_id="noHyphensHere@h"))
        f = _fields(line)
        assert f["event_id"] == ""
        assert f["call_id"] == "noHyphensHere@h"
        # All nine identity fields plus the transaction fp are present.
        for key in ("call_id", "event_id", "from_tag", "caller", "display",
                    "src", "via", "sdp_session", "rtp_port", "fp"):
            assert key in f, key
        assert _FP_RE.match(f["fp"])

    def test_script_display_logged_verbatim(self):
        # (4) a From display of "<script>" is emitted literally, no throw, no
        # escaping (this is a log line, not HTML).
        line = _build_line(_rauland_invite(call_id="S1-S2-EVT9-S4-0-a-b",
                                           display="<script>"))
        assert "<script>" in line
        assert _fields(line)["display"] == "<script>"

    def test_sess_id_and_src_captured(self):
        f = _fields(_build_line(_rauland_invite(call_id="A-B-EVT1-D-0-a-b",
                                                sess_id="55501")))
        assert f["sdp_session"] == "55501"
        assert f["src"] == "172.20.9.170:5060"

    def test_builder_never_raises_on_garbage_message(self):
        # A near-empty message must still yield a prefixed line, never raise.
        msg = parse_sip_message(b"INVITE sip:gw SIP/2.0\r\nContent-Length: 0\r\n\r\n")
        line = invite_fingerprint_line(msg, ("10.0.0.1", 5060), 0, "")
        assert line.startswith(_LINE_PREFIX)
        assert _fields(line)["event_id"] == ""

    def test_via_hosts_top_to_bottom(self):
        msg = parse_sip_message(_rauland_invite(call_id="A-B-EVT1-D-0-a-b"))
        # Wire order is topmost-first; the line reverses it to origin>..>us.
        assert via_hosts(msg) == ["172.20.9.176", "172.20.9.170"]


class TestFingerprintOutageSafety:
    @pytest.mark.asyncio
    async def test_fingerprint_failure_never_aborts_answer(self, monkeypatch):
        # (5) Force the builder to raise; _handle_invite must still send
        # 100 Trying + 200 OK and invoke on_call — the failure is swallowed.
        import sipgw.sip_server as sip_server_mod

        def _boom(*a, **k):
            raise RuntimeError("fingerprint line blew up")

        monkeypatch.setattr(sip_server_mod, "invite_fingerprint_line", _boom)

        config = AppConfig()
        config.sip.immediate_bye = True  # no RTP/timeout tasks to leak
        config.sip.rtp_port_range_start = 40000
        config.sip.rtp_port_range_end = 40100
        callback = AsyncMock()
        server = SIPServer(config=config, on_call=callback)

        sent = []

        class FakeTransport:
            def sendto(self, data, addr):
                sent.append(data)

        raw = _rauland_invite(call_id="Z1-Z2-EVT42-Z4-0-13c4-764@h",
                              from_tag="tZ")
        msg = parse_sip_message(raw)
        await server._handle_invite(msg, ("172.20.9.170", 5060),
                                    FakeTransport(), "udp")
        await asyncio.sleep(0)  # let the _safe_callback task run

        statuses = [parse_sip_message(d).status_code for d in sent]
        assert 100 in statuses, statuses
        assert 200 in statuses, statuses
        callback.assert_awaited()
