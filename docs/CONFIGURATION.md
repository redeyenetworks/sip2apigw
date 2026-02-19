# Configuration Reference

All configuration is in `/opt/sipgw/config.yaml`. Lookup tables are in `/opt/sipgw/lookups.yaml`.

## config.yaml

### SIP Section

```yaml
sip:
  bind_ip: "0.0.0.0"              # IP to bind SIP listener (0.0.0.0 = all interfaces)
  bind_port: 5060                  # SIP port (standard = 5060)
  allowed_networks:                # Source IP filter (CIDR notation)
    - "172.16.0.0/12"
  call_timeout_seconds: 600        # Max call duration before auto-hangup (10 min)
  rtp_port_range_start: 10000      # Start of RTP port range (even numbers)
  rtp_port_range_end: 20000        # End of RTP port range
```

### Fusion Section

```yaml
fusion:
  base_url: "https://admin.icmobile.singlewire.com"
  token_url: "https://admin.icmobile.singlewire.com/api/oauth/token"
  scenario_id: "YOUR_SCENARIO_ID"
  scenario_endpoint: "/api/scenarios/{scenario_id}/launch"  # {scenario_id} is replaced
  variable_name: "customTTS"       # JSON key for the TTS text
  client_id: "YOUR_CLIENT_ID"
  client_secret: "CHANGE_ME"       # <-- SET THIS
```

### Logging Section

```yaml
logging:
  log_dir: "/var/log/sipgw"        # Log file directory
  retention_days: 90               # Days to keep rotated logs
  rotation_time: "midnight"        # When to rotate (midnight ET)
  timezone: "America/New_York"     # Timezone for rotation schedule
```

### Dashboard Section

```yaml
dashboard:
  port: 8080                       # HTTP port for web dashboard
  bind_ip: "0.0.0.0"              # Dashboard bind IP
  auto_refresh_seconds: 10         # Page auto-refresh interval
```

### Database Section

```yaml
database:
  path: "/var/lib/sipgw/calls.db"  # SQLite database file path
```

## lookups.yaml

### Area Mappings

Maps numeric area IDs from the SIP username to speech-ready location names. Uses phonetic spelling for TTS clarity.

```yaml
areas:
  710: "3rd Floor. Cardiac Step-Down."
  730: "1st Floor. E.D."
  # Add new areas here...

default_area: "Unknown Area."
```

### Call Purpose Substitutions

Maps keywords found in the SIP display name to spoken announcements.

```yaml
call_purposes:
  "Blue": "Code Blue"
  "RRT": "Rapid Response Team"
  "Pink": "Code Pink"

default_purpose: "Code"
```

**Order matters**: First matching keyword wins. If the display name contains multiple keywords (unlikely), the first match in YAML order is used.

## Environment Variables

| Variable         | Default                    | Purpose                     |
|------------------|----------------------------|-----------------------------|
| `SIPGW_CONFIG`   | `/opt/sipgw/config.yaml`   | Override config file path   |
| `SIPGW_LOOKUPS`  | `/opt/sipgw/lookups.yaml`  | Override lookups file path  |

## Applying Changes

- **config.yaml changes**: Restart the service (`systemctl restart sipgw`)
- **lookups.yaml changes**: Restart the service (`systemctl restart sipgw`)
- No code changes needed for table updates
