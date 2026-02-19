# Assumptions and Design Decisions

## SIP Library Choice

**Decision**: Custom lightweight SIP implementation instead of pjsua2 or python-sipsimple.

**Rationale**: The gateway's SIP requirements are narrow — answer calls, hold with silence RTP, detect BYE. A full SIP stack (pjsua2, sipsimple) would introduce complex native dependencies (OPAL, oRTP, pjproject) making installation and maintenance harder. The custom implementation is ~400 lines of pure Python covering only the needed SIP methods (INVITE, ACK, BYE, CANCEL, OPTIONS).

**Trade-off**: Less robust handling of edge-case SIP scenarios (re-INVITE with codec changes, SIP timers, etc.), but these are unlikely in a dedicated nurse-call-to-gateway deployment.

## Informacast Fusion API

**Assumptions**:
- OAuth2 token endpoint: `POST /api/oauth/token` with `grant_type=client_credentials`
- Scenario trigger endpoint: `POST /api/scenarios/{id}/launch`
- Request body format: `{"customTTS": "<tts string>"}`
- Token response includes `access_token` and `expires_in`
- The scenario_endpoint is configurable in config.yaml to accommodate API changes

**If the endpoint differs**: Update `scenario_endpoint` in config.yaml (supports `{scenario_id}` placeholder). The `token_url` is also fully configurable.

## Updated Area Mappings

The following new area IDs from the updated list were mapped to speech-ready names by interpreting the abbreviations:

| ID  | Abbrev | Derived Speech Name       | Reasoning                                       |
|-----|--------|---------------------------|--------------------------------------------------|
| 713 | StpB_  | Step-Down B.              | Abbreviation pattern; near CSDU (710)            |
| 715 | GYN_   | 2nd Floor. Gynecology.    | Updated from JS "Mother Baby"; same floor assumed |
| 724 | INF1_  | 1st Floor. Infusion 1.    | INF prefix matches Infusion pattern (718=INFU)   |
| 727 | Lab_   | Laboratory.               | Direct interpretation                             |
| 729 | CPU_   | 1st Floor. Chest Pain Unit.| Updated from JS "E.D. Overflow"; CPU = Chest Pain|
| 733 | PCU_   | Progressive Care Unit.    | Standard hospital abbreviation                    |
| 791 | Stp_   | Step-Down.                | Abbreviation of Step-Down                         |
| 794 | Surg_  | Surgery.                  | Direct interpretation                             |
| 798 | PEDs_  | Pediatrics South.         | Differentiated from 732 (Pediatrics) with "South" |

**Area 732 conflict**: Both `PED_` and `5T_` map to 732 in the updated list. The JS code had Pediatrics active and 5th Floor Orthopedics commented out. Kept as "3rd Floor. Pediatrics." per the active JS code.

**All mappings are editable** in `lookups.yaml` without code changes.

## RTP Silence

- u-law silence byte: `0xFF` (encodes zero amplitude in ITU-T G.711 mu-law)
- Packet interval: 20ms (160 samples at 8kHz)
- RTP payload type: 0 (PCMU)
- Only PCMU offered in SDP (most compatible with nurse call systems)

## Call Handling

- Calls are answered immediately (no ringing delay)
- `100 Trying` sent first, then `200 OK` — no `180 Ringing`
- Webhook is triggered asynchronously after 200 OK so call setup is not delayed
- BYE is sent by the gateway on timeout (configurable, default 600s/10min)
- Re-INVITEs on existing calls get a fresh 200 OK
- Received RTP packets are discarded (UDP socket is opened but not read)

## Dashboard

- No authentication (designed for internal/trusted network access only)
- Auto-refresh via HTML meta refresh tag (no WebSocket)
- Shows last 200 calls by default
- SQLite database for persistence (sufficient for this call volume)

## Logging

- Daily rotation at midnight America/New_York
- Rotated files compressed to .tgz (tarball)
- 90-day retention (configurable)
- Dual output: stdout (for journalctl) + file (/var/log/sipgw/)

## Network

- SIP listens on both UDP and TCP port 5060
- Only sources from 172.16.0.0/12 are accepted (configurable)
- RTP ports allocated from a configurable range (default 10000-20000, even numbers only per RFC 3550)
- The gateway determines its local IP for SDP by creating a probe connection to the configured network

## Security

- The `sipgw` user is a no-login system account
- The service uses systemd security hardening (NoNewPrivileges, ProtectSystem, PrivateTmp)
- CAP_NET_BIND_SERVICE allows binding port 5060 without root
- config.yaml permissions set to 640 (contains OAuth2 client_secret)
