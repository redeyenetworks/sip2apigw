"""#11 immediate-BYE ACK-gated teardown tests.

These cover the deferred-BYE state machine that closes the 481 race: in
``immediate_bye`` mode the gateway answers (100/200) and KEEPS the call, firing
the page immediately (decoupled) and the gateway BYE only once the caller's ACK
confirms the three-way handshake. A lost ACK is covered by a per-call fallback
timer, and the teardown funnel is single-fire (exactly one BYE, one port-free).

The BYE is also made spec-correct: request-URI == the caller's Contact and the
Route set == the reversed Record-Route (packet is still sent to remote_addr).
"""

import asyncio

import pytest
from unittest.mock import AsyncMock

from sipgw.config import AppConfig
from sipgw.sip_message import parse_sip_message
from sipgw.sip_server import SIPServer

CALL_ID = "A1-B1-EVT7788-C1-0-13c4-764@h"
ADDR = ("172.20.9.170", 5061)


def _invite(call_id=CALL_ID, from_tag="tag1",
            contact="sip:MedW_3404@172.20.9.170:5061",
            record_route=("<sip:172.20.9.176;lr>",),
            remote_ip="172.20.9.170", rtp_port=40000) -> bytes:
    sdp = (f"v=0\r\no=- 1 1 IN IP4 {remote_ip}\r\ns=-\r\n"
           f"c=IN IP4 {remote_ip}\r\nt=0 0\r\n"
           f"m=audio {rtp_port} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n")
    rr = "".join(f"Record-Route: {r}\r\n" for r in record_route)
    contact_line = f"Contact: <{contact}>\r\n" if contact else ""
    msg = (
        f"INVITE sip:gw@172.20.9.176:5060 SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {remote_ip}:5061;branch=z9hG4bK-a\r\n"
        f"{rr}"
        f'From: "Code Blue" <sip:MedW_3404@{remote_ip}>;tag={from_tag}\r\n'
        f"To: <sip:gw@172.20.9.176:5060>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 1 INVITE\r\n"
        f"{contact_line}"
        f"Content-Type: application/sdp\r\nContent-Length: {len(sdp)}\r\n\r\n{sdp}"
    )
    return msg.encode()


def _ack(call_id=CALL_ID, from_tag="tag1") -> bytes:
    msg = (
        f"ACK sip:MedW_3404@172.20.9.170:5061 SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP 172.20.9.170:5061;branch=z9hG4bK-ack\r\n"
        f'From: "Code Blue" <sip:MedW_3404@172.20.9.170>;tag={from_tag}\r\n'
        f"To: <sip:gw@172.20.9.176:5060>;tag=sipgw-x\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 1 ACK\r\nContent-Length: 0\r\n\r\n"
    )
    return msg.encode()


def _bye(call_id=CALL_ID, from_tag="tag1") -> bytes:
    msg = (
        f"BYE sip:gw@172.20.9.176:5060 SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP 172.20.9.170:5061;branch=z9hG4bK-bye\r\n"
        f'From: "Code Blue" <sip:MedW_3404@172.20.9.170>;tag={from_tag}\r\n'
        f"To: <sip:gw@172.20.9.176:5060>;tag=sipgw-x\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 2 BYE\r\nContent-Length: 0\r\n\r\n"
    )
    return msg.encode()


class FakeTransport:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def write(self, data):
        self.sent.append((data, None))


def _config(immediate_bye=True, ack_timeout=100.0) -> AppConfig:
    config = AppConfig()
    config.sip.immediate_bye = immediate_bye
    config.sip.rtp_port_range_start = 40000
    config.sip.rtp_port_range_end = 40100
    config.sip.immediate_bye_ack_timeout_seconds = ack_timeout
    return config


def _tokens(sent):
    """Return the list of request methods / response codes for what was sent."""
    out = []
    for data, _ in sent:
        m = parse_sip_message(data)
        out.append(m.method if m.is_request else m.status_code)
    return out


async def _drain():
    # Let scheduled create_task() coroutines run to completion. The teardown
    # funnel + _terminate_call contain no internal await that yields, so a
    # couple of loop turns are sufficient.
    for _ in range(4):
        await asyncio.sleep(0)


