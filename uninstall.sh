#!/usr/bin/env bash
# uninstall.sh — Remove sipgw service
# Run as root: sudo bash uninstall.sh
set -euo pipefail

SERVICE_NAME="sipgw"
INSTALL_DIR="/opt/sipgw"
LOG_DIR="/var/log/sipgw"
DATA_DIR="/var/lib/sipgw"
SERVICE_USER="sipgw"

echo "=== sipgw Uninstaller ==="

# --- Check root ---
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root (sudo)."
    exit 1
fi

# --- Stop and disable service ---
echo "[1/4] Stopping and disabling service..."
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl stop "$SERVICE_NAME"
    echo "  Service stopped."
fi
if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl disable "$SERVICE_NAME"
    echo "  Service disabled."
fi
if [[ -f "/etc/systemd/system/${SERVICE_NAME}.service" ]]; then
    rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
    systemctl daemon-reload
    echo "  Unit file removed."
fi

# --- Remove virtual environment ---
echo "[2/4] Removing virtual environment..."
if [[ -d "$INSTALL_DIR/venv" ]]; then
    rm -rf "$INSTALL_DIR/venv"
    echo "  Virtual environment removed."
else
    echo "  No virtual environment found."
fi

# --- Ask about data removal ---
echo "[3/4] Data directories:"
echo "  Logs: $LOG_DIR"
echo "  Database: $DATA_DIR"
read -rp "  Remove log and data directories? (y/N): " remove_data
if [[ "${remove_data,,}" == "y" ]]; then
    rm -rf "$LOG_DIR" "$DATA_DIR"
    echo "  Data directories removed."
else
    echo "  Data directories preserved."
fi

# --- Ask about user removal ---
echo "[4/4] Service user: $SERVICE_USER"
if id "$SERVICE_USER" &>/dev/null; then
    read -rp "  Remove service user '$SERVICE_USER'? (y/N): " remove_user
    if [[ "${remove_user,,}" == "y" ]]; then
        userdel "$SERVICE_USER" 2>/dev/null || true
        echo "  User removed."
    else
        echo "  User preserved."
    fi
fi

echo ""
echo "=== Uninstall complete ==="
echo ""
echo "Note: Application files in $INSTALL_DIR were NOT removed."
echo "To fully remove: rm -rf $INSTALL_DIR"
echo ""
