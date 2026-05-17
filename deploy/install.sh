#!/bin/bash
# Deploy vrp-agent as systemd service on Linux.
# Run AS root or with sudo. Expects repo cloned at /opt/vrp-agent and .env present.
#
# Override AGENT_USER env var to run as a different OS user (default: vrpagent).

set -euo pipefail

TARGET="${TARGET:-/opt/vrp-agent}"
SERVICE=vrp-agent
AGENT_USER="${AGENT_USER:-vrpagent}"

if [ ! -d "$TARGET" ]; then
    echo "ERROR: $TARGET does not exist. Clone repo there first."
    exit 1
fi

if [ ! -f "$TARGET/.env" ]; then
    echo "ERROR: $TARGET/.env missing. Copy from local."
    exit 1
fi

cd "$TARGET"

echo "─── Creating venv ───"
python3 -m venv .venv || true
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

mkdir -p data logs
chmod 600 .env

echo "─── Installing systemd unit ───"
cp deploy/$SERVICE.service /etc/systemd/system/$SERVICE.service
systemctl daemon-reload
systemctl enable $SERVICE

echo "─── Pre-flight check ───"
sudo -u "$AGENT_USER" $TARGET/.venv/bin/python -m src.check_wallet

echo ""
echo "─── Ready. To start agent: ───"
echo "  systemctl start $SERVICE"
echo "  journalctl -u $SERVICE -f"
