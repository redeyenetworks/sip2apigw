#!/usr/bin/env bash
# Remove the sipgw v1.6.0 host-staging footprint. Touches ONLY the staging units,
# staging dirs, and /opt/sipgw-staging — never the live sipgw.service or prod data.
set -uo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo/root"; exit 1; }

systemctl stop    sipgw-staging.service sipgw-dashboard-staging.service 2>/dev/null || true
systemctl disable sipgw-staging.service sipgw-dashboard-staging.service 2>/dev/null || true
rm -f /etc/systemd/system/sipgw-staging.service /etc/systemd/system/sipgw-dashboard-staging.service
systemctl daemon-reload
rm -rf /var/lib/sipgw-staging /var/log/sipgw-staging /opt/sipgw-staging
echo "host-staging removed. Live sipgw.service and prod data untouched."
