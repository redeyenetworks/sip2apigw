"""Unit tests for RTP silence handler."""

import struct
import pytest
from sipgw.rtp_handler import build_rtp_packet, ULAW_SILENCE, SAMPLES_PER_PACKET


class TestBuildRTPPacket:
    def test_packet_length(self):
        """RTP packet = 12-byte header + 160-byte payload."""
        payload = ULAW_SILENCE * SAMPLES_PER_PACKET
        pkt = build_rtp_packet(seq=1, timestamp=160, ssrc=0x12345678, payload=payload)
        assert len(pkt) == 12 + 160

    def test_rtp_version(self):
        """First two bits should be version 2."""
        payload = ULAW_SILENCE * SAMPLES_PER_PACKET
        pkt = build_rtp_packet(seq=0, timestamp=0, ssrc=0, payload=payload)
        assert (pkt[0] >> 6) == 2

    def test_payload_type_pcmu(self):
        """Payload type should be 0 (PCMU)."""
        payload = ULAW_SILENCE * SAMPLES_PER_PACKET
        pkt = build_rtp_packet(seq=0, timestamp=0, ssrc=0, payload=payload)
        assert (pkt[1] & 0x7F) == 0

    def test_marker_bit(self):
        """Marker bit should be set when marker=True."""
        payload = ULAW_SILENCE * SAMPLES_PER_PACKET
        pkt = build_rtp_packet(seq=0, timestamp=0, ssrc=0, payload=payload, marker=True)
        assert (pkt[1] & 0x80) != 0

    def test_no_marker_bit(self):
        """Marker bit should not be set when marker=False."""
        payload = ULAW_SILENCE * SAMPLES_PER_PACKET
        pkt = build_rtp_packet(seq=0, timestamp=0, ssrc=0, payload=payload, marker=False)
        assert (pkt[1] & 0x80) == 0

    def test_sequence_number(self):
        """Sequence number should be correctly encoded."""
        payload = ULAW_SILENCE * SAMPLES_PER_PACKET
        pkt = build_rtp_packet(seq=1234, timestamp=0, ssrc=0, payload=payload)
        seq = struct.unpack("!H", pkt[2:4])[0]
        assert seq == 1234

    def test_timestamp(self):
        """Timestamp should be correctly encoded."""
        payload = ULAW_SILENCE * SAMPLES_PER_PACKET
        pkt = build_rtp_packet(seq=0, timestamp=320, ssrc=0, payload=payload)
        ts = struct.unpack("!I", pkt[4:8])[0]
        assert ts == 320

    def test_ssrc(self):
        """SSRC should be correctly encoded."""
        payload = ULAW_SILENCE * SAMPLES_PER_PACKET
        pkt = build_rtp_packet(seq=0, timestamp=0, ssrc=0xDEADBEEF, payload=payload)
        ssrc = struct.unpack("!I", pkt[8:12])[0]
        assert ssrc == 0xDEADBEEF

    def test_silence_payload(self):
        """Payload should be all 0xFF (u-law silence)."""
        payload = ULAW_SILENCE * SAMPLES_PER_PACKET
        pkt = build_rtp_packet(seq=0, timestamp=0, ssrc=0, payload=payload)
        assert pkt[12:] == b"\xff" * 160

    def test_sequence_wrapping(self):
        """Sequence number should wrap at 16 bits."""
        payload = ULAW_SILENCE * SAMPLES_PER_PACKET
        pkt = build_rtp_packet(seq=0x10000, timestamp=0, ssrc=0, payload=payload)
        seq = struct.unpack("!H", pkt[2:4])[0]
        assert seq == 0  # Wrapped
