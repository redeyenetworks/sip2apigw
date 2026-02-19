# SIPGW Service Manual

## SIP-to-Webhook Gateway for Rauland Nurse Call Systems

**Version:** 1.1
**Last Updated:** 2026-02-19

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Module Reference](#3-module-reference)
4. [Call Flow](#4-call-flow)
5. [Configuration Reference](#5-configuration-reference)
6. [Lookup Tables Reference](#6-lookup-tables-reference)
7. [TTS Composition](#7-tts-composition)
8. [Fusion API Integration](#8-fusion-api-integration)
9. [SIP Implementation Details](#9-sip-implementation-details)
10. [RTP Silence Stream](#10-rtp-silence-stream)
11. [Dashboard](#11-dashboard)
12. [Logging](#12-logging)
13. [Database](#13-database)
14. [Installation](#14-installation)
15. [Service Management](#15-service-management)
16. [Security](#16-security)
17. [Testing](#17-testing)
18. [Troubleshooting](#18-troubleshooting)
19. [Environment Variables](#19-environment-variables)
20. [Uninstallation](#20-uninstallation)
21. [File Layout](#21-file-layout)

---

## 1. Overview

SIPGW is a Python asyncio service that acts as a bridge between a Rauland nurse call system and the Informacast Fusion mass notification platform. When a nurse call station initiates an alert (Code Blue, Rapid Response Team, or Code Pink), the Rauland system places a SIP call to SIPGW. The service receives the inbound SIP call on port 5060, parses the caller information embedded in SIP headers to determine the alert type, location (area), and room number, then constructs a text-to-speech (TTS) announcement string. That string is delivered to Informacast Fusion by triggering a scenario via the Fusion REST API webhook. A FastAPI-based web dashboard provides real-time monitoring of call activity, logs, and API interactions.

### Key Capabilities

- **SIP Call Handling:** Receives inbound SIP INVITE messages over both UDP and TCP on port 5060, responds with proper SIP signaling (100 Trying, 200 OK), and maintains the call with an RTP silence stream until timeout or caller hangup.
- **Caller Identification:** Parses structured information from the SIP From header to extract the alert type (from the display name) and the area/room identifiers (from the username).
- **TTS Generation:** Builds a human-readable announcement string from the parsed caller information using configurable lookup tables, then wraps it with preambles and repetition for clarity during broadcast.
- **Fusion Webhook Delivery:** Authenticates to the Informacast Fusion API using OAuth2 client credentials, then triggers a configured scenario with the assembled TTS string as the notification payload.
- **Web Dashboard:** Provides a dark-themed, auto-refreshing HTML dashboard that displays call history, statistics, application logs, and API debug logs.
- **Call History:** Persists all processed calls to a SQLite database for historical review and dashboard display.

---

## 2. Architecture

SIPGW is built entirely on Python's asyncio framework, allowing it to handle concurrent SIP signaling, RTP streaming, HTTP webhook delivery, and web dashboard serving within a single process. All source code resides in the `/opt/sipgw/sipgw/` Python package directory.

### High-Level Component Diagram

```
                          +-------------------+
                          |  Rauland Nurse    |
                          |  Call System      |
                          +--------+----------+
                                   |
                              SIP INVITE
                            (UDP/TCP 5060)
                                   |
                                   v
+------------------------------+   |   +-----------------------------+
|       sip_server.py          |<--+   |      rtp_handler.py         |
|  - UDP + TCP listener        |------>|  - u-law silence packets    |
|  - IP allow-list filtering   |       |  - 20ms interval            |
|  - INVITE/ACK/BYE/CANCEL/   |       |  - PCMU/8000                |
|    OPTIONS handling          |       +-----------------------------+
+--------+---------------------+
         |
    SIP From header
         |
         v
+--------+---------------------+
|        parser.py             |
|  - Extract display_name      |
|  - Parse username regex      |
|    a{area}r{room}[b{bed}]   |
+--------+---------------------+
         |
    CallerInfo (area, room, display_name)
         |
         v
+--------+---------------------+       +-----------------------------+
|      tts_builder.py          |<------|      lookups.py             |
|  - build_tts()               |       |  - areas table              |
|  - assemble_tts()            |       |  - call_purposes table      |
+--------+---------------------+       |  - rooms table              |
         |                              +-----------------------------+
    Assembled TTS string                         ^
         |                                       |
         v                              +--------+------------------+
+--------+---------------------+        |     lookups.yaml          |
|       webhook.py             |        +---------------------------+
|  - OAuth2 token management   |
|  - Scenario trigger POST     |
|  - Auto-retry on 401         |
+--------+---------------------+
         |
    HTTPS POST to Fusion API
         |
         v
+--------+---------------------+
|   Informacast Fusion         |
|   Scenario Notification      |
+------------------------------+

+------------------------------+        +-----------------------------+
|      dashboard.py            |        |      database.py            |
|  - FastAPI + Jinja2          |<-------|  - SQLite via aiosqlite     |
|  - Port 8080                 |        |  - Call history storage     |
|  - Auto-refresh              |        +-----------------------------+
+------------------------------+

+------------------------------+        +-----------------------------+
|    logging_config.py         |        |       config.py             |
|  - Dual stdout + file        |        |  - Typed dataclass config   |
|  - Daily rotation + .tgz     |        |  - Loads from config.yaml   |
|  - Separate API debug log    |        +-----------------------------+
+------------------------------+
```

### Concurrency Model

The `main.py` module contains the `SIPGateway` class, which serves as the top-level orchestrator. It wires together all components and runs the SIP server and the FastAPI dashboard concurrently using asyncio. Signal handlers (SIGTERM, SIGINT) are registered for graceful shutdown, ensuring that active calls are terminated cleanly and database connections are closed before the process exits.

---

## 3. Module Reference

### 3.1 main.py -- Entry Point

The entry point for the entire service. Contains the `SIPGateway` class, which:

- Loads configuration from `config.yaml` via `config.py`.
- Initializes all component modules (SIP server, webhook client, database, dashboard, logging).
- Runs the SIP listener and the FastAPI dashboard server concurrently using `asyncio.gather()` or equivalent.
- Registers signal handlers for SIGTERM and SIGINT to perform graceful shutdown.
- Coordinates the lifecycle of all subsystems.

### 3.2 sip_server.py -- SIP Server

An asyncio-based SIP User Agent that listens on both UDP and TCP port 5060 (configurable). Key responsibilities:

- **Dual Transport:** Binds to both UDP and TCP on the configured SIP port, handling messages on either transport.
- **IP Filtering:** Before processing any SIP request, the source IP is checked against the `allowed_networks` CIDR list. Unauthorized sources receive a 403 Forbidden response.
- **INVITE Handling:** Upon receiving an INVITE, the server responds with 100 Trying immediately, then constructs a 200 OK response with an SDP body offering PCMU/8000 audio on a randomly selected port from the configured RTP range. The call is then passed to the processing pipeline (parsing, TTS building, webhook delivery).
- **ACK Handling:** Acknowledges the 200 OK and confirms the dialog is established.
- **BYE Handling:** Responds with 200 OK and terminates the call, stopping the associated RTP stream.
- **CANCEL Handling:** Responds with 200 OK and cleans up any in-progress call state.
- **OPTIONS Handling:** Responds with 200 OK including capability headers, supporting SIP keep-alive and availability probing.
- **Call Timeout:** Each call has a configurable timeout (default 600 seconds). When the timeout expires, the server sends a BYE to the remote party to terminate the call.

### 3.3 sip_message.py -- SIP Message Parser

Provides SIP message parsing and construction utilities:

- **Request Parsing:** Parses incoming SIP requests, extracting the method, Request-URI, headers, and body.
- **Response Parsing:** Parses SIP responses, extracting the status code, reason phrase, headers, and body.
- **Header Extraction:** Provides methods to extract specific headers (From, To, Via, Call-ID, CSeq, Contact, Content-Type, etc.) with proper handling of multi-value headers and header parameters.
- **SDP Parsing:** Parses SDP bodies to extract media descriptions, connection information, and codec attributes.
- **Response Building:** Constructs SIP response messages (100, 200, 403, etc.) with appropriate headers, including Via copy-back, To tag generation, and SDP body attachment for INVITE 200 OK responses.

### 3.4 rtp_handler.py -- RTP Silence Sender

Maintains an active RTP stream to keep the SIP call alive after the 200 OK is sent:

- **Packet Format:** Each RTP packet consists of a 12-byte RTP header followed by 160 bytes of payload. The payload is filled with `0xFF`, which represents silence in the G.711 u-law (PCMU) codec.
- **Timing:** Packets are sent every 20 milliseconds, producing an 8000 Hz sample rate with 160 samples per packet (standard for PCMU).
- **Codec:** Uses PCMU (G.711 u-law) at 8000 Hz, payload type 0.
- **Port Selection:** A random port is selected from the configured RTP port range (`rtp_port_range_start` to `rtp_port_range_end`) for each call.
- **Lifecycle:** The RTP stream starts when the INVITE is answered and stops when the call is terminated (via BYE, CANCEL, or timeout).

### 3.5 parser.py -- Caller Info Parser

Parses the SIP From header to extract structured caller information:

- **Display Name Extraction:** Extracts the display name from the From header (the portion before the angle brackets or the quoted string).
- **Username Parsing:** Applies a regular expression pattern `a(\d+)r(\d+)(?:b(\d+))?` to the username portion of the From URI. This pattern extracts:
  - `a(\d+)` -- The area number (e.g., `a730` yields area 730).
  - `r(\d+)` -- The room number (e.g., `r201` yields room 201).
  - `(?:b(\d+))?` -- An optional bed number (e.g., `b2` yields bed 2).
- **CallerInfo Object:** Returns a structured object containing the parsed display name, area number, room number, and optional bed number.

### 3.6 lookups.py -- Lookup Table Loader

Loads and caches the area, purpose, and room lookup tables from `lookups.yaml`:

- **Module-Level Cache:** The lookup tables are loaded once at module initialization and cached in memory for the lifetime of the process. This avoids repeated file I/O on every call.
- **Area Lookup:** Maps numeric area IDs to human-readable area descriptions (e.g., `730` maps to `"1st Floor. E.D."`).
- **Purpose Lookup:** Maps keywords found in the SIP display name to alert type labels (e.g., `"Blue"` maps to `"Code Blue"`).
- **Room Lookup:** Maps specific room numbers to custom names (e.g., `208` maps to `"Mens' Room"`). Rooms not in the table use the default format `"Room {N}."`.
- **Default Values:** Each lookup provides a fallback: `default_area` for unknown areas, `default_purpose` for unrecognized alert types, and `default_room_format` for unmapped rooms.

### 3.7 tts_builder.py -- TTS String Builder

Contains two key functions for constructing the final TTS announcement:

- **`build_tts(caller_info)`** -- Constructs the base TTS string from a CallerInfo object. See [Section 7: TTS Composition](#7-tts-composition) for full details.
- **`assemble_tts(base_tts, config)`** -- Wraps the base string with preambles and repetition. See [Section 7: TTS Composition](#7-tts-composition) for full details.

### 3.8 webhook.py -- Fusion API Client

Manages authentication and API communication with the Informacast Fusion platform:

- **OAuth2 Client Credentials Flow:** Acquires access tokens from the Fusion token endpoint using client ID, client secret, and audience (provider ID).
- **Token Caching:** Tokens are cached in memory and reused until they are within 60 seconds of expiration, at which point a new token is automatically acquired.
- **Automatic Retry on 401:** If a scenario trigger request receives a 401 Unauthorized response, the client automatically refreshes the token and retries the request once.
- **Scenario Triggering:** Sends a POST request to the scenario notifications endpoint with the assembled TTS string as the field answer.
- **Field ID Auto-Resolution:** If `scenario_field_id` is left empty in the configuration, the client automatically resolves it by querying the scenario definition from the Fusion API on the first call.
- **Detailed Debug Logging:** When API debug logging is enabled, every HTTP request and response is logged with full detail (headers, body, timing, redirects), with sensitive values masked.

### 3.9 database.py -- SQLite Database

Provides asynchronous SQLite database access using the `aiosqlite` library:

- **Call History Storage:** Records every processed call with details including timestamp, caller information, parsed area/room, generated TTS string, Fusion API response status, and response time.
- **Query Interface:** Provides methods for the dashboard to retrieve call history with optional filtering and pagination.
- **Schema Management:** Automatically creates the required tables on first run.

### 3.10 dashboard.py -- Web Dashboard

A FastAPI application serving a web-based monitoring dashboard:

- **HTML Dashboard:** Rendered using Jinja2 templates with a dark theme. Auto-refreshes at the configured interval (default 10 seconds).
- **Call Table:** Displays call history with columns for timestamp, caller, area, room, TTS text, Fusion status, and response time.
- **Statistics Cards:** Shows aggregate counts for total calls, successful calls, and failed calls.
- **Recent Logs Panel:** Displays the last 50 lines of `sipgw.log` with a Copy button for easy clipboard access.
- **API Debug Log Panel:** Displays the last 50 lines of `sipgw_api_debug.log` with an orange-themed styling and a Copy button.
- **JSON API Endpoint:** `GET /api/calls?limit=100` returns call history as JSON for programmatic access.
- **Health Check Endpoint:** `GET /health` returns a simple health status for monitoring systems.

### 3.11 logging_config.py -- Logging Configuration

Configures the application logging infrastructure:

- **Dual Output:** Log messages are sent to both stdout (for `journalctl` visibility) and a rotating file at `/var/log/sipgw/sipgw.log`.
- **Daily Rotation:** Log files rotate at midnight (configurable) in the configured timezone. Rotated files are compressed into `.tgz` archives.
- **Retention:** Old log files are retained for the configured number of days (default 90), after which they are automatically deleted.
- **API Debug Log:** When enabled, a separate log file at `/var/log/sipgw/sipgw_api_debug.log` captures detailed HTTP request/response information for northbound API calls to Fusion.

### 3.12 config.py -- Configuration Loader

Loads and validates the application configuration:

- **Typed Dataclass:** Configuration is represented as a Python dataclass with typed fields, providing IDE auto-completion and runtime type checking.
- **YAML Source:** Configuration is loaded from `config.yaml` (path configurable via environment variable).
- **Validation:** Required fields are checked at load time, and invalid values produce clear error messages.

---

## 4. Call Flow

The following describes the complete lifecycle of a single nurse call alert, from SIP signaling through Fusion notification delivery.

### Step 1: SIP INVITE Received

A Rauland nurse call station generates a SIP INVITE directed to the SIPGW service on UDP or TCP port 5060. The INVITE contains structured information in the From header:

```
From: "Code Blue Rm 201" <sip:a730r201@sipgw.example.com>;tag=abc123
```

### Step 2: IP Authorization Check

The source IP address of the SIP message is checked against the `allowed_networks` list in the configuration. If the source IP does not fall within any of the configured CIDR ranges, the server responds with `403 Forbidden` and drops the request. This prevents unauthorized SIP endpoints from triggering alerts.

### Step 3: SIP 100 Trying

The server immediately sends a `100 Trying` provisional response to the caller, indicating that the request has been received and is being processed. This prevents the caller from retransmitting the INVITE prematurely.

### Step 4: SIP 200 OK with SDP

The server constructs a `200 OK` final response containing an SDP body. The SDP offers PCMU (G.711 u-law) audio at 8000 Hz on a randomly selected port from the configured RTP port range. This establishes the media session parameters.

```
v=0
o=sipgw 0 0 IN IP4 <bind_ip>
s=sipgw
c=IN IP4 <bind_ip>
t=0 0
m=audio <rtp_port> RTP/AVP 0
a=rtpmap:0 PCMU/8000
```

### Step 5: RTP Silence Stream Begins

Once the 200 OK is sent, the RTP handler begins sending silence packets to the caller's media address. Each packet contains 160 bytes of `0xFF` (u-law silence) and is sent every 20 milliseconds. This keeps the call active from the perspective of the Rauland system. The stream continues until the call is terminated.

### Step 6: Parse the From Header

The `parser.py` module extracts the display name and parses the username from the SIP From header:

- **Display Name:** `"Code Blue Rm 201"` -- Used to determine the alert type.
- **Username:** `a730r201` -- Parsed by the regex `a(\d+)r(\d+)(?:b(\d+))?` to extract:
  - Area: `730`
  - Room: `201`
  - Bed: (none in this example)

### Step 7: Build Base TTS String

The `build_tts()` function constructs the base announcement string:

1. **Purpose:** The display name `"Code Blue Rm 201"` is searched for keywords. The keyword `"Blue"` is found, mapping to `"Code Blue"`.
2. **Area:** Area number `730` is looked up in the areas table, yielding `"1st Floor. E.D."`.
3. **Room:** Room number `201` is looked up in the rooms table. If not present, the default format is used: `"Room 201."`.
4. **Result:** `"Code Blue! 1st Floor. E.D. Room 201."`

### Step 8: Assemble Final TTS String

The `assemble_tts()` function wraps the base string with preambles and repetition per the TTS configuration:

- With `play_count=3`, `message_preamble="Attention! "`, and `iteration_preamble="Attention! "`:

```
Attention! Attention! Code Blue! 1st Floor. E.D. Room 201. Attention! Code Blue! 1st Floor. E.D. Room 201. Attention! Code Blue! 1st Floor. E.D. Room 201.
```

See [Section 7: TTS Composition](#7-tts-composition) for a thorough explanation of this process.

### Step 9: Acquire OAuth2 Token

The webhook client checks its cached access token. If no token exists or the cached token is within 60 seconds of expiration, a new token is acquired from the Fusion token endpoint:

```
POST https://api.icmobile.singlewire.com/api/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&client_id=<id>&client_secret=<secret>&audience=<provider_id>
```

The returned access token is cached for subsequent calls.

### Step 10: Trigger Fusion Scenario

The webhook client sends a POST request to trigger the configured scenario:

```
POST https://api.icmobile.singlewire.com/api/v1/scenario-notifications?scenarioId=<uuid>
Authorization: Bearer <token>
Content-Type: application/json

{
  "fields": [
    {
      "fieldId": "<field-uuid>",
      "answer": "Attention! Attention! Code Blue! 1st Floor. E.D. Room 201. Attention! Code Blue! 1st Floor. E.D. Room 201. Attention! Code Blue! 1st Floor. E.D. Room 201."
    }
  ]
}
```

If the response is `401 Unauthorized`, the client automatically refreshes the OAuth2 token and retries the request once.

### Step 11: Log API Response

The full HTTP exchange (request headers, request body, response status, response headers, response body, elapsed time) is written to the API debug log at `/var/log/sipgw/sipgw_api_debug.log`. Sensitive values in headers (such as the Authorization bearer token and client secret) are masked in the log output.

### Step 12: Record Call in Database

The call details are persisted to the SQLite database at `/var/lib/sipgw/calls.db`, including the timestamp, raw caller information, parsed area and room, the generated TTS string, the Fusion API response status, and the response time.

### Step 13: Call Timeout and Termination

The call remains active (with the RTP silence stream running) until one of the following occurs:

- **Caller Hangs Up (BYE):** The Rauland system sends a BYE, and SIPGW responds with 200 OK. The RTP stream is stopped.
- **Caller Cancels (CANCEL):** The Rauland system sends a CANCEL, and SIPGW responds with 200 OK. The RTP stream is stopped.
- **Timeout Expires:** After the configured `call_timeout_seconds` (default 600 seconds / 10 minutes), SIPGW sends a BYE to the Rauland system to terminate the call. The RTP stream is stopped.

### Step 14: Dashboard Update

The web dashboard auto-refreshes at the configured interval (default 10 seconds), picking up the newly recorded call and displaying it in the call table with updated statistics.

---

## 5. Configuration Reference

Configuration is loaded from `config.yaml`. The default path is `/opt/sipgw/config.yaml`, which can be overridden by setting the `SIPGW_CONFIG` environment variable.

### 5.1 Complete config.yaml

```yaml
sip:
  bind_ip: "0.0.0.0"          # IP address to bind the SIP listener
  bind_port: 5060              # SIP port number (used for both UDP and TCP)
  allowed_networks:            # CIDR ranges allowed to send SIP messages
    - "172.16.0.0/12"
    - "127.0.0.0/8"
    - "10.0.0.0/8"
  call_timeout_seconds: 600    # Maximum call duration (seconds) before auto-BYE
  rtp_port_range_start: 10000  # Start of the RTP port range (inclusive)
  rtp_port_range_end: 20000    # End of the RTP port range (inclusive)

fusion:
  base_url: "https://api.icmobile.singlewire.com/api"
  token_url: "https://api.icmobile.singlewire.com/api/token"
  audience: "provider-uuid"              # Provider ID (from Fusion admin console URL)
  scenario_id: "scenario-uuid"           # Scenario UUID to trigger
  scenario_endpoint: "/v1/scenario-notifications"
  variable_name: "customTTS"             # Scenario variable name for TTS content
  scenario_field_id: "field-uuid"        # Field UUID (auto-resolved if left empty)
  client_id: "your-client-id"            # OAuth2 client ID
  client_secret: "your-client-secret"    # OAuth2 client secret

tts:
  play_count: 3                          # Number of times the TTS base message repeats
  message_preamble: "Attention! "        # Prepended once at the very start of the message
  iteration_preamble: "Attention! "      # Prepended before each repetition of the base message

logging:
  log_dir: "/var/log/sipgw"              # Directory for log files
  retention_days: 90                     # Days to retain rotated log files
  rotation_time: "midnight"              # Time of day for log rotation
  timezone: "America/New_York"           # Timezone for log timestamps and rotation
  api_debug_log: true                    # Enable detailed northbound API debug logging

dashboard:
  port: 8080                             # Port for the web dashboard
  bind_ip: "0.0.0.0"                     # IP address to bind the dashboard
  auto_refresh_seconds: 10               # Dashboard auto-refresh interval (seconds)

database:
  path: "/var/lib/sipgw/calls.db"        # Path to the SQLite database file
```

### 5.2 Section Details

#### sip

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `bind_ip` | string | `"0.0.0.0"` | IP address for the SIP listener to bind to. Use `"0.0.0.0"` to listen on all interfaces. |
| `bind_port` | integer | `5060` | Port number for SIP signaling. Both UDP and TCP listeners bind to this port. Standard SIP port is 5060. |
| `allowed_networks` | list of strings | (see above) | List of CIDR notation network ranges. Only SIP messages originating from IPs within these ranges are accepted. All others receive a 403 Forbidden response. |
| `call_timeout_seconds` | integer | `600` | Maximum duration in seconds that a call can remain active before the server automatically sends a BYE to terminate it. Set to 600 (10 minutes) by default. |
| `rtp_port_range_start` | integer | `10000` | The lower bound (inclusive) of the port range used for RTP media streams. Each active call uses one port from this range. |
| `rtp_port_range_end` | integer | `20000` | The upper bound (inclusive) of the port range used for RTP media streams. Ensure that at least as many ports are available as the maximum number of concurrent calls expected. |

#### fusion

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `base_url` | string | (required) | Base URL for the Informacast Fusion API. Typically `https://api.icmobile.singlewire.com/api`. |
| `token_url` | string | (required) | Full URL for the OAuth2 token endpoint. Typically `https://api.icmobile.singlewire.com/api/token`. |
| `audience` | string | (required) | The provider ID from the Fusion admin console. This is the UUID that appears in the admin console URL and is passed as the `audience` parameter in the OAuth2 token request. |
| `scenario_id` | string | (required) | The UUID of the Fusion scenario to trigger. This scenario should be configured with a TTS variable that accepts the announcement text. |
| `scenario_endpoint` | string | `"/v1/scenario-notifications"` | The API path for scenario notification triggering, appended to `base_url`. |
| `variable_name` | string | `"customTTS"` | The name of the variable in the Fusion scenario definition that receives the TTS text. Used for reference and field ID auto-resolution. |
| `scenario_field_id` | string | (optional) | The UUID of the field within the scenario that receives the TTS text. If left empty, the client will automatically resolve this by querying the scenario definition from the Fusion API on the first call. |
| `client_id` | string | (required) | OAuth2 client ID for authenticating with the Fusion API. Obtained from the Fusion admin console. |
| `client_secret` | string | (required) | OAuth2 client secret for authenticating with the Fusion API. Obtained from the Fusion admin console. This value is sensitive and should be protected via file permissions. |

#### tts

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `play_count` | integer | `3` | The number of times the base TTS message is repeated in the final assembled string. Higher values produce a longer announcement. |
| `message_preamble` | string | `"Attention! "` | A string prepended once at the very beginning of the entire assembled TTS message. Appears only at the start, not before each repetition. Include a trailing space if you want separation from the first iteration preamble. |
| `iteration_preamble` | string | `"Attention! "` | A string prepended before each repetition of the base TTS message. Appears before every iteration, including the first. Include a trailing space if you want separation from the base message. |

#### logging

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `log_dir` | string | `"/var/log/sipgw"` | Directory where log files are written. Must be writable by the `sipgw` service user. |
| `retention_days` | integer | `90` | Number of days to retain rotated and compressed log files before automatic deletion. |
| `rotation_time` | string | `"midnight"` | Time of day when log rotation occurs. Uses Python's `TimedRotatingFileHandler` `when` parameter. |
| `timezone` | string | `"America/New_York"` | Timezone used for log timestamps and rotation scheduling. Must be a valid IANA timezone string. |
| `api_debug_log` | boolean | `true` | When `true`, enables the detailed API debug log that captures full HTTP request/response details for all northbound API calls to the Fusion platform. |

#### dashboard

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `port` | integer | `8080` | TCP port for the web dashboard HTTP server. |
| `bind_ip` | string | `"0.0.0.0"` | IP address for the dashboard HTTP server to bind to. Use `"0.0.0.0"` to listen on all interfaces. |
| `auto_refresh_seconds` | integer | `10` | Interval in seconds at which the dashboard HTML page automatically refreshes to show new data. |

#### database

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `path` | string | `"/var/lib/sipgw/calls.db"` | Full file path for the SQLite database file. The directory must exist and be writable by the `sipgw` service user. |

---

## 6. Lookup Tables Reference

Lookup tables are loaded from `lookups.yaml`. The default path is `/opt/sipgw/lookups.yaml`, which can be overridden by setting the `SIPGW_LOOKUPS` environment variable.

### 6.1 Complete lookups.yaml Structure

```yaml
areas:
  710: "3rd Floor. Cardiac Step-Down."
  730: "1st Floor. E.D."
  731: "4th Floor, I.C.U."
  # ... (up to 34 total area mappings)
default_area: "Unknown Area."

call_purposes:
  "Blue": "Code Blue"
  "RRT": "Rapid Response Team"
  "Pink": "Code Pink"
default_purpose: "Code"

rooms:
  208: "Mens' Room"
  209: "Womens' Room"
  # ... add room mappings as needed
default_room_format: "Room {room}."
```

### 6.2 Areas Table

The `areas` table maps numeric area IDs (integers) to human-readable area descriptions (strings). Area IDs are extracted from the SIP username (the digits following `a` in the pattern `a{area}r{room}`).

Each area description typically includes the floor and department/unit name, punctuated with periods for clear TTS pronunciation. For example:

| Area ID | Description |
|---------|-------------|
| 710 | `"3rd Floor. Cardiac Step-Down."` |
| 730 | `"1st Floor. E.D."` |
| 731 | `"4th Floor, I.C.U."` |

If an area ID is not found in the table, the `default_area` value is used (default: `"Unknown Area."`).

### 6.3 Call Purposes Table

The `call_purposes` table maps keyword strings to full alert type labels. The keyword search is performed against the SIP From header display name. The search checks whether the display name contains the keyword as a substring.

| Keyword | Alert Label |
|---------|-------------|
| `"Blue"` | `"Code Blue"` |
| `"RRT"` | `"Rapid Response Team"` |
| `"Pink"` | `"Code Pink"` |

If no keyword is found in the display name, the `default_purpose` value is used (default: `"Code"`).

### 6.4 Rooms Table

The `rooms` table maps specific room numbers (integers) to custom room name strings. This allows overriding the default room format for rooms that have special names.

| Room Number | Custom Name |
|-------------|-------------|
| 208 | `"Mens' Room"` |
| 209 | `"Womens' Room"` |

If a room number is not found in the table, the `default_room_format` template is used, with `{room}` replaced by the actual room number. For example, room 201 would produce `"Room 201."`.

### 6.5 Adding New Entries

To add new lookup entries:

1. Edit `/opt/sipgw/lookups.yaml`.
2. Add the new entry under the appropriate section (`areas`, `call_purposes`, or `rooms`).
3. Restart the service: `sudo systemctl restart sipgw`.

The lookup tables are loaded once at process startup and cached in memory, so a restart is required for changes to take effect.

---

## 7. TTS Composition

The TTS (Text-to-Speech) composition system is responsible for transforming raw SIP caller information into a fully formed announcement string suitable for broadcast over the Informacast Fusion platform. This process occurs in two distinct stages: base string construction and final assembly with preambles and repetition.

### 7.1 Stage 1: build_tts() -- Base String Construction

The `build_tts()` function in `tts_builder.py` takes a `CallerInfo` object (produced by `parser.py`) and constructs the base TTS string by performing three lookups and combining the results.

#### Step 1: Determine Call Purpose

The function `get_call_purpose(display_name)` searches the `call_purposes` lookup table for keyword matches within the display name:

- Input: `"Code Blue Rm 201"` (the SIP From header display name)
- The function iterates through the `call_purposes` keys (`"Blue"`, `"RRT"`, `"Pink"`) and checks if any keyword appears as a substring within the display name.
- `"Blue"` is found in `"Code Blue Rm 201"`, so the result is `"Code Blue"`.
- If no keyword matched, the result would be the `default_purpose` value: `"Code"`.

#### Step 2: Determine Area Name

The function `get_area_name(area_number)` looks up the area ID in the `areas` table:

- Input: `730` (the area number parsed from the SIP username `a730r201`)
- Lookup: `areas[730]` returns `"1st Floor. E.D."`
- If the area ID were not in the table, the result would be the `default_area` value: `"Unknown Area."`

#### Step 3: Determine Room Name

The function `get_room_name(room_number)` looks up the room number in the `rooms` table:

- Input: `201` (the room number parsed from the SIP username `a730r201`)
- Lookup: `rooms[201]` is not present in the table.
- Since no mapping exists, the `default_room_format` template is used: `"Room {room}."` with `{room}` replaced by `201`, yielding `"Room 201."`.

**Room mapping example:** If the room number were `208`, the lookup would find `rooms[208] = "Mens' Room"`, and the result would be `"Mens' Room."` instead of `"Room 208."`. This allows special rooms (restrooms, lobbies, waiting areas) to be announced by their descriptive name rather than their number.

#### Step 4: Combine into Base String

The three components are combined using the format:

```
{purpose}! {area_name} {room_text}
```

Result: `"Code Blue! 1st Floor. E.D. Room 201."`

### 7.2 Stage 2: assemble_tts() -- Preamble and Repetition

The `assemble_tts()` function takes the base TTS string and the TTS configuration (play count, message preamble, iteration preamble) and produces the final assembled string.

#### Assembly Structure

The assembled string follows this structure:

```
{message_preamble}{iteration_preamble}{base} {iteration_preamble}{base} {iteration_preamble}{base} ...
                   |<--- iteration 1 --->|    |<--- iteration 2 --->|    |<--- iteration 3 --->|
```

Key points:

- The **message_preamble** appears exactly once, at the very beginning of the entire assembled string. It is not repeated.
- The **iteration_preamble** appears before every repetition of the base string, including the first iteration.
- The base string is repeated `play_count` times.
- Iterations are separated by a space.

#### Detailed Example with Default Settings

Given:
- `base_tts` = `"Code Blue! 1st Floor. E.D. Room 201."`
- `play_count` = `3`
- `message_preamble` = `"Attention! "`
- `iteration_preamble` = `"Attention! "`

The assembly proceeds as follows:

1. Start with the message preamble: `"Attention! "`
2. Append the iteration preamble for iteration 1: `"Attention! Code Blue! 1st Floor. E.D. Room 201."`
3. Append a space, then the iteration preamble and base for iteration 2: `" Attention! Code Blue! 1st Floor. E.D. Room 201."`
4. Append a space, then the iteration preamble and base for iteration 3: `" Attention! Code Blue! 1st Floor. E.D. Room 201."`

Final assembled string:

```
Attention! Attention! Code Blue! 1st Floor. E.D. Room 201. Attention! Code Blue! 1st Floor. E.D. Room 201. Attention! Code Blue! 1st Floor. E.D. Room 201.
```

Breaking it down visually:

```
[message_preamble][iter_preamble][base]         [iter_preamble][base]         [iter_preamble][base]
Attention!        Attention!     Code Blue!...   Attention!     Code Blue!...  Attention!     Code Blue!...
```

Note that the first `"Attention!"` in the output is the message preamble, and the second `"Attention!"` is the iteration preamble for the first repetition. They appear adjacent because both end/start without additional spacing beyond the trailing space in each preamble string.

#### Example with Different Settings

If `play_count` were set to `1` and `message_preamble` were set to `""` (empty string):

```
Attention! Code Blue! 1st Floor. E.D. Room 201.
```

Only the iteration preamble and a single repetition of the base string would appear.

#### Example with Room Mapping

For a call from username `a730r208` with display name `"Code Blue Restroom"`:

- Purpose: `"Code Blue"` (keyword `"Blue"` found)
- Area: `"1st Floor. E.D."` (area 730)
- Room: `"Mens' Room."` (room 208 is mapped in the rooms table)
- Base TTS: `"Code Blue! 1st Floor. E.D. Mens' Room."`
- Assembled (play_count=3): `"Attention! Attention! Code Blue! 1st Floor. E.D. Mens' Room. Attention! Code Blue! 1st Floor. E.D. Mens' Room. Attention! Code Blue! 1st Floor. E.D. Mens' Room."`

#### Example with RRT Alert

For a call from username `a710r105` with display name `"RRT Cardiac":

- Purpose: `"Rapid Response Team"` (keyword `"RRT"` found)
- Area: `"3rd Floor. Cardiac Step-Down."` (area 710)
- Room: `"Room 105."` (room 105 not in rooms table, uses default format)
- Base TTS: `"Rapid Response Team! 3rd Floor. Cardiac Step-Down. Room 105."`
- Assembled (play_count=3): `"Attention! Attention! Rapid Response Team! 3rd Floor. Cardiac Step-Down. Room 105. Attention! Rapid Response Team! 3rd Floor. Cardiac Step-Down. Room 105. Attention! Rapid Response Team! 3rd Floor. Cardiac Step-Down. Room 105."`

---

## 8. Fusion API Integration

SIPGW communicates with the Informacast Fusion platform via its REST API to trigger scenario-based notifications. This section details the authentication flow and API interaction.

### 8.1 OAuth2 Client Credentials Flow

Fusion uses the OAuth2 client credentials grant type for machine-to-machine authentication.

**Token Request:**

```
POST https://api.icmobile.singlewire.com/api/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
&client_id=<client_id>
&client_secret=<client_secret>
&audience=<audience>
```

- `client_id` and `client_secret` are obtained from the Fusion admin console when creating an API application.
- `audience` is the provider ID, which is the UUID visible in the Fusion admin console URL.

**Token Response:**

```json
{
  "access_token": "eyJ...",
  "token_type": "Bearer",
  "expires_in": 3600
}
```

The access token is cached in memory and reused for subsequent API calls. A new token is requested when the cached token is within 60 seconds of expiration, ensuring there is always a valid token available without risking an expired token on a time-critical call.

### 8.2 Scenario Triggering

Once an access token is available, the scenario is triggered with a POST request:

```
POST https://api.icmobile.singlewire.com/api/v1/scenario-notifications?scenarioId=<scenario_id>
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "fields": [
    {
      "fieldId": "<scenario_field_id>",
      "answer": "<assembled_tts_string>"
    }
  ]
}
```

- `scenarioId` is passed as a query parameter.
- The request body contains a `fields` array with a single entry.
- `fieldId` identifies which variable in the scenario receives the TTS text.
- `answer` contains the fully assembled TTS string.

### 8.3 Field ID Auto-Resolution

If the `scenario_field_id` configuration parameter is left empty, the webhook client will automatically resolve it on the first call by querying the scenario definition from the Fusion API. The client retrieves the scenario configuration, finds the field matching the configured `variable_name`, and caches the field ID for subsequent calls. This simplifies initial configuration by requiring only the scenario ID.

### 8.4 Automatic Token Refresh and Retry

If a scenario trigger request receives a `401 Unauthorized` response, the webhook client:

1. Discards the cached access token.
2. Requests a new access token from the token endpoint.
3. Retries the scenario trigger request with the new token.

This handles cases where the token was revoked or the token endpoint returned a shorter-than-expected expiration.

### 8.5 Required Fusion Configuration

To use SIPGW with Informacast Fusion, the following must be configured in the Fusion admin console:

1. **API Application:** Create an API application with client credentials grant type. Record the client ID and client secret.
2. **Scopes:** The application must have the scope `urn:singlewire:scenario-notifications:write` to be able to trigger scenarios.
3. **Provider ID (Audience):** The provider ID (audience) is the UUID visible in the admin console URL when viewing the provider settings.
4. **Scenario:** Create a scenario with a TTS variable (e.g., named `customTTS`). Record the scenario UUID. The field UUID can either be recorded manually or left empty for auto-resolution.

---

## 9. SIP Implementation Details

SIPGW uses a lightweight, custom SIP implementation built directly on asyncio rather than relying on third-party SIP libraries such as pjsua2 or python-sipsimple. This design decision was made because the requirements are narrow and well-defined: the service only needs to receive inbound calls, hold them with silence, and terminate them after processing. No outbound dialing, call transfer, registration, or complex dialog management is needed.

### 9.1 Supported SIP Methods

| Method | Direction | Behavior |
|--------|-----------|----------|
| INVITE | Inbound | Responds with 100 Trying, then 200 OK with SDP. Starts RTP silence stream. Triggers the processing pipeline. |
| ACK | Inbound | Acknowledges the 200 OK. Confirms dialog establishment. |
| BYE | Inbound | Responds with 200 OK. Stops the RTP stream. Cleans up call state. |
| BYE | Outbound | Sent by SIPGW when the call timeout expires, to terminate the call from the server side. |
| CANCEL | Inbound | Responds with 200 OK. Stops any in-progress processing. Cleans up call state. |
| OPTIONS | Inbound | Responds with 200 OK including capability headers. Supports SIP keep-alive probing. |

### 9.2 Transport

Both UDP and TCP transports are supported on the same configured port (default 5060). The server creates separate asyncio listeners for each transport. SIP messages can arrive on either transport, and responses are sent back on the same transport that received the request.

### 9.3 SDP Offer

The 200 OK response to an INVITE includes an SDP body that offers PCMU (G.711 u-law) audio:

```
v=0
o=sipgw 0 0 IN IP4 <bind_ip>
s=sipgw
c=IN IP4 <bind_ip>
t=0 0
m=audio <rtp_port> RTP/AVP 0
a=rtpmap:0 PCMU/8000
```

The RTP port is randomly selected from the range `[rtp_port_range_start, rtp_port_range_end]`.

### 9.4 IP Filtering

Every inbound SIP request is subject to IP filtering before any processing occurs. The source IP is checked against the `allowed_networks` list using standard CIDR matching. If the source IP is not within any allowed network, the server immediately responds with `403 Forbidden` and discards the request.

This filtering applies to all SIP methods, not just INVITE. This prevents unauthorized endpoints from even probing the server with OPTIONS requests.

---

## 10. RTP Silence Stream

After answering a SIP INVITE with 200 OK, the RTP handler sends a continuous stream of silence packets to keep the call active. This is necessary because the Rauland nurse call system expects an active media stream; without it, the call would be treated as failed.

### 10.1 Packet Structure

Each RTP packet has the following structure:

| Field | Size | Value | Description |
|-------|------|-------|-------------|
| Version | 2 bits | 2 | RTP version 2 |
| Padding | 1 bit | 0 | No padding |
| Extension | 1 bit | 0 | No extension |
| CSRC Count | 4 bits | 0 | No CSRC |
| Marker | 1 bit | varies | Set on first packet |
| Payload Type | 7 bits | 0 | PCMU (G.711 u-law) |
| Sequence Number | 16 bits | increments | Increments by 1 per packet |
| Timestamp | 32 bits | increments | Increments by 160 per packet |
| SSRC | 32 bits | random | Random synchronization source ID |
| Payload | 160 bytes | 0xFF | u-law silence |

**Total packet size:** 12 bytes (header) + 160 bytes (payload) = 172 bytes per packet.

### 10.2 Timing

- **Interval:** 20 milliseconds between packets.
- **Sample Rate:** 8000 Hz (standard for PCMU).
- **Samples per Packet:** 160 (8000 Hz * 0.020 seconds = 160 samples).
- **Timestamp Increment:** 160 per packet (matching the sample count).

### 10.3 Silence Encoding

In the G.711 u-law codec, the byte value `0xFF` represents a sample value of zero (silence). All 160 bytes of the payload are set to `0xFF`, producing a continuous silence signal.

---

## 11. Dashboard

The web dashboard provides a browser-based monitoring interface for the SIPGW service. It is built with FastAPI and Jinja2 templates, styled with a dark theme for comfortable viewing in NOC/operations environments.

### 11.1 Accessing the Dashboard

The dashboard is available at:

```
http://<hostname>:8080
```

The port and bind address are configurable in `config.yaml` under the `dashboard` section.

### 11.2 Dashboard Features

#### Call History Table

The main view displays a table of all processed calls with the following columns:

| Column | Description |
|--------|-------------|
| Timestamp | Date and time the call was received |
| Caller | Raw SIP From header information |
| Area | Resolved area name from the lookup table |
| Room | Resolved room name/number |
| TTS | The generated TTS announcement string |
| Fusion Status | HTTP status code from the Fusion API response |
| Response Time | Elapsed time for the Fusion API call |

The table is sorted by timestamp in descending order, with the most recent calls at the top.

#### Statistics Cards

Three summary cards are displayed above the call table:

- **Total Calls:** Count of all calls processed since the database was created.
- **Successful Calls:** Count of calls where the Fusion API returned a successful response.
- **Failed Calls:** Count of calls where the Fusion API returned an error or the request failed.

#### Recent Logs Panel

Displays the last 50 lines of `/var/log/sipgw/sipgw.log` in a scrollable panel. A **Copy** button allows copying the log content to the clipboard for easy pasting into support tickets or chat messages.

#### API Debug Log Panel

Displays the last 50 lines of `/var/log/sipgw/sipgw_api_debug.log` in a scrollable panel with an orange-themed color scheme to visually distinguish it from the main log panel. A **Copy** button is also provided.

#### Auto-Refresh

The dashboard page automatically refreshes at the interval specified by `dashboard.auto_refresh_seconds` (default 10 seconds). This ensures that new calls and log entries appear without manual page refresh.

### 11.3 API Endpoints

#### GET /api/calls

Returns call history as a JSON array.

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | integer | 100 | Maximum number of calls to return |

**Example Response:**

```json
[
  {
    "timestamp": "2026-02-19T14:30:00",
    "caller": "Code Blue Rm 201 <sip:a730r201@...>",
    "area": "1st Floor. E.D.",
    "room": "Room 201.",
    "tts": "Attention! Attention! Code Blue! ...",
    "fusion_status": 200,
    "response_time_ms": 342
  }
]
```

#### GET /health

Returns a simple health check response.

**Example Response:**

```json
{
  "status": "healthy"
}
```

---

## 12. Logging

SIPGW uses a dual-output logging system with daily rotation, compression, and retention management.

### 12.1 Main Application Log

- **File:** `/var/log/sipgw/sipgw.log`
- **Output:** All log messages are written to both this file and stdout (visible via `journalctl`).
- **Rotation:** The log file rotates daily at midnight (configurable) in the configured timezone.
- **Compression:** Rotated log files are compressed into `.tgz` archives to conserve disk space.
- **Retention:** Compressed log archives are retained for the configured number of days (default 90), after which they are automatically deleted.

### 12.2 API Debug Log

- **File:** `/var/log/sipgw/sipgw_api_debug.log`
- **Enabled:** Only when `logging.api_debug_log` is set to `true` in the configuration.
- **Purpose:** Captures detailed information about every HTTP exchange between SIPGW and the Fusion API.

For every HTTP request/response cycle, the following is logged:

**REQUEST details:**
- HTTP method (GET, POST, etc.)
- Full URL including query parameters
- All request headers, with sensitive values (Authorization bearer tokens, client secrets) masked
- Full request body (JSON formatted for readability)

**REDIRECT details (if any):**
- Intermediate redirect hops with status codes and Location headers

**RESPONSE details:**
- HTTP status code and reason phrase
- All response headers
- Full response body
- Elapsed time for the request

**ERROR details (if any):**
- Full stack traces for connection errors, timeouts, or unexpected exceptions

### 12.3 Viewing Logs

```bash
# Follow the main application log in real time
tail -f /var/log/sipgw/sipgw.log

# Follow the API debug log in real time
tail -f /var/log/sipgw/sipgw_api_debug.log

# View service logs via journalctl
journalctl -u sipgw -f

# View recent log entries (last 100 lines)
journalctl -u sipgw -n 100

# View logs since a specific time
journalctl -u sipgw --since "2026-02-19 14:00:00"
```

---

## 13. Database

SIPGW uses SQLite (via the `aiosqlite` async driver) to persist call history. The database allows the dashboard to display historical call data and provides a record of all alerts processed by the system.

### 13.1 Database Location

The database file is stored at the path configured in `database.path` (default: `/var/lib/sipgw/calls.db`). The directory is created by the installation script with appropriate ownership for the `sipgw` user.

### 13.2 Schema

The database schema is automatically created on first run. The call history table stores the following information for each processed call:

- Timestamp of call receipt
- Raw caller information from the SIP From header
- Parsed area number and resolved area name
- Parsed room number and resolved room name
- Generated base and assembled TTS strings
- Fusion API response status code
- Fusion API response time

### 13.3 Data Retention

The SQLite database does not have automatic data retention. Over time, the database file will grow as more calls are recorded. If the database becomes too large, the file can be deleted and will be automatically recreated on the next service restart. Alternatively, old records can be removed using standard SQLite tools.

---

## 14. Installation

### 14.1 Prerequisites

- **Operating System:** Ubuntu 22.04 or later
- **Python:** 3.11 or later
- **Network:** Port 5060 (SIP) and port 8080 (dashboard) must be accessible
- **Permissions:** Root access is required for installation (systemd service, user creation, directory setup)

### 14.2 Installation Procedure

1. Ensure the source files are present at `/opt/sipgw/`.

2. Run the installation script:

```bash
sudo bash /opt/sipgw/install.sh
```

The installation script performs the following:

- Creates a dedicated `sipgw` system user for running the service.
- Creates the log directory at `/var/log/sipgw/` with ownership set to the `sipgw` user.
- Creates the data directory at `/var/lib/sipgw/` with ownership set to the `sipgw` user.
- Creates a Python virtual environment at `/opt/sipgw/venv/`.
- Installs Python dependencies from `/opt/sipgw/requirements.txt` into the virtual environment.
- Installs the systemd service unit file from `/opt/sipgw/sipgw.service`.
- Enables the service for automatic start on boot.
- Grants the `CAP_NET_BIND_SERVICE` capability to the Python interpreter so the service can bind to port 5060 without running as root.

3. Configure the service:

```bash
sudo nano /opt/sipgw/config.yaml
```

At a minimum, configure the following:

- `fusion.client_id` and `fusion.client_secret` with your Fusion API credentials.
- `fusion.audience` with your Fusion provider ID.
- `fusion.scenario_id` with the UUID of the scenario to trigger.
- `sip.allowed_networks` with the CIDR ranges of your Rauland nurse call system.

4. Configure the lookup tables:

```bash
sudo nano /opt/sipgw/lookups.yaml
```

Populate the `areas`, `call_purposes`, and `rooms` tables with values appropriate for your facility.

5. Start the service:

```bash
sudo systemctl start sipgw
```

6. Verify the service is running:

```bash
systemctl status sipgw
```

7. Open the dashboard in a browser:

```
http://<hostname>:8080
```

---

## 15. Service Management

SIPGW runs as a systemd service named `sipgw`.

### 15.1 Common Commands

```bash
# Start the service
sudo systemctl start sipgw

# Stop the service
sudo systemctl stop sipgw

# Restart the service (e.g., after configuration changes)
sudo systemctl restart sipgw

# Check service status
systemctl status sipgw

# Enable automatic start on boot
sudo systemctl enable sipgw

# Disable automatic start on boot
sudo systemctl disable sipgw

# Follow service logs via journalctl
journalctl -u sipgw -f

# Follow the application log file
tail -f /var/log/sipgw/sipgw.log

# Follow the API debug log file
tail -f /var/log/sipgw/sipgw_api_debug.log
```

### 15.2 When to Restart

The service must be restarted after any of the following changes:

- Modifications to `config.yaml`.
- Modifications to `lookups.yaml` (lookup tables are cached at startup).
- Python code changes.
- To clear a stale cached OAuth2 token.

### 15.3 Dashboard Access

The web dashboard is accessible at:

```
http://<hostname>:8080
```

Replace `<hostname>` with the server's IP address or DNS name. The dashboard port is configurable via `dashboard.port` in `config.yaml`.

---

## 16. Security

### 16.1 SIP IP Filtering

The `sip.allowed_networks` configuration parameter defines a list of CIDR network ranges. Only SIP messages originating from IP addresses within these ranges are accepted. All other messages receive a `403 Forbidden` response. This prevents unauthorized SIP endpoints from triggering alerts.

Ensure that the CIDR ranges cover all IP addresses that the Rauland nurse call system may use to send SIP messages, including any NAT gateways or SIP proxies in the path.

### 16.2 Systemd Hardening

The systemd service unit file includes the following security hardening directives:

| Directive | Effect |
|-----------|--------|
| `NoNewPrivileges=true` | Prevents the service process and its children from gaining new privileges. |
| `ProtectSystem=strict` | Mounts the entire filesystem read-only, except for explicitly allowed paths. |
| `ProtectHome=true` | Makes the `/home`, `/root`, and `/run/user` directories inaccessible. |
| `PrivateTmp=true` | Provides a private `/tmp` directory isolated from other services. |

These directives limit the potential impact of a security vulnerability in the SIPGW service.

### 16.3 File Permissions

The `config.yaml` file should have permissions `640` (owner read/write, group read, no world access) because it contains the OAuth2 client secret. The file should be owned by `root:sipgw` so that the service can read it but unprivileged users cannot.

```bash
sudo chown root:sipgw /opt/sipgw/config.yaml
sudo chmod 640 /opt/sipgw/config.yaml
```

### 16.4 Dedicated Service User

The service runs as the dedicated `sipgw` system user, created during installation. This user has minimal privileges and no login shell. Running as a non-root user limits the potential damage from a compromised process.

### 16.5 Port Binding

SIP uses the well-known port 5060, which is below 1024 and normally requires root privileges to bind. Instead of running the service as root, the `CAP_NET_BIND_SERVICE` Linux capability is granted to the Python interpreter in the virtual environment. This allows the `sipgw` user to bind to port 5060 without needing root access.

### 16.6 API Secret Masking

When API debug logging is enabled, sensitive values in HTTP headers (such as the `Authorization` bearer token and any occurrences of the client secret) are automatically masked in the log output. This prevents accidental exposure of credentials in log files.

### 16.7 Concurrency Safety

The OAuth2 token refresh operation is protected by an asyncio lock to prevent concurrent SIP calls from triggering duplicate token requests. The lock uses a double-check pattern: the token validity is checked before and after acquiring the lock to avoid unnecessary blocking.

### 16.8 XSS Protection

The dashboard uses Jinja2 template rendering with `autoescape=True`, which HTML-escapes all variable output. This prevents cross-site scripting attacks if log entries or call data contain HTML/JavaScript content.

### 16.9 Resource Cleanup

RTP port allocation is wrapped in exception handling to ensure allocated ports are freed back to the pool if call setup fails for any reason after allocation. This prevents port exhaustion under error conditions.

### 16.10 Input Validation

The OAuth2 token response is validated for proper JSON structure and the presence of the required `access_token` field before caching. Non-JSON or malformed responses produce clear error messages rather than unhandled exceptions.

---

## 17. Testing

SIPGW includes a comprehensive test suite with 105 tests across 9 test files. The tests cover unit tests for individual modules, functional tests for the integrated pipeline, and system-level tests using real UDP sockets.

### 17.1 Running Tests

```bash
# Run the full test suite with verbose output
/opt/sipgw/venv/bin/python -m pytest tests/ -v

# Run a specific test file
/opt/sipgw/venv/bin/python -m pytest tests/test_parser.py -v

# Run tests with output capture disabled (to see print statements)
/opt/sipgw/venv/bin/python -m pytest tests/ -v -s
```

### 17.2 Test Files

| Test File | Count | Coverage |
|-----------|-------|----------|
| `test_parser.py` | 11 | SIP username parsing: valid patterns, missing components, edge cases, optional bed number |
| `test_lookups.py` | 13 | Area lookups, purpose lookups (keyword matching, defaults), room lookups (mapped and unmapped rooms) |
| `test_tts_builder.py` | 16 | TTS base string building, room mapping integration, assembly with preambles, repetition counts, edge cases |
| `test_sip_message.py` | 10 | SIP message parsing (INVITE, BYE, OPTIONS), header extraction, response construction, SDP parsing |
| `test_rtp.py` | 10 | RTP packet construction, header fields, payload content, timestamp/sequence number incrementing |
| `test_webhook.py` | 4 | OAuth2 token acquisition (mocked), scenario triggering (mocked), automatic retry on 401 |
| `test_dashboard.py` | 7 | FastAPI dashboard endpoints (HTML page, /api/calls JSON, /health check), response content validation |
| `test_functional.py` | 14 | End-to-end pipeline tests (SIP parsing through TTS assembly), database operations, config loading, assembly pipeline integration |
| `test_system.py` | 3 | Real UDP socket tests: full INVITE handling, unauthorized IP rejection (403 response), OPTIONS keep-alive response |

### 17.3 Test Architecture

- **Unit tests** (`test_parser.py`, `test_lookups.py`, `test_tts_builder.py`, `test_sip_message.py`, `test_rtp.py`) test individual modules in isolation with known inputs and expected outputs.
- **Mocked integration tests** (`test_webhook.py`, `test_dashboard.py`) test modules that interact with external systems, using mocks to simulate HTTP responses and database queries.
- **Functional tests** (`test_functional.py`) test the integrated pipeline from SIP parsing through TTS assembly, using real module code but controlled inputs.
- **System tests** (`test_system.py`) send real SIP messages over UDP sockets to a running (or locally spawned) SIP server instance, validating end-to-end behavior including network transport.

---

## 18. Troubleshooting

### 18.1 Service Won't Start

**Symptoms:** `systemctl start sipgw` fails, or the service starts and immediately exits.

**Diagnostic Steps:**

1. Check the journal for error messages:
   ```bash
   journalctl -u sipgw -n 50
   ```

2. Verify `config.yaml` syntax:
   ```bash
   /opt/sipgw/venv/bin/python -c "import yaml; yaml.safe_load(open('/opt/sipgw/config.yaml'))"
   ```

3. Check if port 5060 is already in use:
   ```bash
   ss -tlnp | grep 5060
   ss -ulnp | grep 5060
   ```

4. Check if port 8080 is already in use:
   ```bash
   ss -tlnp | grep 8080
   ```

5. Verify the Python virtual environment:
   ```bash
   /opt/sipgw/venv/bin/python -c "import sipgw"
   ```

### 18.2 401 Unauthorized from Fusion API

**Symptoms:** Calls are received and parsed correctly, but the Fusion API returns 401. The API debug log shows the 401 response.

**Causes and Solutions:**

- **Invalid credentials:** Verify that `fusion.client_id` and `fusion.client_secret` in `config.yaml` match the values in the Fusion admin console.
- **Disabled application:** Ensure the API application is enabled in the Fusion admin console.
- **Wrong audience:** Verify that `fusion.audience` matches the provider ID from the admin console URL.
- **Expired credentials:** If the client secret was rotated in the admin console, update `config.yaml` with the new secret and restart the service.

### 18.3 403 Forbidden from Fusion API

**Symptoms:** Authentication succeeds (token is acquired), but the scenario trigger request returns 403.

**Causes and Solutions:**

- **Missing scope:** The API application needs the scope `urn:singlewire:scenario-notifications:write`. Add this scope in the Fusion admin console and restart SIPGW to obtain a new token with the correct scope.

### 18.4 TTS Not Playing on Speakers

**Symptoms:** The Fusion API returns a success response, but no audio plays through the speakers/paging system.

**Diagnostic Steps:**

1. Check the API debug log to verify the `answer` field content:
   ```bash
   tail -50 /var/log/sipgw/sipgw_api_debug.log
   ```

2. Verify that `fusion.scenario_field_id` is correct. If auto-resolved, check the debug log for the resolved field ID and compare it with the scenario definition in the Fusion admin console.

3. Test the scenario manually from the Fusion admin console to confirm it works independently of SIPGW.

4. Verify the scenario is configured to use the TTS variable for audio output.

### 18.5 SIP Calls Rejected (403 Forbidden)

**Symptoms:** The Rauland system reports that calls to SIPGW are rejected. The SIPGW log shows 403 responses.

**Causes and Solutions:**

- **IP not allowed:** Check that the source IP of the Rauland system is included in the `sip.allowed_networks` CIDR ranges in `config.yaml`.
- **NAT/proxy:** If the Rauland system connects through a NAT gateway or SIP proxy, add the NAT/proxy IP to the allowed networks, not the original station IP.
- Verify the current allowed networks:
  ```bash
  grep -A 10 "allowed_networks" /opt/sipgw/config.yaml
  ```

### 18.6 Dashboard Not Loading

**Symptoms:** Browsing to `http://<hostname>:8080` returns a connection error or timeout.

**Diagnostic Steps:**

1. Verify the service is running:
   ```bash
   systemctl status sipgw
   ```

2. Check if port 8080 is listening:
   ```bash
   ss -tlnp | grep 8080
   ```

3. Check firewall rules:
   ```bash
   sudo ufw status
   sudo iptables -L -n | grep 8080
   ```

4. Try accessing from localhost:
   ```bash
   curl http://localhost:8080/health
   ```

### 18.7 Stale OAuth2 Token

**Symptoms:** The Fusion API returns authentication errors even though the credentials are correct. This can happen if the token was cached before a credential rotation, or if the system clock drifted.

**Solution:** Restart the service to clear the cached token:

```bash
sudo systemctl restart sipgw
```

### 18.8 No Calls Appearing in Database/Dashboard

**Symptoms:** SIP calls are received (visible in logs) but do not appear in the dashboard or database.

**Diagnostic Steps:**

1. Check the database file exists and is writable:
   ```bash
   ls -la /var/lib/sipgw/calls.db
   ```

2. Check for database errors in the log:
   ```bash
   grep -i "database\|sqlite" /var/log/sipgw/sipgw.log
   ```

3. Verify the database directory exists:
   ```bash
   ls -la /var/lib/sipgw/
   ```

---

## 19. Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SIPGW_CONFIG` | `/opt/sipgw/config.yaml` | Path to the main configuration file. Set this to use a configuration file at a non-default location. |
| `SIPGW_LOOKUPS` | `/opt/sipgw/lookups.yaml` | Path to the lookup tables file. Set this to use a lookup file at a non-default location. |

These environment variables can be set in the systemd service unit file, in the shell environment, or via a systemd override file:

```bash
sudo systemctl edit sipgw
```

Add:

```ini
[Service]
Environment="SIPGW_CONFIG=/etc/sipgw/config.yaml"
Environment="SIPGW_LOOKUPS=/etc/sipgw/lookups.yaml"
```

Then reload and restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart sipgw
```

---

## 20. Uninstallation

To remove SIPGW from the system:

```bash
sudo bash /opt/sipgw/uninstall.sh
```

The uninstallation script performs the following:

1. Stops the `sipgw` systemd service if it is running.
2. Disables the service from automatic startup.
3. Removes the systemd unit file.
4. Reloads the systemd daemon configuration.
5. Optionally removes data and log directories (the script will prompt or provide flags):
   - `/var/log/sipgw/` (log files and compressed archives)
   - `/var/lib/sipgw/` (SQLite database)

Note that the uninstall script does not remove the `/opt/sipgw/` directory itself or the `sipgw` system user. These can be removed manually if desired:

```bash
sudo rm -rf /opt/sipgw
sudo userdel sipgw
```

---

## 21. File Layout

```
/opt/sipgw/
├── config.yaml           # Main service configuration
├── lookups.yaml          # Area, purpose, and room lookup tables
├── requirements.txt      # Python package dependencies
├── sipgw.service         # systemd unit file
├── install.sh            # Installation script
├── uninstall.sh          # Uninstallation script
├── sipgw/                # Python package (application code)
│   ├── __init__.py       # Package initialization
│   ├── config.py         # Typed dataclass configuration loader
│   ├── main.py           # Entry point and SIPGateway orchestrator class
│   ├── sip_server.py     # Asyncio SIP server (UDP + TCP, IP filtering)
│   ├── sip_message.py    # SIP message parser and response builder
│   ├── rtp_handler.py    # RTP silence stream sender (PCMU/8000)
│   ├── parser.py         # SIP From header parser (area/room/bed extraction)
│   ├── lookups.py        # Lookup table loader and cache
│   ├── tts_builder.py    # TTS string builder and assembler
│   ├── webhook.py        # Fusion API client (OAuth2 + scenario triggering)
│   ├── database.py       # SQLite database interface (aiosqlite)
│   ├── dashboard.py      # FastAPI web dashboard
│   └── logging_config.py # Logging configuration (dual output, rotation)
├── tests/                # Test suite (105 tests across 9 files)
│   ├── test_parser.py
│   ├── test_lookups.py
│   ├── test_tts_builder.py
│   ├── test_sip_message.py
│   ├── test_rtp.py
│   ├── test_webhook.py
│   ├── test_dashboard.py
│   ├── test_functional.py
│   └── test_system.py
├── docs/                 # Documentation
│   └── SIPGW_SERVICE_MANUAL.md
└── venv/                 # Python virtual environment

/var/log/sipgw/
├── sipgw.log             # Main application log (daily rotation, .tgz compression)
└── sipgw_api_debug.log   # Detailed northbound API debug log

/var/lib/sipgw/
└── calls.db              # SQLite call history database
```

---

*End of SIPGW Service Manual*
