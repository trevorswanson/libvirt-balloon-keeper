#!/usr/bin/env bash
set -euo pipefail
ROOT="${PLUGIN_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)}"
CONFIG="${ROOT}/config.toml"
PID_FILE="${API_PID_FILE:-/var/run/libvirt-balloon-keeper-api.pid}"
SOCKET="${API_SOCKET:-/var/run/libvirt-balloon-keeper-api.sock}"
LOG_FILE="${API_LOG_FILE:-/var/log/libvirt-balloon-keeper-api.log}"
[[ "$SOCKET" = /* ]] || { logger -t libvirt-balloon-keeper "invalid API socket"; exit 64; }
[[ -f "$CONFIG" ]] || { logger -t libvirt-balloon-keeper "API config missing: $CONFIG"; exit 1; }
if [[ -f "$PID_FILE" ]]; then
    pid=$(cat "$PID_FILE")
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then exit 0; fi
    rm -f "$PID_FILE"
fi
install -d -m 0750 "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")"
install -d -m 0750 "$(dirname "$SOCKET")"
nohup /usr/bin/python3 "$ROOT/web_server.py" --config "$CONFIG" --socket "$SOCKET" >>"$LOG_FILE" 2>&1 &
printf '%s\n' "$!" >"$PID_FILE"
chmod 0640 "$PID_FILE"
