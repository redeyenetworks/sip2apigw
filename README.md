# sipgw — SIP-to-Webhook Gateway

SIP gateway that receives inbound calls from a Rauland nurse call system, parses caller information (area, room, alert type), builds a text-to-speech announcement string, and triggers an Informacast Fusion scenario via webhook.

## Quick Start

```bash
# Install (as root)
sudo bash install.sh

# Configure
sudo vi /opt/sipgw/config.yaml     # Set fusion.client_secret
sudo vi /opt/sipgw/lookups.yaml    # Review area mappings

# Start
sudo systemctl start sipgw

# Verify
systemctl status sipgw
curl http://localhost:8080/health
```

## How It Works

1. Listens on SIP port 5060 (UDP + TCP) for inbound INVITEs
2. Answers calls immediately, sends RTP silence to hold the line
3. Parses caller info from SIP headers: `a{area}r{room}[b{bed}]`
4. Builds TTS string: `"{Purpose}! {AreaName}. Room {Room}."`
5. POSTs to Informacast Fusion scenario via OAuth2-authenticated API
6. Records call details to SQLite for the web dashboard
7. Terminates call on BYE or configurable timeout (default 10 min)

## Example

SIP call from `"Code Blue" <sip:a730r201@172.16.1.100>` produces:

```
Code Blue! 1st Floor. E.D. Room 201.
```

## Project Structure

```
/opt/sipgw/
├── config.yaml          # Main configuration
├── lookups.yaml         # Area names and purpose substitutions (editable)
├── requirements.txt     # Python dependencies
├── sipgw/               # Application modules
│   ├── main.py          # Entry point
│   ├── sip_server.py    # SIP UDP+TCP listener
│   ├── sip_message.py   # SIP message parser
│   ├── rtp_handler.py   # RTP silence sender
│   ├── parser.py        # Caller info parser
│   ├── lookups.py       # Lookup table loader
│   ├── tts_builder.py   # TTS string builder
│   ├── webhook.py       # Fusion API client (OAuth2)
│   ├── database.py      # SQLite storage
│   ├── dashboard.py     # FastAPI web dashboard
│   ├── config.py        # Config loader
│   └── logging_config.py# Logging setup
├── tests/               # Test suite
├── docs/                # Documentation
│   ├── ARCHITECTURE.md  # System design
│   ├── ASSUMPTIONS.md   # Design decisions
│   ├── CONFIGURATION.md # Config reference
│   └── TESTING.md       # Test guide
├── sipgw.service        # systemd unit file
├── install.sh           # Installation script
└── uninstall.sh         # Removal script
```

## Configuration

All tunables are in `config.yaml`. See [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

Key settings:
- `fusion.client_secret` — **must be set before first run**
- `sip.allowed_networks` — IP filter (default: 172.16.0.0/12)
- `sip.call_timeout_seconds` — auto-hangup timer (default: 600)

## Lookup Tables

Area names and call purpose substitutions are in `lookups.yaml`. Edit this file to add/change mappings without code changes. Restart the service after editing.

## Dashboard

Web dashboard at `http://<host>:8080` (no auth, auto-refreshes every 10s).

Shows: timestamp, caller ID, display name, parsed area/room, TTS string, Fusion HTTP status, response time.

## Logs

- Stdout (via journalctl): `journalctl -u sipgw -f`
- File: `/var/log/sipgw/sipgw.log`
- Daily rotation at midnight ET, compressed to .tgz, 90-day retention

## Testing

```bash
source /opt/sipgw/venv/bin/activate
python -m pytest tests/ -v
```

See [docs/TESTING.md](docs/TESTING.md) for details.

## Service Management

```bash
sudo systemctl start sipgw
sudo systemctl stop sipgw
sudo systemctl restart sipgw
sudo systemctl status sipgw
```

## Uninstall

```bash
sudo bash uninstall.sh
```
