"""Lightweight SIP message parser.

Parses raw SIP messages (requests and responses) into a structured format.
Only implements the subset of SIP needed for INVITE/ACK/BYE/CANCEL handling.
"""

import re
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("sipgw.sip_message")

# #15 INVITE fingerprint. Bump the version if the field set below changes so
# older log lines remain unambiguous.
_FINGERPRINT_VERSION = "v1"


@dataclass
class SIPMessage:
    """Parsed SIP message."""

    # Request fields
    is_request: bool = True
    method: str = ""
    request_uri: str = ""

    # Response fields
    status_code: int = 0
    reason_phrase: str = ""

    # Common
    headers: Dict[str, List[str]] = field(default_factory=dict)
    body: str = ""
    raw: bytes = b""

    def get_header(self, name: str) -> Optional[str]:
        """Get the first value of a header (case-insensitive)."""
        name_lower = name.lower()
        for k, values in self.headers.items():
            if k.lower() == name_lower:
                return values[0] if values else None
        return None

    def get_headers(self, name: str) -> List[str]:
        """Get all values of a header (case-insensitive)."""
        name_lower = name.lower()
        for k, values in self.headers.items():
            if k.lower() == name_lower:
                return values
        return []

    def get_call_id(self) -> str:
        return self.get_header("Call-ID") or self.get_header("i") or ""

    def get_cseq(self) -> str:
        return self.get_header("CSeq") or ""

    def get_from(self) -> str:
        return self.get_header("From") or self.get_header("f") or ""

    def get_to(self) -> str:
        return self.get_header("To") or self.get_header("t") or ""


def invite_fingerprint(msg: "SIPMessage") -> str:
    """Stable, transaction-scoped fingerprint of an INVITE for correlation.

    Keyed on Call-ID + From user-part + From tag + CSeq. A UDP retransmission of
    the same INVITE is byte-identical and yields the SAME fingerprint; a
    genuinely new call yields a different one. Via/branch and Contact are
    deliberately excluded so hop-by-hop routing changes don't perturb it. Values
    are stripped but NOT lowercased (Call-ID and the URI user-part are
    case-sensitive per RFC 3261). Never raises — missing fields still yield a
    stable ``v1:<16-hex>`` string.

    This is the TRANSACTION identity. It is intentionally distinct from #5's
    future CLINICAL identity (area/room/bed/purpose); do not unify them.
    """
    try:
        call_id = (msg.get_call_id() or "").strip()
    except Exception:
        call_id = ""
    try:
        from_hdr = msg.get_from() or ""
    except Exception:
        from_hdr = ""
    m = re.search(r"sip:([^@>;\s]+)", from_hdr)
    from_user = (m.group(1).strip() if m else "")
    t = re.search(r";tag=([^;>\s]+)", from_hdr)
    from_tag = (t.group(1).strip() if t else "")
    try:
        cseq = (msg.get_cseq() or "").strip()
    except Exception:
        cseq = ""

    canonical = "\n".join([
        f"call_id={call_id}",
        f"from_user={from_user}",
        f"from_tag={from_tag}",
        f"cseq={cseq}",
    ])
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{_FINGERPRINT_VERSION}:{digest}"


def extract_event_id(call_id: str) -> str:
    """Best-effort upstream event id from a Rauland Call-ID (segment 3).

    Rauland Call-IDs look like ``<a>-<b>-<event>-<d>-0-13c4-764``; the third
    hyphen-separated segment (index 2) is the upstream notification/event id
    that stays constant across the duplicate/retransmitted INVITEs of one
    clinical event. Returns "" for any shape that doesn't have at least three
    segments. Never raises.

    TELEMETRY / LOGGING ONLY. This value is emitted to the log and kept on the
    in-memory ActiveCall. It may now ANNOTATE #5's non-suppressing shadow audit
    (recorded as ``event_id_match`` alongside the cf-v1 clinical decision), but
    it MUST NEVER be a sole/primary suppression key nor relax the purpose
    hard-guard: #5's clinical fingerprint (cf-v1:) remains the sole match key
    and RRT vs Code Blue must never merge on a shared event_id.  [FLAG-FOR-REVIEW:
    subtle safety-boundary wording — annotation-only, not a match/suppress key.]
    """
    try:
        parts = (call_id or "").split("-")
        if len(parts) >= 3:
            return parts[2].strip()
    except Exception:
        pass
    return ""


