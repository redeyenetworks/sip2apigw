#!/usr/bin/env bash
# install.sh — Install and configure sipgw service
# Run as root: sudo bash install.sh
set -euo pipefail

INSTALL_DIR="/opt/sipgw"
LOG_DIR="/var/log/sipgw"
DATA_DIR="/var/lib/sipgw"
SERVICE_USER="sipgw"
SERVICE_NAME="sipgw"
PYTHON_MIN="3.11"

echo "=== sipgw Installer ==="

# --- Check root ---
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root (sudo)."
    exit 1
fi

# --- Check Python version ---
echo "[1/7] Checking Python version..."
PYTHON_BIN=""
for candidate in python3.12 python3.11 python3; do
    if command -v "$candidate" &>/dev/null; then
        ver=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [[ "$major" -ge 3 && "$minor" -ge 11 ]]; then
            PYTHON_BIN="$candidate"
            echo "  Found $candidate ($ver)"
            break
        fi
    fi
done

if [[ -z "$PYTHON_BIN" ]]; then
    echo "ERROR: Python >= $PYTHON_MIN required. Install with: apt install python3.11"
    exit 1
fi

# --- Install system dependencies ---
echo "[2/7] Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip > /dev/null

# --- Create service user ---
echo "[3/7] Creating service user '$SERVICE_USER'..."
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --shell /usr/sbin/nologin --home-dir "$INSTALL_DIR" "$SERVICE_USER"
    echo "  User '$SERVICE_USER' created."
else
    echo "  User '$SERVICE_USER' already exists."
fi

# --- Create directories ---
echo "[4/7] Creating directories..."
mkdir -p "$LOG_DIR" "$DATA_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$LOG_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
chmod 750 "$LOG_DIR" "$DATA_DIR"
echo "  $LOG_DIR (owner: $SERVICE_USER)"
echo "  $DATA_DIR (owner: $SERVICE_USER)"

# --- Set up Python virtual environment ---
echo "[5/7] Setting up Python virtual environment..."
if [[ ! -d "$INSTALL_DIR/venv" ]]; then
    "$PYTHON_BIN" -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
echo "  Virtual environment ready at $INSTALL_DIR/venv"

# --- Set ownership ---
echo "[6/7] Setting file ownership..."
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
# Keep install/uninstall scripts owned by root
chown root:root "$INSTALL_DIR/install.sh" "$INSTALL_DIR/uninstall.sh"
chmod 750 "$INSTALL_DIR/install.sh" "$INSTALL_DIR/uninstall.sh"

# Protect config file (contains secrets)
chmod 640 "$INSTALL_DIR/config.yaml"
chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/config.yaml"

# --- Install systemd unit ---
echo "[7/7] Installing systemd service..."
cp "$INSTALL_DIR/sipgw.service" /etc/systemd/system/sipgw.service
systemctl daemon-reload
systemctl enable sipgw.service
echo "  Service installed and enabled."

echo ""
echo "=== Installation complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit /opt/sipgw/config.yaml and set the Fusion client_secret"
echo "  2. Review /opt/sipgw/lookups.yaml for area mappings"
echo "  3. Start the service: systemctl start sipgw"
echo "  4. Check status: systemctl status sipgw"
echo "  5. View logs: journalctl -u sipgw -f"
echo "  6. Dashboard: http://$(hostname -I | awk '{print $1}'):8080"
echo ""
