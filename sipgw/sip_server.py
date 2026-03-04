"""SIP server for handling inbound calls.

Listens on UDP and TCP port 5060, answers INVITEs, holds calls with RTP
silence, and terminates on BYE or configurable timeout. Supports multiple
concurrent inbound SIP calls.

This is a lightweight, purpose-built SIP UA server. It handles only the
SIP methods needed for this gateway (INVITE, ACK, BYE, CANCEL, OPTIONS)
rather than implementing the full SIP specification.
"""

import asyncio
import random
import time
import logging
import socket
from ipaddress import ip_address, ip_network
from typing import Dict, Optional, Callable, Awaitable, Tuple
from dataclasses import dataclass, field

from .sip_message import (
    SIPMessage,
    parse_sip_message,
    build_response,
    parse_sdp_connection,
    parse_sdp_media_port,
)
from .rtp_handler import RTPSilenceStream
from .config import AppConfig

logger = logging.getLogger("sipgw.sip")
sip_debug = logging.getLogger("sipgw.sip_debug")


@dataclass
class ActiveCall:
    """Represents an active SIP call."""

    call_id: str
    from_tag: str
    to_tag: str
    from_header: str
    to_header: str
    via_headers: list
    remote_addr: Tuple[str, int]  # SIP signaling address
    remote_rtp_addr: Tuple[str, int]  # RTP address from SDP
    local_rtp_port: int
    caller_user: str
    caller_display_name: str
    state: str = "trying"  # trying -> answered -> terminated
    rtp_stream: Optional[RTPSilenceStream] = None
    timeout_task: Optional[asyncio.Task] = None
    transport: object = None
    protocol_type: str = "udp"
    created_at: float = field(default_factory=time.time)


# Type for the call callback
CallCallback = Callable[[str, str, str, str, int, int], Awaitable[None]]


class SIPUDPProtocol(asyncio.DatagramProtocol):
    """Asyncio UDP protocol for SIP."""

    def __init__(self, server: "SIPServer"):
        self.server = server
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        asyncio.ensure_future(
            self.server.handle_message(data, addr, self.transport, "udp")
        )

    def error_received(self, exc):
        logger.error(f"UDP error: {exc}")


class SIPTCPServerProtocol(asyncio.Protocol):
    """Asyncio TCP protocol for SIP connections."""

    def __init__(self, server: "SIPServer"):
        self.server = server
        self.transport = None
        self.buffer = b""
        self.addr = None

    def connection_made(self, transport):
        self.transport = transport
        self.addr = transport.get_extra_info("peername")
        logger.debug(f"TCP connection from {self.addr}")

    def connection_lost(self, exc):
        logger.debug(f"TCP connection lost from {self.addr}: {exc}")

    def data_received(self, data):
        self.buffer += data
        while True:
            msg_data = self._try_extract_message()
            if msg_data is None:
                break
            asyncio.ensure_future(
                self.server.handle_message(msg_data, self.addr, self.transport, "tcp")
            )

    def _try_extract_message(self) -> Optional[bytes]:
        """Try to extract a complete SIP message from the buffer."""
        header_end = self.buffer.find(b"\r\n\r\n")
        if header_end == -1:
            return None

        headers_data = self.buffer[:header_end]
        body_start = header_end + 4

        content_length = 0
        for line in headers_data.split(b"\r\n"):
            lower_line = line.lower()
            if lower_line.startswith(b"content-length:") or lower_line.startswith(b"l:"):
                try:
                    content_length = int(line.split(b":", 1)[1].strip())
                except (ValueError, IndexError):
                    pass
                break

        total_length = body_start + content_length
        if len(self.buffer) < total_length:
            return None

        message = self.buffer[:total_length]
        self.buffer = self.buffer[total_length:]
        return message


