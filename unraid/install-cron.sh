#!/usr/bin/env bash
set -euo pipefail
ROOT="${PLUGIN_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)}"
WRAPPER="${ROOT}/run-once.sh"
TMP="$(mktemp /tmp/libvirt-balloon-keeper-cron.XXXXXX)"
trap 'rm -f "$TMP"' EXIT
/usr/bin/python3 -c 'import sys
b="# BEGIN libvirt-balloon-keeper"; e="# END libvirt-balloon-keeper"; skip=False
for line in sys.stdin:
    if line.rstrip("\n")==b: skip=True; continue
    if line.rstrip("\n")==e: skip=False; continue
    if not skip: sys.stdout.write(line)' < <(crontab -l 2>/dev/null || true) >"$TMP"
printf '%s\n' '# BEGIN libvirt-balloon-keeper' "* * * * * /usr/bin/bash $WRAPPER" '# END libvirt-balloon-keeper' >>"$TMP"
crontab "$TMP"
logger -t libvirt-balloon-keeper 'installed one-shot cron schedule'
