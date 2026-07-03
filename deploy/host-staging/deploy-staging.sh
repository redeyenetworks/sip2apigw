#!/usr/bin/env bash
# Deploy sipgw v1.6.0 to a SEPARATELY-NAMED staging unit on the cutover host to
# run the real-systemd drills that cannot run in a container.
#
# STAGING ONLY. This script never touches the live sipgw.service, ports 5060/8080,
# /opt/sipgw, /var/lib/sipgw (prod DB), or /var/log/sipgw. It is dry-run + alt
# ports + staging DB, so NO real page can ever be sent.
#
# Usage (on the cutover host, as a sudo-capable user):
#   sudo git clone -b release/v1.6.0 https://github.com/redeyenetworks/sip2apigw.git /opt/sipgw-staging
#   sudo bash /opt/sipgw-staging/deploy/host-staging/deploy-staging.sh
set -euo pipefail

STAGING_DIR="/opt/sipgw-staging"
STAGING_DB_DIR="/var/lib/sipgw-staging"
STAGING_LOG_DIR="/var/log/sipgw-staging"
SVC_USER="sipgw"
SIP_PORT=5062
DASH_PORT=8082

# ---- hard safety guards: refuse anything that could touch prod ---------------
[[ $EUID -eq 0 ]] || { echo "run with sudo/root (systemd + /var dirs)"; exit 1; }
[[ "$STAGING_DIR" != "/opt/sipgw" ]] || { echo "REFUSING: staging dir == prod"; exit 1; }
[[ "$SIP_PORT" != "5060" && "$DASH_PORT" != "8080" ]] || { echo "REFUSING: prod ports"; exit 1; }
[[ -d "$STAGING_DIR/sipgw" ]] || { echo "clone release/v1.6.0 to $STAGING_DIR first"; exit 1; }
id "$SVC_USER" >/dev/null 2>&1 || { echo "user $SVC_USER not found"; exit 1; }

echo "=== sipgw v1.6.0 host-staging deploy (dry-run, SIP :$SIP_PORT, dash :$DASH_PORT) ==="

# ---- venv + deps -------------------------------------------------------------
python3 -m venv "$STAGING_DIR/venv"
"$STAGING_DIR/venv/bin/pip" install --quiet --upgrade pip
"$STAGING_DIR/venv/bin/pip" install --quiet -r "$STAGING_DIR/requirements.txt"

# ---- staging dirs ------------------------------------------------------------
mkdir -p "$STAGING_DB_DIR" "$STAGING_LOG_DIR"

# ---- staging config (dry-run; alt ports; staging paths; loopback allowed) ----
cat > "$STAGING_DIR/config.yaml" <<YAML
sip:
  bind_ip: "127.0.0.1"
  bind_port: ${SIP_PORT}
  allowed_networks: ["127.0.0.0/8", "172.16.0.0/12"]
  # Mirror production (config.yaml.example): immediate_bye is ON so the M7 drill
  # exercises the #11 ACK-gated deferred-BYE teardown on real systemd. A short
  # ACK fallback keeps the lost-ACK case fast in the drill.
  immediate_bye: true
  immediate_bye_ack_timeout_seconds: 2.0
fusion:
  base_url: "https://api.icmobile.singlewire.com/api"
  token_url: "https://api.icmobile.singlewire.com/api/token"
  dry_run: true
  scenario_field_id: "staging-preset"
escalation:
  webhook_url: ""
health:
  heartbeat_interval_seconds: 10.0
  stale_after_seconds: 30.0
dedupe:
  enforce: false
  window_seconds: 0
logging:
  log_dir: "${STAGING_LOG_DIR}"
  timezone: ""
dashboard:
  bind_ip: "127.0.0.1"
  port: ${DASH_PORT}
database:
  path: "${STAGING_DB_DIR}/calls.db"
YAML

# lookups: reuse the host's real mappings if present (read-only copy), else example
if [[ -f /opt/sipgw/lookups.yaml ]]; then
  cp /opt/sipgw/lookups.yaml "$STAGING_DIR/lookups.yaml"
else
  cp "$STAGING_DIR/lookups.yaml.example" "$STAGING_DIR/lookups.yaml"
fi

chown -R "$SVC_USER:$SVC_USER" "$STAGING_DIR" "$STAGING_DB_DIR" "$STAGING_LOG_DIR"

# ---- writer unit: Type=notify + watchdog (the #8 drill target) ---------------
cat > /etc/systemd/system/sipgw-staging.service <<UNIT
[Unit]
Description=sipgw STAGING (v1.6.0 cutover-host drills) - writer
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=notify
NotifyAccess=main
WatchdogSec=30
User=${SVC_USER}
Group=${SVC_USER}
WorkingDirectory=${STAGING_DIR}
ExecStart=${STAGING_DIR}/venv/bin/python -m sipgw.main ${STAGING_DIR}/config.yaml
Restart=always
RestartSec=5
Environment=SIPGW_CONFIG=${STAGING_DIR}/config.yaml
Environment=SIPGW_LOOKUPS=${STAGING_DIR}/lookups.yaml
Environment=SIPGW_DRY_RUN=1
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=${STAGING_LOG_DIR} ${STAGING_DB_DIR}
ProtectHome=true
PrivateTmp=true
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
UNIT

# ---- dashboard unit: read-only + MemoryMax/CPUQuota (the #14 drill target) ----
cat > /etc/systemd/system/sipgw-dashboard-staging.service <<UNIT
[Unit]
Description=sipgw STAGING - dashboard (read-only)
After=network-online.target sipgw-staging.service
Wants=network-online.target

[Service]
Type=simple
User=${SVC_USER}
Group=${SVC_USER}
WorkingDirectory=${STAGING_DIR}
ExecStart=${STAGING_DIR}/venv/bin/python -m sipgw.dashboard_app ${STAGING_DIR}/config.yaml
Restart=always
RestartSec=3
Environment=SIPGW_CONFIG=${STAGING_DIR}/config.yaml
Environment=SIPGW_LOOKUPS=${STAGING_DIR}/lookups.yaml
Environment=SIPGW_DRY_RUN=1
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=${STAGING_DB_DIR} ${STAGING_LOG_DIR}
ProtectHome=true
PrivateTmp=true
MemoryMax=256M
CPUQuota=50%
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable sipgw-staging.service sipgw-dashboard-staging.service >/dev/null 2>&1 || true
systemctl start sipgw-staging.service
sleep 3
systemctl start sipgw-dashboard-staging.service
sleep 2

echo ""
echo "=== deployed. quick status: ==="
systemctl is-active sipgw-staging.service          && echo "  writer active"
systemctl is-active sipgw-dashboard-staging.service && echo "  dashboard active"
echo "  /health: $(curl -s --max-time 3 http://127.0.0.1:${DASH_PORT}/health || echo UNREACHABLE)"
echo ""
echo "Next: sudo bash ${STAGING_DIR}/deploy/host-staging/drills.sh"
