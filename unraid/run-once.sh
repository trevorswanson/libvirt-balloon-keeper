#!/usr/bin/env bash
set -u
ROOT="${PLUGIN_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)}"
CONFIG="${ROOT}/config.toml"
LOCK_FILE=/var/run/libvirt-balloon-keeper-plugin.lock
exec 9>"$LOCK_FILE"
if ! flock -n 9; then logger -t libvirt-balloon-keeper 'tick already active; skipping overlap'; exit 0; fi
[[ -f "$CONFIG" ]] || { logger -t libvirt-balloon-keeper "config missing: $CONFIG"; exit 1; }
if ! /usr/bin/python3 "$ROOT/balloon_keeper.py" --config "$CONFIG" --notify-command /usr/local/emhttp/webGui/scripts/notify; then
  logger -t libvirt-balloon-keeper 'controller tick failed; cron will retry'; exit 1
fi
