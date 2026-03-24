"""Unit tests for lookup tables."""

import os
import pytest
import tempfile
import yaml
from sipgw.lookups import load_lookups, get_area_name, get_call_purpose, get_room_name
import sipgw.lookups as lookups_mod


@pytest.fixture(autouse=True)
def reset_lookups():
    """Reset module-level lookup state between tests."""
    lookups_mod._area_map = {}
    lookups_mod._purpose_map = {}
    lookups_mod._room_map = {}
    lookups_mod._area_room_map = {}
    lookups_mod._default_area = "Unknown Area."
    lookups_mod._default_purpose = "Code"
    lookups_mod._default_room_format = "Room {room}."
    lookups_mod._loaded = False
    yield


@pytest.fixture
def sample_lookups_file():
    """Create a temporary lookups YAML file."""
    data = {
        "areas": {
            710: "3rd Floor. Cardiac Step-Down.",
            711: "2nd Floor. Orthopedics.",
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
    yield path
    os.unlink(path)


class TestGetAreaName:
    def test_known_area(self, sample_lookups_file):
        load_lookups(sample_lookups_file)
        assert get_area_name("710") == "3rd Floor. Cardiac Step-Down."

    def test_another_known_area(self, sample_lookups_file):
        load_lookups(sample_lookups_file)
        assert get_area_name("730") == "1st Floor. E.D."

    def test_unknown_area(self, sample_lookups_file):
        load_lookups(sample_lookups_file)
        assert get_area_name("999") == "Unknown Area."

    def test_icu(self, sample_lookups_file):
        load_lookups(sample_lookups_file)
        assert get_area_name("731") == "4th Floor, I.C.U."


class TestGetCallPurpose:
    def test_blue(self, sample_lookups_file):
        load_lookups(sample_lookups_file)
        assert get_call_purpose("Code Blue Alert") == "Code Blue"

    def test_rrt(self, sample_lookups_file):
        load_lookups(sample_lookups_file)
        assert get_call_purpose("RRT Alert") == "Rapid Response Team"

    def test_pink(self, sample_lookups_file):
        load_lookups(sample_lookups_file)
        assert get_call_purpose("Code Pink") == "Code Pink"

    def test_blue_keyword_only(self, sample_lookups_file):
        load_lookups(sample_lookups_file)
        assert get_call_purpose("Blue") == "Code Blue"

    def test_empty_display_name(self, sample_lookups_file):
        load_lookups(sample_lookups_file)
        assert get_call_purpose("") == "Code"

    def test_none_display_name(self, sample_lookups_file):
        load_lookups(sample_lookups_file)
        assert get_call_purpose(None) == "Code"

    def test_no_match(self, sample_lookups_file):
        load_lookups(sample_lookups_file)
        assert get_call_purpose("Random Text") == "Code"

    def test_whitespace_only(self, sample_lookups_file):
        load_lookups(sample_lookups_file)
        assert get_call_purpose("   ") == "Code"


class TestGetRoomName:
    def test_unmapped_room(self, sample_lookups_file):
        load_lookups(sample_lookups_file)
        assert get_room_name("201") == "Room 201."

    def test_mapped_room(self):
        lookups_mod._room_map = {"208": "Mens' Room"}
        lookups_mod._loaded = True
        assert get_room_name("208") == "Mens' Room."

    def test_mapped_room_unmapped_falls_back(self):
        lookups_mod._room_map = {"208": "Mens' Room"}
        lookups_mod._loaded = True
        assert get_room_name("300") == "Room 300."

    def test_room_from_yaml(self):
        data = {
            "areas": {},
            "call_purposes": {},
            "rooms": {208: "Mens' Room", 209: "Womens' Room"},
            "default_room_format": "Room {room}.",
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            path = f.name
        try:
            load_lookups(path)
            assert get_room_name("208") == "Mens' Room."
            assert get_room_name("209") == "Womens' Room."
            assert get_room_name("100") == "Room 100."
        finally:
            os.unlink(path)

    def test_custom_default_format(self):
        lookups_mod._room_map = {}
        lookups_mod._default_room_format = "Rm {room}."
        lookups_mod._loaded = True
        assert get_room_name("201") == "Rm 201."

    def test_leading_zeros_preserved(self):
        lookups_mod._room_map = {}
        lookups_mod._loaded = True
        assert get_room_name("01196") == "Room 01196."


class TestAreaRoomCombo:
    """Tests for area+room combo override lookups."""

    def test_combo_override_found(self):
        lookups_mod._area_room_map = {"797*2201": "Prepost 1"}
        lookups_mod._room_map = {}
        lookups_mod._loaded = True
        assert get_room_name("2201", area_number="797") == "Prepost 1."

    def test_combo_override_different_area_falls_through(self):
        """Same room number in different area has no combo override."""
        lookups_mod._area_room_map = {"797*2201": "Prepost 1"}
        lookups_mod._room_map = {}
        lookups_mod._loaded = True
        assert get_room_name("2201", area_number="795") == "Room 2201."

    def test_combo_override_takes_priority_over_room_map(self):
        """Combo override wins over room-only mapping."""
        lookups_mod._area_room_map = {"797*2201": "Prepost 1"}
        lookups_mod._room_map = {"2201": "Generic Room Name"}
        lookups_mod._loaded = True
        assert get_room_name("2201", area_number="797") == "Prepost 1."

    def test_room_map_used_when_no_combo(self):
        """Room-only mapping used when no combo override exists."""
        lookups_mod._area_room_map = {"797*2201": "Prepost 1"}
        lookups_mod._room_map = {"2201": "Generic Room Name"}
        lookups_mod._loaded = True
        assert get_room_name("2201", area_number="795") == "Generic Room Name."

    def test_no_area_falls_to_room_map(self):
        """When area_number is None, skip combo and use room map."""
        lookups_mod._area_room_map = {"797*2201": "Prepost 1"}
        lookups_mod._room_map = {"2201": "Generic Room Name"}
        lookups_mod._loaded = True
        assert get_room_name("2201", area_number=None) == "Generic Room Name."

    def test_combo_from_yaml(self):
        data = {
            "areas": {},
            "call_purposes": {},
            "rooms": {},
            "area_rooms": {"797*2201": "Prepost 1", "710*3196": "Dialysis"},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            path = f.name
        try:
            load_lookups(path)
            assert get_room_name("2201", area_number="797") == "Prepost 1."
            assert get_room_name("3196", area_number="710") == "Dialysis."
            assert get_room_name("2201", area_number="795") == "Room 2201."
        finally:
            os.unlink(path)