def parse_sdp_session_id(body: str) -> str:
    """Extract the SDP session id from the o= line (field index 1).

    ``o=<username> <sess-id> <sess-version> <nettype> <addrtype> <addr>`` — the
    second whitespace-separated field is the session id. Best-effort: returns
    "" when there is no usable o= line. Never raises.
    """
    try:
        for line in (body or "").split("\n"):
            line = line.strip()
            if line.startswith("o="):
                parts = line.split()
                if len(parts) >= 2:
                    return parts[1].strip()
    except Exception:
        pass
    return ""


def via_hosts(msg: "SIPMessage") -> List[str]:
    """Ordered sent-by ``host[:port]`` list from the Via (or compact v) headers.

    Returned top-to-bottom as the headers appear on the wire (topmost Via, added
    by the most recent hop, first). Handles comma-folded multi-Via header values.
    Best-effort and never raises — malformed Via entries are skipped.
    """
    hosts: List[str] = []
    try:
        vias = msg.get_headers("Via") or msg.get_headers("v")
        for raw in vias:
            for chunk in (raw or "").split(","):
                chunk = chunk.strip()
                if not chunk:
                    continue
                # "SIP/2.0/UDP host[:port];branch=..." -> sent-by is token 1.
                toks = chunk.split()
                if len(toks) >= 2:
                    sent_by = toks[1].split(";")[0].strip()
                    if sent_by:
                        hosts.append(sent_by)
    except Exception:
        pass
    return hosts


def _from_identity(from_hdr: str) -> Tuple[str, str, str]:
    """(user, tag, display) from a From header — mirrors the parsing used by the
    SIP server / transaction fingerprint, but self-contained here to avoid a
    circular import. Best-effort; never raises.
    """
    user = tag = display = ""
    try:
        from_hdr = from_hdr or ""
        dm = re.match(r'^\s*"([^"]*)"', from_hdr)
        if dm:
            display = dm.group(1)
        else:
            dm = re.match(r"^\s*([^<]*)<", from_hdr)
            if dm:
                display = dm.group(1).strip()
        um = re.search(r"sip:([^@>;\s]+)", from_hdr)
        if um:
            user = um.group(1).strip()
        tm = re.search(r";tag=([^;>\s]+)", from_hdr)
        if tm:
            tag = tm.group(1).strip()
    except Exception:
        pass
    return user, tag, display


def _logfmt_val(value) -> str:
    """Render a value as a logfmt field value: bare when safe, quoted+escaped
    when it contains whitespace, a quote, or '='. Newlines/tabs are collapsed to
    spaces so one INVITE always maps to exactly one log line. Never raises.
    """
    try:
        s = "" if value is None else str(value)
    except Exception:
        s = ""
    if s == "" or any(c in s for c in ' \t\r\n"='):
        s = (s.replace("\\", "\\\\").replace('"', '\\"')
             .replace("\r", " ").replace("\n", " ").replace("\t", " "))
        return f'"{s}"'
    return s


def invite_fingerprint_line(msg: "SIPMessage", addr, remote_rtp_port,
                            sdp_session: str = "") -> str:
    """Build ONE structured (logfmt) observability line for a NEW INVITE.

    Purely additive telemetry (#15). Joins the shipped, retransmit-stable
    transaction fingerprint (``fp=``) with per-INVITE identity fields so the
    duplicate/retransmitted INVITEs of a single upstream event can be correlated
    from the log alone:

        INVITE fingerprint: call_id=.. event_id=.. from_tag=.. caller=.. \
            display=.. src=.. via=<origin>..>us> sdp_session=.. rtp_port=.. fp=..

    ``event_id`` is LOGGING ONLY (see ``extract_event_id``); it may annotate #5's
    non-suppressing shadow audit but must never be a sole/primary suppression key
    nor relax the purpose guard. The builder never raises: any internal failure
    still yields a best-effort string so it can never abort answering an INVITE.
    """
    try:
        call_id = (msg.get_call_id() or "").strip()
    except Exception:
        call_id = ""
    try:
        from_hdr = msg.get_from() or ""
    except Exception:
        from_hdr = ""
    caller, from_tag, display = _from_identity(from_hdr)
    event_id = extract_event_id(call_id)
    try:
        src = f"{addr[0]}:{addr[1]}"
    except Exception:
        src = ""
    # Via is stored top-to-bottom (us-facing first); reverse to read origin>..>us.
    via = ">".join(reversed(via_hosts(msg)))
    try:
        fp = invite_fingerprint(msg)
    except Exception:
        fp = ""
    fields = [
        ("call_id", call_id),
        ("event_id", event_id),
        ("from_tag", from_tag),
        ("caller", caller),
        ("display", display),
        ("src", src),
        ("via", via),
        ("sdp_session", sdp_session or ""),
        ("rtp_port", remote_rtp_port),
        ("fp", fp),
    ]
    return "INVITE fingerprint: " + " ".join(
        f"{k}={_logfmt_val(v)}" for k, v in fields
    )


