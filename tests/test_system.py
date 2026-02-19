"""System-level tests.

These tests verify the SIP server behavior using actual UDP sockets.
They send SIP messages to the server and verify responses.

Uses asyncio sockets so the event loop isn't blocked during send/recv.
"""

import asyncio
import socket
import pytest
import os
from unittest.mock import AsyncMock

from sipgw.config import AppConfig
from sipgw.sip_server import SIPServer
from sipgw.sip_message import parse_sip_message


def find_free_port() -> int:
    """Find a free UDP port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def build_invite(
    user: str = "a730r201",
    display_name: str = "Code Blue",
    remote_ip: str = "127.0.0.1",
    remote_port: int = 5061,
    server_ip: str = "127.0.0.1",
    server_port: int = 5060,
    call_id: str = "test-call-001",
    rtp_port: int = 40000,
) -> bytes:
    """Build a SIP INVITE for testing."""
    sdp = (
        f"v=0\r\n"
        f"o=- 1 1 IN IP4 {remote_ip}\r\n"
        f"s=-\r\n"
        f"c=IN IP4 {remote_ip}\r\n"
        f"t=0 0\r\n"
        f"m=audio {rtp_port} RTP/AVP 0\r\n"
        f"a=rtpmap:0 PCMU/8000\r\n"
    )
    msg = (
        f"INVITE sip:gateway@{server_ip}:{server_port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {remote_ip}:{remote_port};branch=z9hG4bKtest{call_id}\r\n"
        f'From: "{display_name}" <sip:{user}@{remote_ip}>;tag=tag{call_id}\r\n'
        f"To: <sip:gateway@{server_ip}:{server_port}>\r\n"
        f"Call-ID: {call_id}@{remote_ip}\r\n"
        f"CSeq: 1 INVITE\r\n"
        f"Contact: <sip:{user}@{remote_ip}:{remote_port}>\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {len(sdp)}\r\n"
        f"\r\n"
        f"{sdp}"
    )
    return msg.encode("utf-8")


def build_options(
    remote_ip: str = "127.0.0.1",
    remote_port: int = 5061,
    server_ip: str = "127.0.0.1",
    server_port: int = 5060,
) -> bytes:
    """Build a SIP OPTIONS for testing."""
    msg = (
        f"OPTIONS sip:gateway@{server_ip}:{server_port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {remote_ip}:{remote_port};branch=z9hG4bKoptions\r\n"
        f"From: <sip:probe@{remote_ip}>;tag=opttest\r\n"
        f"To: <sip:gateway@{server_ip}:{server_port}>\r\n"
        f"Call-ID: options-test@{remote_ip}\r\n"
        f"CSeq: 1 OPTIONS\r\n"
        f"Content-Length: 0\r\n"
        f"\r\n"
    )
    return msg.encode("utf-8")


class UDPTestClient:
    """Async UDP client for sending/receiving SIP messages in tests."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]

    async def send(self, data: bytes, addr: tuple):
        loop = asyncio.get_running_loop()
        await loop.sock_sendto(self.sock, data, addr)

    async def recv(self, timeout: float = 3.0) -> bytes:
        loop = asyncio.get_running_loop()
        try:
            data, addr = await asyncio.wait_for(
                loop.sock_recvfrom(self.sock, 65535),
                timeout=timeout,
            )
            return data
        except asyncio.TimeoutError:
            return None

    async def recv_all(self, count: int = 5, timeout: float = 3.0) -> list:
        """Receive up to count messages within timeout."""
        messages = []
        deadline = asyncio.get_event_loop().time() + timeout
        for _ in range(count):
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            data = await self.recv(timeout=remaining)
            if data is None:
                break
            messages.append(data)
        return messages

    def close(self):
        self.sock.close()


class TestSIPServerSystem:
    """System tests that start a real SIP server on a test port."""

    @pytest.mark.asyncio
    async def test_invite_gets_200_ok(self):
        """Send an INVITE and verify we get a 200 OK response."""
        sip_port = find_free_port()
        rtp_start = find_free_port()
        if rtp_start % 2 != 0:
            rtp_start += 1

        config = AppConfig()
        config.sip.bind_ip = "127.0.0.1"
        config.sip.bind_port = sip_port
        config.sip.allowed_networks = ["127.0.0.0/8"]
        config.sip.call_timeout_seconds = 5
        config.sip.rtp_port_range_start = rtp_start
        config.sip.rtp_port_range_end = rtp_start + 100

        callback = AsyncMock()
        server = SIPServer(config=config, on_call=callback)

        server_task = asyncio.create_task(server.start())
        await asyncio.sleep(0.3)

        client = UDPTestClient()
        try:
            invite = build_invite(
                remote_ip="127.0.0.1",
                remote_port=client.port,
                server_ip="127.0.0.1",
                server_port=sip_port,
            )
            await client.send(invite, ("127.0.0.1", sip_port))

            raw_msgs = await client.recv_all(count=5, timeout=3.0)

            responses = [parse_sip_message(r) for r in raw_msgs]
            status_codes = [r.status_code for r in responses]

            assert 200 in status_codes, f"Expected 200 OK, got status codes: {status_codes}"

            ok_msg = next(r for r in responses if r.status_code == 200)
            assert "m=audio" in ok_msg.body

        finally:
            client.close()
            await server.stop()
            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_unauthorized_ip_gets_403(self):
        """Send from 127.0.0.1 when only 172.16/12 is allowed."""
        sip_port = find_free_port()

        config = AppConfig()
        config.sip.bind_ip = "127.0.0.1"
        config.sip.bind_port = sip_port
        config.sip.allowed_networks = ["172.16.0.0/12"]
        config.sip.call_timeout_seconds = 5
        config.sip.rtp_port_range_start = 14000
        config.sip.rtp_port_range_end = 14100

        callback = AsyncMock()
        server = SIPServer(config=config, on_call=callback)

        server_task = asyncio.create_task(server.start())
        await asyncio.sleep(0.3)

        client = UDPTestClient()
        try:
            invite = build_invite(
                remote_ip="127.0.0.1",
                remote_port=client.port,
                server_ip="127.0.0.1",
                server_port=sip_port,
            )
            await client.send(invite, ("127.0.0.1", sip_port))

            data = await client.recv(timeout=3.0)
            assert data is not None, "No response received"

            msg = parse_sip_message(data)
            assert msg.status_code == 403

        finally:
            client.close()
            await server.stop()
            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_options_response(self):
        """Test OPTIONS keepalive/probe."""
        sip_port = find_free_port()

        config = AppConfig()
        config.sip.bind_ip = "127.0.0.1"
        config.sip.bind_port = sip_port
        config.sip.allowed_networks = ["127.0.0.0/8"]

        callback = AsyncMock()
        server = SIPServer(config=config, on_call=callback)

        server_task = asyncio.create_task(server.start())
        await asyncio.sleep(0.3)

        client = UDPTestClient()
        try:
            options = build_options(
                remote_ip="127.0.0.1",
                remote_port=client.port,
                server_ip="127.0.0.1",
                server_port=sip_port,
            )
            await client.send(options, ("127.0.0.1", sip_port))

            data = await client.recv(timeout=3.0)
            assert data is not None, "No response received"

            msg = parse_sip_message(data)
            assert msg.status_code == 200

        finally:
            client.close()
            await server.stop()
            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                pass