async def _cancel_pending(server):
    for c in list(server.calls.values()):
        t = c.ack_timeout_task
        if t and not t.done():
            t.cancel()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_invite_defers_bye_until_ack():
    # (a) happy path: 200 OK sent, NO BYE in the same tick, call retained, page
    # fired; then the ACK draws exactly one BYE, frees the port, removes the call.
    server = SIPServer(config=_config(), on_call=AsyncMock())
    ft = FakeTransport()
    await server._handle_invite(parse_sip_message(_invite()), ADDR, ft, "udp")
    await _drain()  # let _safe_callback run

    toks = _tokens(ft.sent)
    assert 100 in toks and 200 in toks
    assert "BYE" not in toks                 # deferred — not before the ACK
    assert CALL_ID in server.calls           # call retained during ACK-wait
    assert 40000 in server._rtp_ports_in_use
    server.on_call.assert_awaited_once()     # page fired (decoupled)

    server._handle_ack(parse_sip_message(_ack()), ADDR)
    await _drain()

    toks = _tokens(ft.sent)
    assert toks.count("BYE") == 1            # exactly one gateway BYE
    assert CALL_ID not in server.calls
    assert 40000 not in server._rtp_ports_in_use


@pytest.mark.asyncio
async def test_page_decoupled_from_ack():
    # (b) the page fires even if no ACK ever arrives (fully decoupled).
    server = SIPServer(config=_config(ack_timeout=100.0), on_call=AsyncMock())
    ft = FakeTransport()
    await server._handle_invite(parse_sip_message(_invite()), ADDR, ft, "udp")
    await _drain()
    server.on_call.assert_awaited_once()
    assert "BYE" not in _tokens(ft.sent)     # still waiting on the ACK
    await _cancel_pending(server)


@pytest.mark.asyncio
async def test_lost_ack_fallback_tears_down():
    # (c) a lost ACK must never strand the dialog: the fallback timer fires the
    # BYE and frees the RTP port on its own.
    server = SIPServer(config=_config(ack_timeout=0.01), on_call=AsyncMock())
    ft = FakeTransport()
    await server._handle_invite(parse_sip_message(_invite()), ADDR, ft, "udp")
    await asyncio.sleep(0.05)  # let the fallback fire
    await _drain()

    toks = _tokens(ft.sent)
    assert toks.count("BYE") == 1
    assert CALL_ID not in server.calls
    assert 40000 not in server._rtp_ports_in_use


@pytest.mark.asyncio
async def test_duplicate_ack_single_bye():
    # (d1) two ACKs before teardown runs -> the funnel yields exactly ONE BYE.
    server = SIPServer(config=_config(), on_call=AsyncMock())
    ft = FakeTransport()
    await server._handle_invite(parse_sip_message(_invite()), ADDR, ft, "udp")
    await _drain()

    server._handle_ack(parse_sip_message(_ack()), ADDR)
    server._handle_ack(parse_sip_message(_ack()), ADDR)  # duplicate, same tick
    await _drain()

    assert _tokens(ft.sent).count("BYE") == 1
    assert CALL_ID not in server.calls
    assert 40000 not in server._rtp_ports_in_use


@pytest.mark.asyncio
async def test_ack_then_late_fallback_no_double_bye():
    # (d2) ACK tears the call down; a LATER fallback body (as if the timer had
    # fired anyway) must no-op — the funnel is single-fire.
    server = SIPServer(config=_config(ack_timeout=100.0), on_call=AsyncMock())
    ft = FakeTransport()
    await server._handle_invite(parse_sip_message(_invite()), ADDR, ft, "udp")
    await _drain()
    call = server.calls[CALL_ID]

    server._handle_ack(parse_sip_message(_ack()), ADDR)
    await _drain()
    assert _tokens(ft.sent).count("BYE") == 1

    # Force the teardown funnel again — must not send a second BYE / double-free.
    await server._immediate_bye_teardown(call, "late fallback")
    assert _tokens(ft.sent).count("BYE") == 1


@pytest.mark.asyncio
async def test_teardown_funnel_idempotent_direct():
    # (d3) calling the funnel twice back-to-back yields exactly one BYE.
    server = SIPServer(config=_config(ack_timeout=100.0), on_call=AsyncMock())
    ft = FakeTransport()
    await server._handle_invite(parse_sip_message(_invite()), ADDR, ft, "udp")
    await _drain()
    call = server.calls[CALL_ID]

    await asyncio.gather(
        server._immediate_bye_teardown(call, "race A"),
        server._immediate_bye_teardown(call, "race B"),
    )
    assert _tokens(ft.sent).count("BYE") == 1
    assert 40000 not in server._rtp_ports_in_use


@pytest.mark.asyncio
async def test_retransmit_invite_during_ack_wait_no_double_page():
    # (e) a retransmitted INVITE while we wait for the ACK hits the re-INVITE
    # 200-resend branch: 200 re-sent, on_call NOT invoked a second time.
    server = SIPServer(config=_config(ack_timeout=100.0), on_call=AsyncMock())
    ft = FakeTransport()
    await server._handle_invite(parse_sip_message(_invite()), ADDR, ft, "udp")
    await _drain()
    server.on_call.assert_awaited_once()
    first_200 = _tokens(ft.sent).count(200)

    # Same Call-ID retransmit while the call is still held.
    await server._handle_invite(parse_sip_message(_invite()), ADDR, ft, "udp")
    await _drain()

    assert _tokens(ft.sent).count(200) == first_200 + 1  # 200 re-sent
    server.on_call.assert_awaited_once()                 # NO second page
    assert "BYE" not in _tokens(ft.sent)
    await _cancel_pending(server)