def parse_sip_message(data: bytes) -> SIPMessage:
    """Parse raw bytes into a SIPMessage.

    Handles both requests and responses. Headers are stored preserving
    original case but looked up case-insensitively.
    """
    msg = SIPMessage(raw=data)

    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        text = data.decode("latin-1", errors="replace")

    # Split headers and body
    parts = text.split("\r\n\r\n", 1)
    header_section = parts[0]
    msg.body = parts[1] if len(parts) > 1 else ""

    lines = header_section.split("\r\n")
    if not lines:
        raise ValueError("Empty SIP message")

    # Parse first line (request-line or status-line)
    first_line = lines[0]

    if first_line.startswith("SIP/"):
        # Response: SIP/2.0 200 OK
        msg.is_request = False
        parts = first_line.split(None, 2)
        msg.status_code = int(parts[1]) if len(parts) > 1 else 0
        msg.reason_phrase = parts[2] if len(parts) > 2 else ""
    else:
        # Request: INVITE sip:user@host SIP/2.0
        msg.is_request = True
        parts = first_line.split(None, 2)
        msg.method = parts[0] if parts else ""
        msg.request_uri = parts[1] if len(parts) > 1 else ""

    # Parse headers
    current_name = None
    for line in lines[1:]:
        if not line:
            continue
        # Header continuation (starts with whitespace)
        if line[0] in (" ", "\t") and current_name:
            msg.headers[current_name][-1] += " " + line.strip()
            continue
        # New header
        colon_pos = line.find(":")
        if colon_pos > 0:
            name = line[:colon_pos].strip()
            value = line[colon_pos + 1:].strip()
            current_name = name
            if name not in msg.headers:
                msg.headers[name] = []
            msg.headers[name].append(value)

    return msg


def build_response(
    request: SIPMessage,
    status_code: int,
    reason: str,
    extra_headers: Optional[Dict[str, str]] = None,
    body: str = "",
    to_tag: str = "",
) -> bytes:
    """Build a SIP response from a request.

    Copies Via, From, To, Call-ID, CSeq from the request.
    Adds to_tag to the To header if provided.
    """
    lines = [f"SIP/2.0 {status_code} {reason}"]

    # Copy Via headers (all of them, in order)
    for via in request.get_headers("Via"):
        lines.append(f"Via: {via}")
    # Also check compact form
    if not request.get_headers("Via"):
        for via in request.get_headers("v"):
            lines.append(f"Via: {via}")

    # From (copy as-is)
    from_val = request.get_from()
    lines.append(f"From: {from_val}")

    # To (add tag if needed)
    to_val = request.get_to()
    if to_tag and ";tag=" not in to_val:
        to_val += f";tag={to_tag}"
    lines.append(f"To: {to_val}")

    # Call-ID
    lines.append(f"Call-ID: {request.get_call_id()}")

    # CSeq
    lines.append(f"CSeq: {request.get_cseq()}")

    # Extra headers
    if extra_headers:
        for name, value in extra_headers.items():
            lines.append(f"{name}: {value}")

    # Content-Length and Content-Type
    if body:
        lines.append("Content-Type: application/sdp")
        lines.append(f"Content-Length: {len(body)}")
    else:
        lines.append("Content-Length: 0")

    # Final CRLF + body
    response = "\r\n".join(lines) + "\r\n\r\n"
    if body:
        response += body

    return response.encode("utf-8")


def parse_sdp_connection(body: str) -> Optional[str]:
    """Extract the connection IP from SDP c= line."""
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("c="):
            # c=IN IP4 192.168.1.1
            parts = line.split()
            if len(parts) >= 3:
                return parts[2]
    return None


def parse_sdp_media_port(body: str) -> Optional[int]:
    """Extract the audio RTP port from SDP m=audio line."""
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("m=audio"):
            # m=audio 40000 RTP/AVP 0
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return int(parts[1])
                except ValueError:
                    pass
    return None
