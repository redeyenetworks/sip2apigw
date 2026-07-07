# Glossary & Terminology

This glossary defines the SIP, networking, clinical, and platform terms used throughout the **RedEye sip2api Gateway** manual. It is written for a mixed audience — clinical/telecom staff and IT/network engineers at Tift Regional Medical Center, plus RedEye support — so every entry defines its jargon in plain language and, where useful, ties the term to how the gateway actually uses it.

Unless a term is explicitly labeled **(roadmap)** or **(planned)**, every definition below describes **current production behavior in product v1.5.1**. Terms that belong to the in-progress v1.6.0 reliability release or the future high-availability (HA) design are labeled as such and are **not** current behavior.

> **One-sentence orientation:** A Rauland nurse-call station places a **SIP INVITE** to the gateway; the gateway answers, reads the caller's area/room/bed and call purpose, builds a spoken (**TTS**) message, and triggers an **InformaCast Fusion** overhead page — "SIP in, page out."

---

## Conventions used in this glossary

- **(roadmap)** / **(planned)** — a v1.6.0 or HA capability that is designed or partially coded but **not** part of the deployed v1.5.1 behavior.
- `<CLIENT_ID>` / `<CLIENT_SECRET>` — placeholders. Real OAuth2 credential values never appear in this manual.
- **Northbound** — traffic from the gateway *up* to InformaCast Fusion (HTTPS/443). **Southbound** — traffic from Rauland *down* to the gateway (SIP/5060).

---

## A

**ACK**
: The third message of the SIP three-way INVITE handshake (INVITE → 200 OK → **ACK**). The caller sends ACK to confirm it received the gateway's `200 OK`, finalizing the dialog. In this gateway the ACK is effectively a formality: the gateway answers and immediately tears the call down (see **Immediate-BYE**).

**Allowlist (IP allowlist)**
: The set of source networks permitted to send SIP to the gateway, expressed in CIDR notation. In production the allowlist is `172.16.0.0/12`. Any SIP packet from outside the allowed networks is rejected with `403 Forbidden` and dropped before processing. This is the gateway's primary southbound access control.

**Area**
: The first numeric field parsed from the SIP caller username (`a{area}`), identifying the physical zone the alert came from (e.g. area `730` → "1st Floor. E.D."). Areas are translated to spoken location text via the `areas` table in **lookups**. Unknown areas fall back to `default_area` ("Unknown Area.").

**Area/room/bed**
: The three-part location identity encoded in the SIP caller username as `a{area}*r{room}*b{bed}` (bed optional). Parsed by the gateway into a `CallerInfo` record and turned into human-readable location text for the page. Example username `a730*r201` → area 730, room 201, no bed.

**Audience (OAuth2)**
: A parameter in the OAuth2 token request identifying the target API tenant — here, the Fusion **provider ID** (a customer-owned UUID from the Fusion admin console). It scopes the issued token to Tift's Fusion provider.

## B

**Bed**
: The optional third location field (`b{bed}`) in the SIP caller username, identifying a specific bed within a room. Omitted when the nurse-call station is not bed-specific.

**BYE**
: The SIP request that ends an established call/dialog; the receiver answers `200 OK`. The gateway sends a BYE almost immediately after answering (see **Immediate-BYE**) because it needs no live audio — it only needs the INVITE's caller data to fire a page.

## C

**Call-ID**
: A globally-unique SIP header identifying a single call/dialog; every message in one call (INVITE, ACK, BYE) carries the same Call-ID. The gateway uses it to correlate messages of one transaction in logs. *(roadmap: in the HA design, Call-ID is the persistence key that pins one call to one gateway node.)*

**CANCEL**
: A SIP request that aborts an INVITE **before** it has been answered. The gateway responds `200 OK` and cleans up any pending call state.

**Code Blue**
: The highest-acuity clinical alert this gateway carries — a patient in cardiopulmonary arrest requiring an immediate resuscitation response. Encoded in the SIP display name (keyword "Blue") and mapped to the spoken label "Code Blue." The gateway's core reliability principle exists for this event: **a duplicate page is acceptable; a missed page is never acceptable.**

**CSeq (Command Sequence)**
: A SIP header pairing a sequence number with a method name (e.g. `1 INVITE`, `2 BYE`); it orders requests within a dialog and matches responses to requests.

## D

**Dialog**
: A peer-to-peer SIP relationship between two endpoints, established by a successful INVITE/200 OK/ACK exchange and identified by the Call-ID plus the From and To tags. The gateway establishes a dialog only long enough to answer and immediately end it.

**Display name**
: The human-readable label in the SIP `From` header, before the `<sip:...>` URI (e.g. `"Code Blue Rm 201"`). The gateway substring-matches it against the **call_purposes** table to determine the alert type (Code Blue / RRT / Code Pink).