class SIPServer:
    """SIP server that answers and holds inbound calls."""

    def __init__(
        self,
        config: AppConfig,
        on_call: CallCallback,
    ):
        """
        Args:
            config: Application configuration.
            on_call: Async callback invoked when a call is answered.
                     Signature: (call_id, caller_user, display_name,
                                 from_header, area_number, room_number) -> None
        """
        self.config = config
        self.on_call = on_call
        self.calls: Dict[str, ActiveCall] = {}
        self.allowed_networks = [
            ip_network(n, strict=False) for n in config.sip.allowed_networks
        ]
        self._rtp_ports_in_use: set = set()
        self._running = False
        self._bind_ip = config.sip.bind_ip
        self._local_ip: Optional[str] = None

    def _get_local_ip(self) -> str:
        """Determine the local IP for SDP and Contact headers."""
        if self._local_ip:
            return self._local_ip
        if self._bind_ip != "0.0.0.0":
            self._local_ip = self._bind_ip
            return self._local_ip
        # Try to determine local IP by connecting to a known address
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("172.16.0.1", 5060))
            self._local_ip = s.getsockname()[0]
            s.close()
        except Exception:
            self._local_ip = "127.0.0.1"
        return self._local_ip

    def _is_allowed(self, addr: str) -> bool:
        """Check if source IP is in allowed networks."""
        try:
            ip = ip_address(addr)
            return any(ip in net for net in self.allowed_networks)
        except ValueError:
            return False

    def _allocate_rtp_port(self) -> int:
        """Allocate an even-numbered RTP port from the configured range."""
        start = self.config.sip.rtp_port_range_start
        end = self.config.sip.rtp_port_range_end
        # Ensure we start with an even number
        if start % 2 != 0:
            start += 1
        for port in range(start, end, 2):
            if port not in self._rtp_ports_in_use:
                self._rtp_ports_in_use.add(port)
                return port
        raise RuntimeError("No available RTP ports")

    def _free_rtp_port(self, port: int):
        self._rtp_ports_in_use.discard(port)

    @staticmethod
    def _extract_tag(header_value: str) -> str:
        """Extract ;tag= parameter from a SIP header."""
        for part in header_value.split(";"):
            part = part.strip()
            if part.startswith("tag="):
                return part[4:]
        return ""

    async def start(self):
        """Start listening on UDP and TCP."""
        self._running = True
        loop = asyncio.get_running_loop()
        bind = (self._bind_ip, self.config.sip.bind_port)

        # UDP listener
        udp_transport, _ = await loop.create_datagram_endpoint(
            lambda: SIPUDPProtocol(self),
            local_addr=bind,
        )

        # TCP listener
        tcp_server = await loop.create_server(
            lambda: SIPTCPServerProtocol(self),
            *bind,
            reuse_address=True,
        )

        local_ip = self._get_local_ip()
        logger.info(
            f"SIP server listening on {bind[0]}:{bind[1]} (UDP+TCP), "
            f"local IP for SDP: {local_ip}"
        )

        try:
            # Run forever
            while self._running:
                await asyncio.sleep(1)
        finally:
            udp_transport.close()
            tcp_server.close()
            # Terminate all active calls
            for call in list(self.calls.values()):
                await self._terminate_call(call, "shutdown")

    async def stop(self):
        """Stop the server."""
        self._running = False

    async def handle_message(
        self,
        data: bytes,
        addr: Tuple[str, int],
        transport,
        protocol_type: str,
    ):
        """Route an incoming SIP message to the appropriate handler."""
        # Log raw inbound SIP message
        sip_debug.info("<<< RECV from %s:%s (%s)\n%s", addr[0], addr[1], protocol_type,
                        data.decode("utf-8", errors="replace").rstrip())

        try:
            msg = parse_sip_message(data)
        except Exception as e:
            logger.warning(f"Failed to parse SIP from {addr}: {e}")
            return

        if not msg.is_request:
            return  # We only process requests

        # IP filter
        if not self._is_allowed(addr[0]):
            logger.warning(f"Rejected {msg.method} from unauthorized IP {addr[0]}")
            self._send(
                build_response(msg, 403, "Forbidden"),
                addr, transport, protocol_type,
            )
            return

        method = msg.method.upper()
        logger.debug(f"Received {method} from {addr[0]}:{addr[1]} ({protocol_type})")

        try:
            if method == "INVITE":
                await self._handle_invite(msg, addr, transport, protocol_type)
            elif method == "ACK":
                self._handle_ack(msg, addr)
            elif method == "BYE":
                await self._handle_bye(msg, addr, transport, protocol_type)
            elif method == "CANCEL":
                await self._handle_cancel(msg, addr, transport, protocol_type)
            elif method == "OPTIONS":
                self._handle_options(msg, addr, transport, protocol_type)
            else:
                self._send(
                    build_response(
                        msg, 405, "Method Not Allowed",
                        extra_headers={"Allow": "INVITE, ACK, BYE, CANCEL, OPTIONS"},
                    ),
                    addr, transport, protocol_type,
                )
        except Exception as e:
            logger.error(f"Error handling {method} from {addr}: {e}", exc_info=True)

    async def _handle_invite(self, msg: SIPMessage, addr, transport, protocol_type):
        """Handle an incoming INVITE — answer the call immediately."""
        call_id = msg.get_call_id()

        if call_id in self.calls:
            # Re-INVITE on existing call — just re-send 200 OK
            call = self.calls[call_id]
            logger.info(f"Re-INVITE for existing call {call_id}")
            sdp = self._build_sdp(call.local_rtp_port)
            self._send(
                build_response(
                    msg, 200, "OK",
                    extra_headers={
                        "Contact": f"<sip:sipgw@{self._get_local_ip()}:{self.config.sip.bind_port}>",
                    },
                    body=sdp,
                    to_tag=call.to_tag,
                ),
                addr, transport, protocol_type,
            )
            return

        # Parse remote RTP endpoint from SDP
        remote_rtp_ip = parse_sdp_connection(msg.body) or addr[0]
        remote_rtp_port = parse_sdp_media_port(msg.body) or 0

        # Allocate local RTP port
        try:
            local_rtp_port = self._allocate_rtp_port()
        except RuntimeError:
            logger.error("No RTP ports available, rejecting call")
            self._send(
                build_response(msg, 503, "Service Unavailable"),
                addr, transport, protocol_type,
            )
            return

        # Parse caller info — free port on any failure before call is registered
        try:
            from_header = msg.get_from()
            caller_user, caller_display = self._parse_from_header(from_header)
            from_tag = self._extract_tag(from_header)
            to_tag = f"sipgw-{random.randint(100000, 999999)}"

            # Create call record
            call = ActiveCall(
                call_id=call_id,
                from_tag=from_tag,
                to_tag=to_tag,
                from_header=from_header,
                to_header=msg.get_to(),
                via_headers=msg.get_headers("Via") or msg.get_headers("v"),
                remote_addr=addr,
                remote_rtp_addr=(remote_rtp_ip, remote_rtp_port),
                local_rtp_port=local_rtp_port,
                caller_user=caller_user,
                caller_display_name=caller_display,
                transport=transport,
                protocol_type=protocol_type,
            )

            self.calls[call_id] = call
        except Exception:
            self._free_rtp_port(local_rtp_port)
            raise

        # Send 100 Trying
        self._send(
            build_response(msg, 100, "Trying"),
            addr, transport, protocol_type,
        )

        # Send 200 OK with SDP
        call.state = "answered"
        sdp = self._build_sdp(local_rtp_port)
        self._send(
            build_response(
                msg, 200, "OK",
                extra_headers={
                    "Contact": f"<sip:sipgw@{self._get_local_ip()}:{self.config.sip.bind_port}>",
                },
                body=sdp,
                to_tag=to_tag,
            ),
            addr, transport, protocol_type,
        )

        if self.config.sip.immediate_bye:
            # Immediate BYE mode: answer then immediately hang up, no RTP
            logger.info(
                f"Call {call_id} answered+BYE (immediate): {caller_display} <{caller_user}> "
                f"from {addr[0]}:{addr[1]}"
            )
            self._send_bye(call)
            self._free_rtp_port(local_rtp_port)
            call.state = "terminated"
            self.calls.pop(call_id, None)

            # Trigger async callback for webhook + DB
            asyncio.create_task(self._safe_callback(call))
            return

        # Start RTP silence stream
        if remote_rtp_port > 0:
            call.rtp_stream = RTPSilenceStream(
                local_port=local_rtp_port,
                remote_addr=(remote_rtp_ip, remote_rtp_port),
                bind_ip=self._get_local_ip() if self._bind_ip == "0.0.0.0" else self._bind_ip,
            )
            call.rtp_stream.start()

        # Start call timeout
        call.timeout_task = asyncio.create_task(
            self._call_timeout(call)
        )

        logger.info(
            f"Call {call_id} answered: {caller_display} <{caller_user}> "
            f"from {addr[0]}:{addr[1]}, RTP {remote_rtp_ip}:{remote_rtp_port} "
            f"-> local:{local_rtp_port}"
        )

        # Trigger async callback for webhook + DB
        asyncio.create_task(self._safe_callback(call))

    def _handle_ack(self, msg: SIPMessage, addr):
        """Handle ACK — confirms call establishment."""
        call_id = msg.get_call_id()
        if call_id in self.calls:
            logger.debug(f"ACK received for call {call_id}")
        else:
            logger.debug(f"ACK for unknown call {call_id}")

    async def _handle_bye(self, msg: SIPMessage, addr, transport, protocol_type):
        """Handle BYE — terminate the call."""
        call_id = msg.get_call_id()

        # Send 200 OK for BYE
        self._send(
            build_response(
                msg, 200, "OK",
                to_tag=self.calls[call_id].to_tag if call_id in self.calls else "",
            ),
            addr, transport, protocol_type,
        )

        if call_id in self.calls:
            call = self.calls[call_id]
            await self._terminate_call(call, "BYE received")
        else:
            logger.warning(f"BYE for unknown call {call_id}")

    async def _handle_cancel(self, msg: SIPMessage, addr, transport, protocol_type):
        """Handle CANCEL — abort a pending call."""
        call_id = msg.get_call_id()

        # 200 OK for the CANCEL itself
        self._send(
            build_response(msg, 200, "OK"),
            addr, transport, protocol_type,
        )

        if call_id in self.calls:
            call = self.calls[call_id]
            # Send 487 Request Terminated for the original INVITE
            # (We need to reconstruct a minimal INVITE response)
            self._send(
                build_response(
                    msg, 487, "Request Terminated",
                    to_tag=call.to_tag,
                ),
                addr, transport, protocol_type,
            )
            await self._terminate_call(call, "CANCEL received")
        else:
            logger.warning(f"CANCEL for unknown call {call_id}")

    def _handle_options(self, msg: SIPMessage, addr, transport, protocol_type):
        """Handle OPTIONS — respond with capabilities."""
        self._send(
            build_response(
                msg, 200, "OK",
                extra_headers={
                    "Allow": "INVITE, ACK, BYE, CANCEL, OPTIONS",
                    "Accept": "application/sdp",
                },
            ),
            addr, transport, protocol_type,
        )

    async def _call_timeout(self, call: ActiveCall):
        """Terminate a call after the configured timeout."""
        timeout = self.config.sip.call_timeout_seconds
        await asyncio.sleep(timeout)

        if call.call_id in self.calls:
            logger.info(f"Call {call.call_id} timed out after {timeout}s")
            # Send BYE to the remote end
            self._send_bye(call)
            await self._terminate_call(call, f"timeout ({timeout}s)")

    async def _terminate_call(self, call: ActiveCall, reason: str):
        """Clean up a call: stop RTP, cancel timeout, remove from tracking."""
        if call.state == "terminated":
            return

        call.state = "terminated"
        duration = time.time() - call.created_at

        # Stop RTP
        if call.rtp_stream:
            call.rtp_stream.stop()

        # Cancel timeout
        if call.timeout_task and not call.timeout_task.done():
            call.timeout_task.cancel()

        # Free RTP port
        self._free_rtp_port(call.local_rtp_port)

        # Remove from active calls
        self.calls.pop(call.call_id, None)

        logger.info(
            f"Call {call.call_id} terminated: reason={reason} "
            f"duration={duration:.1f}s"
        )

    def _send_bye(self, call: ActiveCall):
        """Send a BYE request to end a call from our side."""
        local_ip = self._get_local_ip()
        port = self.config.sip.bind_port
        branch = f"z9hG4bK-sipgw-{random.randint(100000, 999999)}"
        tag = f"sipgw-bye-{random.randint(100000, 999999)}"

        bye = (
            f"BYE sip:{call.caller_user}@{call.remote_addr[0]}:{call.remote_addr[1]} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {local_ip}:{port};branch={branch}\r\n"
            f"From: <sip:sipgw@{local_ip}:{port}>;tag={call.to_tag}\r\n"
            f"To: {call.from_header}\r\n"
            f"Call-ID: {call.call_id}\r\n"
            f"CSeq: 1 BYE\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )

        self._send(
            bye.encode("utf-8"),
            call.remote_addr,
            call.transport,
            call.protocol_type,
        )

    async def _safe_callback(self, call: ActiveCall):
        """Invoke the on_call callback with error handling."""
        try:
            await self.on_call(
                call.call_id,
                call.caller_user,
                call.caller_display_name,
                call.from_header,
            )
        except Exception as e:
            logger.error(f"Call callback error for {call.call_id}: {e}")

    def _send(self, data: bytes, addr, transport, protocol_type: str):
        """Send SIP data via the appropriate transport."""
        # Log raw outbound SIP message
        sip_debug.info(">>> SEND to %s:%s (%s)\n%s", addr[0], addr[1], protocol_type,
                        data.decode("utf-8", errors="replace").rstrip())
        try:
            if protocol_type == "udp":
                transport.sendto(data, addr)
            else:
                # TCP
                transport.write(data)
        except Exception as e:
            logger.error(f"Failed to send SIP response to {addr}: {e}")

    def _build_sdp(self, rtp_port: int) -> str:
        """Build an SDP body for a 200 OK response."""
        local_ip = self._get_local_ip()
        session_id = random.randint(1000000, 9999999)
        return (
            f"v=0\r\n"
            f"o=sipgw {session_id} {session_id} IN IP4 {local_ip}\r\n"
            f"s=sipgw\r\n"
            f"c=IN IP4 {local_ip}\r\n"
            f"t=0 0\r\n"
            f"m=audio {rtp_port} RTP/AVP 0\r\n"
            f"a=rtpmap:0 PCMU/8000\r\n"
            f"a=sendrecv\r\n"
        )

    @staticmethod
    def _parse_from_header(from_header: str) -> tuple:
        """Extract (user, display_name) from a SIP From header.

        Handles formats:
            "Display Name" <sip:user@host>;tag=xxx
            <sip:user@host>;tag=xxx
            sip:user@host
        """
        import re

        display_name = ""
        user = ""

        # Quoted display name
        dname_match = re.match(r'^"([^"]*)"', from_header)
        if dname_match:
            display_name = dname_match.group(1)
        else:
            # Unquoted display name before <
            dname_match = re.match(r"^([^<]*)<", from_header)
            if dname_match:
                display_name = dname_match.group(1).strip()

        # User from URI
        uri_match = re.search(r"sip:([^@>]+)", from_header)
        if uri_match:
            user = uri_match.group(1)

        return user, display_name
