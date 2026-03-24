"""TTS string builder.

Constructs the announcement string from parsed caller information
and assembles the final output with preambles and repetition.

Base format: "{CallPurpose}! {AreaName} {RoomName}"
Example base: "Code Blue! 1st Floor. E.D. Room 201."

Assembled (3x): "Attention! Attention! Code Blue! 1st Floor. E.D. Room 201.
    Attention! Code Blue! 1st Floor. E.D. Room 201.
    Attention! Code Blue! 1st Floor. E.D. Room 201."
"""

import logging
from .parser import CallerInfo
from .lookups import get_area_name, get_call_purpose, get_room_name

logger = logging.getLogger("sipgw.tts")


def build_tts(caller: CallerInfo) -> str:
    """Build a base TTS announcement string from parsed caller info.

    Args:
        caller: Parsed CallerInfo from SIP headers.

    Returns:
        Base TTS string (single play, no preambles).
    """
    purpose = get_call_purpose(caller.display_name)
    area_name = get_area_name(caller.area_number) if caller.area_number is not None else "Unknown Area."
    room_text = get_room_name(caller.room_number, caller.area_number) if caller.room_number is not None else "Room Unknown."

    tts = f"{purpose}! {area_name} {room_text}"

    logger.info(f"Built TTS: {tts}")
    return tts


def assemble_tts(
    base_tts: str,
    play_count: int = 3,
    message_preamble: str = "Attention! ",
    iteration_preamble: str = "Attention! ",
) -> str:
    """Assemble the final TTS string with preambles and repetition.

    Structure: {message_preamble}{iteration_preamble}{base} {iteration_preamble}{base} ...

    Args:
        base_tts: The base TTS string from build_tts().
        play_count: Number of times to repeat the TTS content.
        message_preamble: String prepended once at the start of the entire message.
        iteration_preamble: String prepended before each repetition.

    Returns:
        Assembled TTS string ready for the Fusion webhook.
    """
    if play_count < 1:
        play_count = 1

    iterations = [f"{iteration_preamble}{base_tts}" for _ in range(play_count)]
    assembled = f"{message_preamble}{' '.join(iterations)}"

    logger.info(f"Assembled TTS ({play_count}x): {assembled}")
    return assembled
