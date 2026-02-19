"""Unit tests for SIP caller info parser."""

import pytest
from sipgw.parser import parse_caller_username, parse_sip_from_header, parse_caller


class TestParseCallerUsername:
    """Test the a{area}r{room}[b{bed}] username parser."""

    def test_basic_area_room(self):
        area, room, bed, ok = parse_caller_username("a710r201")
        assert ok is True
        assert area == 710
        assert room == 201
        assert bed is None

    def test_with_bed(self):
        area, room, bed, ok = parse_caller_username("a710r201b3")
        assert ok is True
        assert area == 710
        assert room == 201
        assert bed == 3

    def test_strips_asterisks(self):
        area, room, bed, ok = parse_caller_username("a*710r*201b*3")
        assert ok is True
        assert area == 710
        assert room == 201
        assert bed == 3

    def test_large_area_number(self):
        area, room, bed, ok = parse_caller_username("a730r100")
        assert ok is True
        assert area == 730
        assert room == 100

    def test_bed_empty_after_b(self):
        area, room, bed, ok = parse_caller_username("a710r201b")
        assert ok is True
        assert area == 710
        assert room == 201
        assert bed is None

    def test_invalid_format_no_prefix(self):
        _, _, _, ok = parse_caller_username("710201")
        assert ok is False

    def test_invalid_format_missing_r(self):
        _, _, _, ok = parse_caller_username("a710")
        assert ok is False

    def test_invalid_format_letters(self):
        _, _, _, ok = parse_caller_username("axyzr201")
        assert ok is False

    def test_empty_string(self):
        _, _, _, ok = parse_caller_username("")
        assert ok is False

    def test_whitespace_handling(self):
        area, room, bed, ok = parse_caller_username("  a710r201  ")
        assert ok is True
        assert area == 710
        assert room == 201

    def test_all_asterisks(self):
        area, room, bed, ok = parse_caller_username("a*7*1*0r*2*0*1")
        assert ok is True
        assert area == 710
        assert room == 201


class TestParseSipFromHeader:
    """Test From header parsing."""

    def test_quoted_display_name(self):
        user, display = parse_sip_from_header('"Code Blue" <sip:a710r201@172.16.1.100>;tag=12345')
        assert user == "a710r201"
        assert display == "Code Blue"

    def test_no_display_name(self):
        user, display = parse_sip_from_header("<sip:a710r201@172.16.1.100>;tag=12345")
        assert user == "a710r201"
        assert display == ""

    def test_unquoted_display_name(self):
        user, display = parse_sip_from_header("Code Blue <sip:a710r201@172.16.1.100>")
        assert user == "a710r201"
        assert display == "Code Blue"

    def test_bare_uri(self):
        user, display = parse_sip_from_header("sip:a710r201@172.16.1.100")
        assert user == "a710r201"
        assert display == ""

    def test_rrt_display_name(self):
        user, display = parse_sip_from_header('"RRT Alert" <sip:a731r400@172.16.2.50>')
        assert user == "a731r400"
        assert display == "RRT Alert"

    def test_pink_display_name(self):
        user, display = parse_sip_from_header('"Code Pink" <sip:a715r100@172.16.1.10>')
        assert user == "a715r100"
        assert display == "Code Pink"


class TestParseCaller:
    """Test full caller parsing pipeline."""

    def test_full_parse_code_blue(self):
        caller = parse_caller('"Code Blue" <sip:a730r201@172.16.1.100>;tag=abc')
        assert caller.parse_success is True
        assert caller.raw_user == "a730r201"
        assert caller.display_name == "Code Blue"
        assert caller.area_number == 730
        assert caller.room_number == 201
        assert caller.bed_number is None

    def test_full_parse_with_asterisks(self):
        caller = parse_caller('"Blue" <sip:a*710r*201b*1@172.16.1.100>')
        assert caller.parse_success is True
        assert caller.raw_user == "a*710r*201b*1"
        assert caller.area_number == 710
        assert caller.room_number == 201
        assert caller.bed_number == 1

    def test_full_parse_rrt(self):
        caller = parse_caller('"RRT" <sip:a731r400@172.16.2.50>;tag=xyz')
        assert caller.parse_success is True
        assert caller.display_name == "RRT"
        assert caller.area_number == 731
        assert caller.room_number == 400

    def test_unparseable_user(self):
        caller = parse_caller('"Code Blue" <sip:unknown@172.16.1.100>')
        assert caller.parse_success is False
        assert caller.raw_user == "unknown"
        assert caller.display_name == "Code Blue"