**Duplicate INVITE**
: A repeated INVITE for the same clinical event, commonly seen here because the Rauland source **double-emits** — roughly one-third or more of events arrive as duplicates. This is expected upstream behavior, **not** a gateway defect. Consistent with the at-least-once principle, the gateway currently delivers each INVITE it receives (a duplicate page is preferred over a missed one). *(roadmap: v1.6.0 adds shadow-only clinical de-duplication telemetry — see **Dedupe**.)*

**Dedupe (clinical de-duplication)** *(roadmap, v1.6.0)*
: A planned observe-only feature that fingerprints the clinical `(area, room, bed, purpose)` tuple to *measure* how often true duplicate pages arrive. It ships inert (never suppresses a page) and is explicitly out of scope for v1.5.1, where no de-duplication occurs.

## E

**Event-ID / event correlation**
: An identifier used to tie together the log lines, database row, and page for a single nurse-call event. In v1.5.1 correlation is done through the SIP **Call-ID** and the database record for the call. *(roadmap: v1.6.0 introduces an explicit, stable INVITE transaction fingerprint (`v1:<hex>`) derived from Call-ID / From / CSeq for tighter correlation.)*

**Egress (northbound egress)**
: The gateway's outbound HTTPS connection on **port 443** to `api.icmobile.singlewire.com`, used to fetch OAuth2 tokens and trigger Fusion scenarios.

## F

**Fusion** → see **InformaCast Fusion**.

**`fusion_status`**
: The field recorded per call capturing the outcome of the northbound Fusion delivery. A successful trigger records the HTTP status; a delivery exception records **`fusion_status = -1`** with no durable retry in v1.5.1. The 2026-06-12 lost Code Blue is an example: a transient connect-timeout during the inline token fetch produced `fusion_status = -1`.

## I

**Immediate-BYE**
: The gateway's defining call-handling behavior: it answers the INVITE with `200 OK` and then **immediately sends a BYE** (no media exchanged, roughly one second end-to-end). The gateway needs only the INVITE's headers to build a page, so it does not hold the call open. This mirrors the Rauland nurse-call receiver's own behavior; the `call_timeout` is 1 second.

**INVITE**
: The SIP request that initiates a call. The Rauland station sends an INVITE to the gateway carrying the caller username (area/room/bed) and display name (purpose). The INVITE is the trigger for the entire page pipeline.

**InformaCast Fusion (Singlewire)**
: The mass-notification / overhead-paging platform (from vendor **Singlewire**) that actually broadcasts the audible page. The gateway is a *client* of Fusion: it authenticates via OAuth2 and triggers a pre-built **scenario** whose spoken content is the gateway-composed TTS string. Fusion base URL: `https://api.icmobile.singlewire.com/api`.

**IP allowlist** → see **Allowlist**.

## L

**Lookups (`lookups.yaml`)**
: The customer-owned translation tables that turn coded SIP fields into spoken English. Three tables: **areas** (area ID → location text), **call_purposes** (display-name keyword → alert label, e.g. "Blue" → "Code Blue"), and **area_rooms** / **rooms** (specific room mappings), each with a default fallback. Editing this file changes what the overhead page says without a code change.

## O

**OAuth2 client-credentials flow**
: The machine-to-machine authentication the gateway uses against Fusion. It POSTs a `<CLIENT_ID>` / `<CLIENT_SECRET>` plus the **audience** (provider ID) to the token endpoint and receives a bearer access token, which it then presents on the scenario-trigger call. No user is involved — the gateway *is* the client. In v1.5.1 the token is fetched **inline** on the page path (and refreshed once on a `401`); this inline fetch is one source of occasional multi-second deliveries and was implicated in the 2026-06-12 incident. *(roadmap: v1.6.0 moves token refresh to a background loop off the hot path.)*

**Outbox** *(roadmap, v1.6.0)*
: A planned durable, record-first delivery store: every page would be written to the database as `pending` **before** any network send, then driven through a retry/backoff state machine (`pending → delivering → delivered | failed | expired`) by a background worker. **This does not exist in v1.5.1** — today's delivery is a single inline attempt with no durable retry. The outbox is the central fix for the missed-page failure mode.

## P

**Proxy (SIP proxy)**
: The intermediary that forwards Rauland's SIP toward the gateway. In the production path the Rauland **UAC** (`172.20.9.170`) sends to a proxy (`172.20.9.176`), which is the actual packet source and `Contact` seen by the gateway (`10.249.0.60`). Knowing the proxy is the packet source matters for reading SIP logs and for the allowlist.

**Purpose (call purpose)**
: The clinical alert type of the event (Code Blue, RRT, Code Pink), determined by keyword-matching the SIP display name against the **call_purposes** table. An unrecognized purpose falls back to `default_purpose`.

## R

**Room**
: The second numeric field parsed from the SIP caller username (`r{room}`), identifying the room within an area. Rendered in the page as a mapped room name if one exists in **lookups**, otherwise as `default_room_format` ("Room {room}.").

