"""RTP silence sender.

Sends RTP packets containing u-law silence (0xFF) to keep SIP calls alive.
Discards any received RTP packets.
"""

import asyncio
import socket
import struct
import random
import logging
from typing import Optional, Tuple

logger = logging.getLogger("sipgw.rtp")

# u-law silence byte (encodes zero amplitude)
ULAW_SILENCE = b"\xff"

# RTP constants
RTP_VERSION = 2
PAYLOAD_TYPE_PCMU = 0
SAMPLES_PER_PACKET = 160  # 20ms at 8kHz
PACKET_INTERVAL = 0.02  # 20ms


def build_rtp_packet(
    seq: int,
    timestamp: int,
    ssrc: int,
    payload: bytes,
    marker: bool = False,
) -> bytes:
    """Build a single RTP packet.

    Header format (12 bytes):
      0                   1                   2                   3
      0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
     +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
     |V=2|P|X|  CC   |M|     PT      |       sequence number         |
     +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
     |                           timestamp                           |
     +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
     |                             SSRC                              |
     +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    """
    byte0 = (RTP_VERSION << 6)  # V=2, P=0, X=0, CC=0
    byte1 = PAYLOAD_TYPE_PCMU
    if marker:
        byte1 |= 0x80

    header = struct.pack(
        "!BBHII",
        byte0,
        byte1,
        seq & 0xFFFF,
        timestamp & 0xFFFFFFFF,
        ssrc,
    )
    return header + payload


class RTPSilenceStream:
    """Sends periodic RTP silence packets over UDP."""

    def __init__(
        self,
        local_port: int,
        remote_addr: Tuple[str, int],
        bind_ip: str = "0.0.0.0",
    ):
        self.local_port = local_port
        self.remote_addr = remote_addr
        self.bind_ip = bind_ip
        self.ssrc = random.randint(0, 0xFFFFFFFF)
        self.seq = random.randint(0, 0xFFFF)
        self.timestamp = random.randint(0, 0xFFFFFFFF)
        self._sock: Optional[socket.socket] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def start(self) -> asyncio.Task:
        """Start sending RTP silence in a background task."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.bind_ip, self.local_port))
        self._sock.setblocking(False)
        self._running = True

        logger.info(
            f"RTP stream started: local={self.bind_ip}:{self.local_port} "
            f"remote={self.remote_addr[0]}:{self.remote_addr[1]} ssrc={self.ssrc:#x}"
        )

        self._task = asyncio.create_task(self._send_loop())
        return self._task

    async def _send_loop(self):
        """Continuously send silence packets every 20ms."""
        silence_payload = ULAW_SILENCE * SAMPLES_PER_PACKET  # 160 bytes
        loop = asyncio.get_event_loop()

        # Send first packet with marker bit
        pkt = build_rtp_packet(self.seq, self.timestamp, self.ssrc, silence_payload, marker=True)
        try:
            await loop.sock_sendto(self._sock, pkt, self.remote_addr)
        except Exception as e:
            logger.warning(f"RTP send error (first packet): {e}")

        self.seq = (self.seq + 1) & 0xFFFF
        self.timestamp = (self.timestamp + SAMPLES_PER_PACKET) & 0xFFFFFFFF

        while self._running:
            pkt = build_rtp_packet(self.seq, self.timestamp, self.ssrc, silence_payload)
            try:
                await loop.sock_sendto(self._sock, pkt, self.remote_addr)
            except Exception as e:
                if self._running:
                    logger.warning(f"RTP send error: {e}")
                break

            self.seq = (self.seq + 1) & 0xFFFF
            self.timestamp = (self.timestamp + SAMPLES_PER_PACKET) & 0xFFFFFFFF

            await asyncio.sleep(PACKET_INTERVAL)

    def stop(self):
        """Stop sending and close the socket."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        logger.info(f"RTP stream stopped: ssrc={self.ssrc:#x}")
