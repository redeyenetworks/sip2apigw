# Testing Guide

## Test Structure

```
tests/
├── test_parser.py          # Unit: SIP username/header parsing
├── test_lookups.py         # Unit: Area name and purpose lookups
├── test_tts_builder.py     # Unit: TTS string construction
├── test_sip_message.py     # Unit: SIP message parsing and response building
├── test_rtp.py             # Unit: RTP packet construction
├── test_webhook.py         # Unit: OAuth2 and Fusion API client (mocked HTTP)
├── test_dashboard.py       # Unit: FastAPI dashboard endpoints
├── test_functional.py      # Functional: End-to-end pipeline (parse→TTS→DB)
└── test_system.py          # System: Real UDP socket SIP server tests
```

## Running Tests

### Prerequisites

```bash
# Activate the virtual environment
source /opt/sipgw/venv/bin/activate

# Install test dependencies (already in requirements.txt)
pip install pytest pytest-asyncio
```

### Run All Tests

```bash
cd /opt/sipgw
python -m pytest tests/ -v
```

### Run by Category

```bash
# Unit tests only (fast, no I/O)
python -m pytest tests/test_parser.py tests/test_lookups.py tests/test_tts_builder.py tests/test_sip_message.py tests/test_rtp.py -v

# Webhook tests (mocked HTTP)
python -m pytest tests/test_webhook.py -v

# Dashboard tests
python -m pytest tests/test_dashboard.py -v

# Functional tests (integration, uses temp files)
python -m pytest tests/test_functional.py -v

# System tests (starts real UDP SIP server on random port)
python -m pytest tests/test_system.py -v
```

### Run with Output

```bash
python -m pytest tests/ -v -s  # Show print/log output
```

## Test Descriptions

### Unit Tests

**test_parser.py**: Validates the `a{area}r{room}[b{bed}]` username parser:
- Basic area+room extraction
- Asterisk stripping (`a*710r*201` → area=710, room=201)
- Bed number parsing (optional)
- Invalid format handling
- SIP From header display name + URI extraction

**test_lookups.py**: Validates lookup table loading and querying:
- Known area ID → correct name
- Unknown area ID → default
- Call purpose keyword matching (Blue, RRT, Pink)
- Empty/None display name handling

**test_tts_builder.py**: Validates TTS string composition:
- `"Code Blue! 1st Floor. E.D. Room 201."` format
- Different code types (Blue, RRT, Pink)
- Unknown areas
- Empty display names

**test_sip_message.py**: Validates SIP message parsing:
- INVITE parsing (method, headers, SDP body)
- BYE parsing
- Response building (100, 200 with SDP)
- SDP c= and m= line extraction

**test_rtp.py**: Validates RTP packet construction:
- Correct header format (version, payload type, SSRC)
- Marker bit behavior
- u-law silence payload (160 bytes of 0xFF)
- Sequence number wrapping

**test_webhook.py**: Validates Fusion API client:
- Successful trigger with token fetch
- Token caching (no redundant token requests)
- 401 retry with token refresh
- Error handling

**test_dashboard.py**: Validates FastAPI endpoints:
- HTML dashboard rendering with call data
- Auto-refresh meta tag
- JSON API endpoint
- Health check endpoint
- Empty state display

### Functional Tests

**test_functional.py**: End-to-end pipeline tests without network:
- Build a SIP INVITE → parse it → extract caller → build TTS → verify string
- Multiple scenarios (Code Blue ED, RRT ICU, Code Pink, etc.)
- Database record/retrieve integration
- Uses the production lookups.yaml

### System Tests

**test_system.py**: Real network tests:
- Start SIP server on a random port
- Send UDP INVITE → verify 100 Trying + 200 OK response
- Send from unauthorized IP → verify 403 Forbidden
- Send OPTIONS → verify 200 OK with Allow header

These tests use actual UDP sockets and bind to random ports to avoid conflicts.

## Manual Testing

### Send a Test SIP INVITE

Use a SIP tool like `sipp` or `pjsua`:

```bash
# Using sipp (install: apt install sip-tester)
sipp -sn uac 127.0.0.1:5060 -m 1 -s a730r201

# Or craft a UDP packet manually
python3 -c "
import socket
invite = (
    'INVITE sip:gw@10.0.0.1:5060 SIP/2.0\r\n'
    'Via: SIP/2.0/UDP 172.16.1.1:5060;branch=z9hG4bKtest\r\n'
    'From: \"Code Blue\" <sip:a730r201@172.16.1.1>;tag=test\r\n'
    'To: <sip:gw@10.0.0.1:5060>\r\n'
    'Call-ID: manual-test@172.16.1.1\r\n'
    'CSeq: 1 INVITE\r\n'
    'Content-Type: application/sdp\r\n'
    'Content-Length: 0\r\n'
    '\r\n'
).encode()
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.sendto(invite, ('10.0.0.1', 5060))
data, addr = s.recvfrom(65535)
print(data.decode())
"
```

### Check Dashboard

```bash
curl http://localhost:8080/
curl http://localhost:8080/api/calls
curl http://localhost:8080/health
```