**RRT (Rapid Response Team)**
: A clinical alert requesting a rapid-response team for a patient who is deteriorating but not yet in full arrest — a step below Code Blue. Encoded via the display-name keyword "RRT" → spoken label "Rapid Response Team."

**RTP (Real-time Transport Protocol)**
: The protocol that carries live audio in a normal SIP call. This gateway exchanges **no** meaningful audio — it answers and immediately ends the call (see **Immediate-BYE**), so RTP is not a functional part of v1.5.1 paging. (Where a silence stream is referenced elsewhere, it exists only to satisfy media negotiation, not to carry a page; the page itself is spoken by Fusion, not over RTP.)

## S

**Scenario / scenario-notification**
: A pre-configured notification template inside InformaCast Fusion. The gateway does not build the page's routing or audio path itself — it **triggers** the customer's scenario (name "SIPtoTTSBridge", id `4cba52d8-0d50-11f1-aba0-913f93e445e2`) via the scenario-notification API, passing the composed announcement into the scenario's `customTTS` field variable. Fusion then speaks it (voice "Joanna") to the "Tifton/TRMC/Main Hospital" device group.

**SDP (Session Description Protocol)**
: The small text block carried inside SIP INVITE/200 OK messages that describes the proposed media session (codecs, IP, ports). Because the gateway does immediate-BYE, SDP is negotiated as a formality and no real media session is used.

**SIP (Session Initiation Protocol)**
: The signaling protocol (RFC 3261) used to set up, modify, and tear down real-time sessions such as calls. Rauland uses SIP to *signal* a nurse-call alert to the gateway. The gateway binds SIP on `0.0.0.0:5060` over **both UDP and TCP**.

**Singlewire** → see **InformaCast Fusion**.

**SQLite**
: The embedded, file-based database (`/var/lib/sipgw/calls.db`) where the gateway records every processed call — timestamp, caller info, parsed area/room, the TTS string, and the Fusion result. It backs the dashboard's call history.

**systemd**
: The Linux service manager that runs the gateway as a single unit, `sipgw.service` (Type=simple, `Restart=always`, `RestartSec=5`), under the unprivileged `sipgw` user with hardening (`NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`) and `CAP_NET_BIND_SERVICE` so it can bind port 5060. systemd restarts the service automatically if it exits.

## T

**481 "Call Leg/Transaction Does Not Exist"**
: A specific, harmless SIP error occasionally seen from a **BYE-before-ACK** race — the gateway's immediate-BYE arrives before the caller's ACK, so the peer replies `481 Call Leg/Transaction Does Not Exist`. The page is still delivered; no action is needed. (This exact reason phrase is used consistently across the manual.)

**TTS (text-to-speech)**
: The spoken announcement. The gateway *composes the text* (e.g. "Attention! Attention! Code Blue! 1st Floor. E.D. Room 201. …") by combining the parsed purpose, area, and room via **lookups**, with configurable preambles and repetition; **Fusion** performs the actual speech synthesis (voice "Joanna") and broadcast.

## U

**UAC (User Agent Client)**
: The SIP endpoint that *originates* a request. Here the Rauland nurse-call system (`172.20.9.170`) is the UAC that emits the INVITE; the gateway acts as the receiving user agent (server side) for that call.

**UTC (timestamps)**
: All log and record timestamps are currently in **UTC**. The `logging.timezone` config value is present but **not applied**; the host clock runs UTC, so timestamps are UTC in effect. This is a known item. *(roadmap: v1.6.0 issue #12 introduces canonical RFC3339-`Z` UTC timestamps with host-local display.)*

## W

**Watchdog / heartbeat / real `/health`** *(roadmap, v1.6.0)*
: Planned liveness machinery — a writer heartbeat row, a systemd `Type=notify` watchdog, and a `/health` endpoint backed by that heartbeat. **Not present in v1.5.1.** Today the service relies on systemd `Restart=always` for recovery.

**WAL (Write-Ahead Logging)** *(roadmap, v1.6.0)*
: A SQLite journaling mode planned alongside the **outbox** to let a read-only dashboard read consistently while the writer commits. v1.5.1 does not run the database in WAL mode.

---

## Related reading

- **Overview & Architecture** — how these pieces fit together end to end.
- **Call Flow** — the step-by-step lifecycle of one INVITE → page.
- **Lookup Tables Reference** — the full `areas`, `call_purposes`, and room tables.
- **Troubleshooting** — reading `481`, duplicate INVITEs, and `fusion_status = -1` in the logs.

> **Roadmap note:** Terms marked **(roadmap)** / **(planned)** — Outbox, Dedupe, Watchdog/heartbeat/`/health`, WAL, background token refresh, RFC3339-Z timestamps, and HA — describe the in-progress v1.6.0 reliability release and future high-availability design. They are documented here for orientation only and do **not** reflect current v1.5.1 production behavior.
