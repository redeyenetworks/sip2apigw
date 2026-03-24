"""Unit tests for TTS string builder."""

import os
import pytest
import tempfile
import yaml
from sipgw.parser import CallerInfo
from sipgw.tts_builder import build_tts, assemble_tts
import sipgw.lookups as lookups_mod
from sipgw.lookups import load_lookups


@pytest.fixture(autouse=True)
def load_test_lookups():
    """Load lookups for TTS tests."""
    lookups_mod._area_map = {}
    lookups_mod._purpose_map = {}
    lookups_mod._room_map = {}
    lookups_mod._area_room_map = {}
    lookups_mod._default_area = "Unknown Area."
    lookups_mod._default_purpose = "Code"
    lookups_mod._default_room_format = "Room {room}."
    lookups_mod._loaded = False

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
        path = f.name

    load_lookups(path)
    yield
    os.unlink(path)


class TestBuildTTS:
    def test_code_blue_ed(self):
        caller = CallerInfo(
            raw_user="a730r201",
            display_name="Code Blue",
            area_number="730",
            room_number="201",
            parse_success=True,
        )
        result = build_tts(caller)
        assert result == "Code Blue! 1st Floor. E.D. Room 201."

    def test_code_blue_cardiac(self):
        caller = CallerInfo(
            raw_user="a710r100",
            display_name="Blue Alert",
            area_number="710",
            room_number="100",
            parse_success=True,
        )
        result = build_tts(caller)
        assert result == "Code Blue! 3rd Floor. Cardiac Step-Down. Room 100."

    def test_rrt_icu(self):
        caller = CallerInfo(
            raw_user="a731r400",
            display_name="RRT",
            area_number="731",
            room_number="400",
            parse_success=True,
        )
        result = build_tts(caller)
        assert result == "Rapid Response Team! 4th Floor, I.C.U. Room 400."

    def test_unknown_area(self):
        caller = CallerInfo(
            raw_user="a999r100",
            display_name="Blue",
            area_number="999",
            room_number="100",
            parse_success=True,
        )
        result = build_tts(caller)
        assert result == "Code Blue! Unknown Area. Room 100."

    def test_empty_display_name(self):
        caller = CallerInfo(
            raw_user="a730r201",
            display_name="",
            area_number="730",
            room_number="201",
            parse_success=True,
        )
        result = build_tts(caller)
        assert result == "Code! 1st Floor. E.D. Room 201."

    def test_code_pink(self):
        caller = CallerInfo(
            raw_user="a710r50",
            display_name="Code Pink",
            area_number="710",
            room_number="50",
            parse_success=True,
        )
        result = build_tts(caller)
        assert result == "Code Pink! 3rd Floor. Cardiac Step-Down. Room 50."

    def test_no_area(self):
        caller = CallerInfo(
            raw_user="unknown",
            display_name="Blue",
            area_number=None,
            room_number=None,
            parse_success=False,
        )
        result = build_tts(caller)
        assert result == "Code Blue! Unknown Area. Room Unknown."

    def test_room_mapping(self):
        """Test that mapped room numbers use the mapped name."""
        lookups_mod._room_map["208"] = "Mens' Room"
        caller = CallerInfo(
            raw_user="a730r208",
            display_name="Code Blue",
            area_number="730",
            room_number="208",
            parse_success=True,
        )
        result = build_tts(caller)
        assert result == "Code Blue! 1st Floor. E.D. Mens' Room."

    def test_room_mapping_unmapped_falls_back(self):
        """Test that unmapped room numbers use the default format."""
        lookups_mod._room_map["208"] = "Mens' Room"
        caller = CallerInfo(
            raw_user="a730r201",
            display_name="Code Blue",
            area_number="730",
            room_number="201",
            parse_success=True,
        )
        result = build_tts(caller)
        assert result == "Code Blue! 1st Floor. E.D. Room 201."

    def test_leading_zeros_preserved(self):
        """Test that leading zeros in room numbers are preserved."""
        caller = CallerInfo(
            raw_user="a730r01196",
            display_name="Code Blue",
            area_number="730",
            room_number="01196",
            parse_success=True,
        )
        result = build_tts(caller)
        assert result == "Code Blue! 1st Floor. E.D. Room 01196."

    def test_area_room_combo_override(self):
        """Area+room combo override takes priority."""
        lookups_mod._area_room_map["797*2201"] = "Prepost 1"
        caller = CallerInfo(
            raw_user="a797r2201b1",
            display_name="Code Blue",
            area_number="797",
            room_number="2201",
            bed_number="1",
            parse_success=True,
        )
        result = build_tts(caller)
        assert "Prepost 1." in result

    def test_area_room_no_combo_uses_default(self):
        """Same room in different area without combo uses default format."""
        lookups_mod._area_room_map["797*2201"] = "Prepost 1"
        caller = CallerInfo(
            raw_user="a795r2201b1",
            display_name="Code Blue",
            area_number="795",
            room_number="2201",
            parse_success=True,
        )
        result = build_tts(caller)
        assert "Room 2201." in result


class TestAssembleTTS:
    """Tests for TTS assembly with preambles and repetition."""

    def test_default_assembly(self):
        base = "Code Blue! 1st Floor. E.D. Room 201."
        result = assemble_tts(base)
        expected = (
            "Attention! "
            "Attention! Code Blue! 1st Floor. E.D. Room 201. "
            "Attention! Code Blue! 1st Floor. E.D. Room 201. "
            "Attention! Code Blue! 1st Floor. E.D. Room 201."
        )
        assert result == expected

    def test_play_count_1(self):
        base = "Code Blue! 1st Floor. E.D. Room 201."
        result = assemble_tts(base, play_count=1)
        assert result == "Attention! Attention! Code Blue! 1st Floor. E.D. Room 201."

    def test_play_count_zero_floors_to_one(self):
        base = "Test."
        result = assemble_tts(base, play_count=0)
        assert result == "Attention! Attention! Test."

    def test_empty_preambles(self):
        base = "Code Blue! 1st Floor. E.D. Room 201."
        result = assemble_tts(base, play_count=2, message_preamble="", iteration_preamble="")
        assert result == "Code Blue! 1st Floor. E.D. Room 201. Code Blue! 1st Floor. E.D. Room 201."

    def test_message_preamble_only(self):
        base = "Code Blue! 1st Floor. E.D. Room 201."
        result = assemble_tts(base, play_count=2, message_preamble="Alert! ", iteration_preamble="")
        assert result == "Alert! Code Blue! 1st Floor. E.D. Room 201. Code Blue! 1st Floor. E.D. Room 201."

    def test_iteration_preamble_only(self):
        base = "Code Blue! 1st Floor. E.D. Room 201."
        result = assemble_tts(base, play_count=2, message_preamble="", iteration_preamble="Now! ")
        assert result == "Now! Code Blue! 1st Floor. E.D. Room 201. Now! Code Blue! 1st Floor. E.D. Room 201."

    def test_play_count_5(self):
        base = "Test."
        result = assemble_tts(base, play_count=5, message_preamble="", iteration_preamble="")
        assert result.count("Test.") == 5
