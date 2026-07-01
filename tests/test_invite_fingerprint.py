"""#15 INVITE fingerprint tests."""

import re

from sipgw.sip_message import invite_fingerprint, parse_sip_message

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
