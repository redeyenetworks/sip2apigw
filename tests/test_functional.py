"""Functional tests — test component integration without live network.

These tests verify that the pipeline from SIP message parsing through
TTS string generation works correctly end-to-end.
"""

import os
import pytest
import tempfile
import yaml
from sipgw.sip_message import parse_sip_message
from sipgw.parser import parse_caller, parse_sip_from_header, parse_caller_username
from sipgw.tts_builder import build_tts, assemble_tts
from sipgw.lookups import load_lookups, get_area_name, get_call_purpose
import sipgw.lookups as lookups_mod


@pytest.fixture(autouse=True)
def load_full_lookups():
    """Load the production lookups.yaml for functional tests."""
    lookups_mod._area_map = {}
    lookups_mod._purpose_map = {}
    lookups_mod._room_map = {}
    lookups_mod._default_area = "Unknown Area."
    lookups_mod._default_purpose = "Code"
    lookups_mod._default_room_format = "Room {room}."
    lookups_mod._loaded = False

    lookups_path = os.path.join(os.path.dirname(__file__), "..", "lookups.yaml")
    if os.path.exists(lookups_path):
        load_lookups(lookups_path)
    else:
        # Fall back to embedded test data
        data = {
            "areas": {
                710: "3rd Floor. Cardiac Step-Down.",
                730: "1st Floor. E.D.",
                731: "4th Floor, I.C.U.",
            },
            "default_area": "Unknown Area.",
            "call_purposes": {
                "Blue": "Code Blue",
                "RRT": "Rapid Response Team",
                "Pink": "Code Pink",
            },
            "default_purpose": "Code",
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            load_lookups(f.name)
            os.unlink(f.name)
    yield


def make_invite(user: str, display_name: str, remote_ip: str = "172.16.1.100") -> bytes:
    """Build a minimal SIP INVITE message for testing."""
    sdp = (
        "v=0\r\n"
        f"o=- 1 1 IN IP4 {remote_ip}\r\n"
        "s=-\r\n"
        f"c=IN IP4 {remote_ip}\r\n"
        "t=0 0\r\n"
        "m=audio 40000 RTP/AVP 0\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
    )
    msg = (
        f"INVITE sip:gateway@10.0.0.1:5060 SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {remote_ip}:5060;branch=z9hG4bKtest\r\n"
        f'From: "{display_name}" <sip:{user}@{remote_ip}>;tag=test123\r\n'
        f"To: <sip:gateway@10.0.0.1:5060>\r\n"
        f"Call-ID: test-call-001@{remote_ip}\r\n"
        f"CSeq: 1 INVITE\r\n"
        f"Contact: <sip:{user}@{remote_ip}>\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {len(sdp)}\r\n"
        f"\r\n"
        f"{sdp}"
    )
    return msg.encode("utf-8")


class TestEndToEndPipeline:
    """Test the full pipeline: SIP INVITE -> parse -> TTS string."""

    def test_code_blue_ed(self):
        """Simulate Code Blue from E.D. room 201."""
        data = make_invite("a730r201", "Code Blue")
        msg = parse_sip_message(data)

        from_header = msg.get_from()
        caller = parse_caller(from_header)

        assert caller.parse_success is True
        assert caller.area_number == "730"
        assert caller.room_number == "201"

        tts = build_tts(caller)
        assert tts == "Code Blue! 1st Floor... E.D... Room 201."

    def test_rrt_icu(self):
        """Simulate RRT from I.C.U. room 400."""
        data = make_invite("a731r400", "RRT Alert")
        msg = parse_sip_message(data)

        caller = parse_caller(msg.get_from())
        assert caller.area_number == "731"

        tts = build_tts(caller)
        assert tts == "Rapid Response Team! 4th Floor... I.C.U... Room 400."

    def test_code_blue_cardiac(self):
        """Simulate Code Blue from Cardiac Step-Down room 100."""
        data = make_invite("a710r100", "Blue")
        msg = parse_sip_message(data)

        caller = parse_caller(msg.get_from())
        tts = build_tts(caller)
        assert "Code Blue!" in tts
        assert caller.area_number == "710"

    def test_asterisks_in_username(self):
        """Test that asterisks in the SIP username are handled."""
        data = make_invite("a*730r*201", "Code Blue")
        msg = parse_sip_message(data)

        caller = parse_caller(msg.get_from())
        assert caller.parse_success is True
        assert caller.area_number == "730"
        assert caller.room_number == "201"

    def test_with_bed_number(self):
        """Test parsing with bed number (bed is parsed but ignored in TTS)."""
        data = make_invite("a710r201b3", "Code Blue")
        msg = parse_sip_message(data)

        caller = parse_caller(msg.get_from())
        assert caller.bed_number == "3"
        # Bed number should not appear in TTS output
        tts = build_tts(caller)
        assert "b3" not in tts.lower()
        assert "bed" not in tts.lower()

    def test_code_pink(self):
        """Simulate Code Pink alert."""
        data = make_invite("a715r50", "Code Pink")
        msg = parse_sip_message(data)

        caller = parse_caller(msg.get_from())
        tts = build_tts(caller)
        assert "Code Pink!" in tts

    def test_empty_display_name(self):
        """Handle case where display name is empty."""
        data = make_invite("a730r201", "")
        msg = parse_sip_message(data)

        caller = parse_caller(msg.get_from())
        tts = build_tts(caller)
        assert tts.startswith("Code!")

    def test_unknown_area(self):
        """Handle unknown area ID."""
        data = make_invite("a999r100", "Blue")
        msg = parse_sip_message(data)

        caller = parse_caller(msg.get_from())
        tts = build_tts(caller)
        assert "Unknown Area." in tts


class TestDatabaseIntegration:
    """Test database operations."""

    @pytest.mark.asyncio
    async def test_record_and_retrieve(self):
        """Test recording a call and retrieving it."""
        from sipgw.database import CallDatabase

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            db = CallDatabase(db_path)
            await db.initialize()

            row_id = await db.record_call(
                caller_id="a730r201",
                display_name="Code Blue",
                area_number="730",
                area_name="1st Floor. E.D.",
                room_number="201",
                tts_string="Code Blue! 1st Floor. E.D. Room 201.",
                fusion_status=200,
                response_time_ms=150.5,
            )

            assert row_id == 1

            calls = await db.get_recent_calls(limit=10)
            assert len(calls) == 1
            assert calls[0]["caller_id"] == "a730r201"
            assert calls[0]["tts_string"] == "Code Blue! 1st Floor. E.D. Room 201."
            assert calls[0]["fusion_status"] == 200

            await db.close()
        finally:
            os.unlink(db_path)

    @pytest.mark.asyncio
    async def test_multiple_records(self):
        """Test inserting and retrieving multiple call records."""
        from sipgw.database import CallDatabase

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            db = CallDatabase(db_path)
            await db.initialize()

            for i in range(5):
                await db.record_call(
                    caller_id=f"a730r{200 + i}",
                    display_name="Code Blue",
                    area_number="730",
                    area_name="1st Floor. E.D.",
                    room_number=str(200 + i),
                    tts_string=f"Code Blue! 1st Floor. E.D. Room {200 + i}.",
                    fusion_status=200,
                    response_time_ms=100.0 + i * 10,
                )

            calls = await db.get_recent_calls(limit=10)
            assert len(calls) == 5
            # Should be in reverse chronological order
            assert calls[0]["room_number"] == "204"

            await db.close()
        finally:
            os.unlink(db_path)


class TestTTSAssemblyPipeline:
    """Test the full pipeline with TTS assembly."""

    def test_assembled_code_blue(self):
        data = make_invite("a730r201", "Code Blue")
        msg = parse_sip_message(data)
        caller = parse_caller(msg.get_from())
        base_tts = build_tts(caller)
        assembled = assemble_tts(base_tts, play_count=2, message_preamble="Alert! ", iteration_preamble="Now! ")
        assert assembled.startswith("Alert! Now! Code Blue!")
        assert assembled.count("Code Blue!") == 2

    def test_assembled_default_preambles(self):
        data = make_invite("a731r400", "RRT Alert")
        msg = parse_sip_message(data)
        caller = parse_caller(msg.get_from())
        base_tts = build_tts(caller)
        assembled = assemble_tts(base_tts, play_count=1)
        assert "Attention!" in assembled
        assert "Rapid Response Team!" in assembled
        assert "Room 400." in assembled


class TestTTSConfig:
    def test_default_tts_config(self):
        from sipgw.config import TTSConfig
        cfg = TTSConfig()
        assert cfg.play_count == 3
        assert cfg.message_preamble == "Attention! "
        assert cfg.iteration_preamble == "Attention! "

    def test_tts_config_from_yaml(self):
        from sipgw.config import load_config
        data = {
            "tts": {
                "play_count": 5,
                "message_preamble": "Alert! ",
                "iteration_preamble": "Warning! ",
            }
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            path = f.name
        try:
            config = load_config(path)
            assert config.tts.play_count == 5
            assert config.tts.message_preamble == "Alert! "
            assert config.tts.iteration_preamble == "Warning! "
        finally:
            os.unlink(path)