@pytest.mark.asyncio
async def test_peer_bye_during_ack_wait_no_gateway_bye():
    # (f) peer BYE during the ACK-wait terminates cleanly; the deferred gateway
    # BYE is never sent and the fallback timer is cancelled.
    server = SIPServer(config=_config(ack_timeout=100.0), on_call=AsyncMock())
    ft = FakeTransport()
    await server._handle_invite(parse_sip_message(_invite()), ADDR, ft, "udp")
    await _drain()
    call = server.calls[CALL_ID]

    await server._handle_bye(parse_sip_message(_bye()), ADDR, ft, "udp")
    await _drain()

    # We SENT no BYE request (peer sent it); only responses + the 200-for-BYE.
    assert "BYE" not in _tokens(ft.sent)
    assert CALL_ID not in server.calls
    assert 40000 not in server._rtp_ports_in_use
    assert call.ack_timeout_task.cancelled() or call.ack_timeout_task.done()


@pytest.mark.asyncio
async def test_build_bye_uses_contact_and_reversed_record_route():
    # (g) request-URI == captured Contact; Route == reversed Record-Route.
    server = SIPServer(config=_config(ack_timeout=100.0), on_call=AsyncMock())
    ft = FakeTransport()
    rr = ("<sip:172.20.9.176;lr>", "<sip:172.20.9.180;lr>")
    await server._handle_invite(
        parse_sip_message(_invite(record_route=rr,
                                  contact="sip:MedW_3404@172.20.9.170:5061")),
        ADDR, ft, "udp")
    call = server.calls[CALL_ID]

    bye = server._build_bye(call)
    text = bye.decode()
    assert text.split("\r\n", 1)[0] == "BYE sip:MedW_3404@172.20.9.170:5061 SIP/2.0"
    m = parse_sip_message(bye)
    # Reversed: bottom Record-Route becomes the top Route.
    assert m.get_headers("Route") == ["<sip:172.20.9.180;lr>", "<sip:172.20.9.176;lr>"]
    assert "SIP/2.0/UDP" in m.get_header("Via")  # v1.6.0 Via-transport regression
    await _cancel_pending(server)


@pytest.mark.asyncio
async def test_build_bye_falls_back_when_contact_absent():
    # (g) no Contact -> request-URI falls back to From-user@remote_addr.
    server = SIPServer(config=_config(ack_timeout=100.0), on_call=AsyncMock())
    ft = FakeTransport()
    await server._handle_invite(
        parse_sip_message(_invite(contact="")), ADDR, ft, "udp")
    call = server.calls[CALL_ID]

    text = server._build_bye(call).decode()
    assert text.split("\r\n", 1)[0] == \
        "BYE sip:MedW_3404@172.20.9.170:5061 SIP/2.0"
    await _cancel_pending(server)


@pytest.mark.asyncio
async def test_build_bye_tcp_via_transport():
    # (g) TCP call -> Via transport token stays TCP (v1.6.0 regression guard).
    server = SIPServer(config=_config(ack_timeout=100.0), on_call=AsyncMock())
    ft = FakeTransport()
    await server._handle_invite(parse_sip_message(_invite()), ADDR, ft, "tcp")
    call = server.calls[CALL_ID]
    m = parse_sip_message(server._build_bye(call))
    assert "SIP/2.0/TCP" in m.get_header("Via")
    await _cancel_pending(server)


@pytest.mark.asyncio
async def test_non_immediate_bye_ack_does_not_teardown():
    # Guard: in normal (RTP-held) mode an ACK must NOT trigger the immediate-BYE
    # teardown — that path is owned by _call_timeout / peer BYE.
    server = SIPServer(config=_config(immediate_bye=False), on_call=AsyncMock())
    ft = FakeTransport()
    await server._handle_invite(parse_sip_message(_invite()), ADDR, ft, "udp")
    await _drain()
    assert CALL_ID in server.calls
    server._handle_ack(parse_sip_message(_ack()), ADDR)
    await _drain()
    assert CALL_ID in server.calls            # still held
    assert "BYE" not in _tokens(ft.sent)
    # cleanup the held call's RTP + timeout task
    await server._terminate_call(server.calls[CALL_ID], "test cleanup")
