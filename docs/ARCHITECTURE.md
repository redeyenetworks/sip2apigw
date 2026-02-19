# sipgw Architecture

## Overview

sipgw is a SIP-to-Webhook gateway that receives inbound SIP calls (Code Blue, RRT, Code Pink alerts from a Rauland nurse call system), parses caller information, builds a text-to-speech announcement string, and triggers an Informacast Fusion scenario via REST API.

## Component Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                     Rauland Nurse Call System                │
│                     (172.16.0.0/12 network)                  │
└──────────┬───────────────────────────┬──────────────────────┘
           │ SIP INVITE (UDP/TCP:5060)  │ RTP (discarded)
           ▼                           ▼
┌──────────────────────────────────────────────────────────────┐
│  sipgw                                                       │
│                                                              │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────────┐ │
│  │  SIP Server   │──▶│    Parser     │──▶│  TTS Builder     │ │
│  │  (sip_server) │   │  (parser)     │   │  (tts_builder)   │ │
│  │  UDP+TCP:5060 │   │              │   │                  │ │
│  └──────┬───────┘   └──────────────┘   └────────┬─────────┘ │
│         │                                        │           │
│  ┌──────▼───────┐                     ┌─────────▼─────────┐ │
│  │ RTP Handler   │                     │  Fusion Webhook    │ │
│  │ (rtp_handler) │                     │  (webhook)         │ │
│  │ Silence pkts  │                     │  OAuth2 + POST     │ │
│  └──────────────┘                     └─────────┬─────────┘ │
│                                                  │           │
│  ┌──────────────┐   ┌──────────────┐            │           │
│  │   Dashboard   │   │   Database    │◀───────────┘           │
│  │  (dashboard)  │──▶│  (database)   │                        │
│  │  HTTP:8080    │   │  SQLite       │                        │
│  └──────────────┘   └──────────────┘                        │
│                                                              │
│  ┌──────────────┐   ┌──────────────┐                        │
│  │   Lookups     │   │    Config     │                        │
│  │ (lookups.yaml)│   │ (config.yaml) │                        │
│  └──────────────┘   └──────────────┘                        │
└──────────────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│  Informacast Fusion Cloud            │
│  admin.icmobile.singlewire.com       │
│  POST /api/scenarios/{id}/launch     │
└──────────────────────────────────────┘
```

## Module Responsibilities

| Module            | File                | Purpose                                         |
|-------------------|---------------------|-------------------------------------------------|
| **Config**        | `config.py`         | Load config.yaml, provide typed dataclass access |
| **Lookups**       | `lookups.py`        | Area ID→name and purpose substitution tables     |
| **Parser**        | `parser.py`         | Extract area/room/bed from SIP username          |
| **TTS Builder**   | `tts_builder.py`    | Compose announcement string                      |
| **SIP Message**   | `sip_message.py`    | Parse/build SIP messages and SDP                 |
| **SIP Server**    | `sip_server.py`     | UDP+TCP listener, call state machine             |
| **RTP Handler**   | `rtp_handler.py`    | Send u-law silence RTP packets                   |
| **Webhook**       | `webhook.py`        | OAuth2 auth + Fusion scenario trigger            |
| **Database**      | `database.py`       | SQLite call history via aiosqlite                |
| **Dashboard**     | `dashboard.py`      | FastAPI web UI with auto-refresh                 |
| **Logging**       | `logging_config.py` | Dual-output logging with rotation + compression  |
| **Main**          | `main.py`           | Entry point, wires all components                |

## Call Flow

1. SIP INVITE arrives on port 5060 (UDP or TCP)
2. Source IP checked against allowed_networks (172.16.0.0/12)
3. SIP server sends `100 Trying` immediately
4. SIP server sends `200 OK` with SDP (offering PCMU/8000 RTP)
5. RTP silence stream starts (0xFF u-law packets every 20ms)
6. Asynchronously:
   - From header parsed → CallerInfo (area, room, bed, display name)
   - TTS string built: `"{Purpose}! {AreaName}. Room {Room}."`
   - OAuth2 token fetched/cached
   - POST to Fusion scenario with TTS variable
   - Result recorded to SQLite
7. Call held until BYE received or timeout (default 600s)
8. On termination: RTP stopped, call cleaned up

## SIP Implementation

The SIP server is a purpose-built, lightweight implementation rather than
a full SIP stack. It handles only the methods needed for this gateway:

- **INVITE**: Answer calls, establish RTP
- **ACK**: Confirm call establishment
- **BYE**: Terminate calls
- **CANCEL**: Abort pending calls
- **OPTIONS**: Respond to keepalive probes

This approach was chosen over pjsua2/sipsimple because:
- No complex native library dependencies
- Simpler installation (pure Python)
- Exact match for the limited requirements
- Full control over behavior

## Security

- Calls accepted only from configured networks (default: 172.16.0.0/12)
- systemd runs as unprivileged `sipgw` user
- CAP_NET_BIND_SERVICE for port 5060 binding
- Config file contains OAuth2 secret — permissions set to 640
- Dashboard has no authentication (intended for internal network only)
