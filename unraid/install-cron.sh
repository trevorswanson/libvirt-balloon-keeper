#!/usr/bin/env bash
set -euo pipefail
# Unraid assembles root's crontab from /boot/config/plugins/<plugin>/*.cron
# fragments via update_cron, so the schedule is written as a fragment in the
# plugin's own directory instead of splicing the root crontab directly.
ROOT="${PLUGIN_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)}"
WRAPPER="${ROOT}/run-once.sh"
FRAGMENT="${ROOT}/libvirt-balloon-keeper.cron"
UPDATE_CRON="${UPDATE_CRON:-/usr/local/sbin/update_cron}"
printf '%s\n' "* * * * * /usr/bin/bash $WRAPPER" >"$FRAGMENT"
"$UPDATE_CRON"
logger -t libvirt-balloon-keeper 'installed one-shot cron schedule'
