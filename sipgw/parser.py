"""SIP caller information parser.

Extracts area number, room number, and bed number from the SIP username,
and call purpose from the SIP display name.

Username format: a{area}r{room}[b{bed}]
Asterisks in the username are stripped before parsing.
"""

import re
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("sipgw.parser")

# Matches: a<digits>r<digits>[b<digits>]
_USERNAME_PATTERN = re.compile(r"^a(\d+)r(\d+)(?:b(\d*))?$")


@dataclass
class CallerInfo:
    """Parsed caller information from SIP headers."""
    raw_user: str
    display_name: str
    area_number: Optional[int] = None
    room_number: Optional[int] = None
    bed_number: Optional[int] = None
    parse_success: bool = False


def parse_caller_username(raw_user: str) -> tuple:
    """Parse the SIP username into (area, room, bed).

    Args:
        raw_user: Raw SIP username, may contain asterisks.

    Returns:
        Tuple of (area_number, room_number, bed_number, success).
        Numbers are int or None if not present. success is bool.
    """
    cleaned = raw_user.replace("*", "").strip()
    match = _USERNAME_PATTERN.match(cleaned)

    if not match:
        logger.warning(f"Username '{raw_user}' (cleaned: '{cleaned}') does not match expected format")
        return None, None, None, False

    area = int(match.group(1)) if match.group(1) else None
    room = int(match.group(2)) if match.group(2) else None
    bed = None
    if match.group(3) is not None and match.group(3) != "":
        bed = int(match.group(3))

    return area, room, bed, True


def parse_sip_from_header(from_header: str) -> tuple:
    """Extract display name and user from a SIP From header.

    From header format: "Display Name" <sip:user@host>;tag=xxx
    or: <sip:user@host>;tag=xxx
    or: sip:user@host

    Returns:
        Tuple of (user, display_name).
    """
    display_name = ""
    user = ""

    # Extract display name (quoted)
    dname_match = re.match(r'^"([^"]*)"', from_header)
    if dname_match:
        display_name = dname_match.group(1)
    else:
        # Unquoted display name before <
        dname_match = re.match(r'^([^<]*)<', from_header)
        if dname_match:
            display_name = dname_match.group(1).strip()

    # Extract user from URI
    uri_match = re.search(r'sip:([^@>]+)', from_header)
    if uri_match:
        user = uri_match.group(1)

    return user, display_name


def parse_caller(from_header: str) -> CallerInfo:
    """Full parse of caller info from SIP From header.

    Combines username parsing and display name extraction.
    """
    user, display_name = parse_sip_from_header(from_header)
    area, room, bed, success = parse_caller_username(user)

    info = CallerInfo(
        raw_user=user,
        display_name=display_name,
        area_number=area,
        room_number=room,
        bed_number=bed,
        parse_success=success,
    )

    logger.info(
        f"Parsed caller: user={user}, display={display_name}, "
        f"area={area}, room={room}, bed={bed}, ok={success}"
    )
    return info
