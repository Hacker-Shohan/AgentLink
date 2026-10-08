#!/usr/bin/env bash
# AI Agent PC Harness — Linux bootstrap
# Invoked as: curl -fsSL "https://<vps>/bootstrap.sh?token=...&device_id=..." | bash
# {{SERVER_BASE}} and {{TOKEN}} are substituted server-side before this is served.
set -euo pipefail

HARNESS_SERVER_BASE="{{SERVER_BASE}}"
HARNESS_TOKEN="{{TOKEN}}"

INSTALL_DIR="$HOME/.local/share/agent-harness"
mkdir -p "$INSTALL_DIR"

if ! command -v python3 >/dev/null; then
  echo "Python 3 is required. Install it with your package manager and re-run this command." >&2
  exit 1
fi

python3 -m pip install --quiet --user websocket-client

curl -fsSLk "$HARNESS_SERVER_BASE/client/harness_client.py" -o "$INSTALL_DIR/harness_client.py"

WS_SERVER="${HARNESS_SERVER_BASE/https:/wss:}"
WS_SERVER="${WS_SERVER/http:/ws:}"

nohup python3 "$INSTALL_DIR/harness_client.py" --server "$WS_SERVER" --token "$HARNESS_TOKEN" --insecure --reset \
  > "$INSTALL_DIR/client.log" 2>&1 &

echo "Harness client started (PID $!). Logs: $INSTALL_DIR/client.log"
