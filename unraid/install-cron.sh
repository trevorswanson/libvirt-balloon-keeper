#!/usr/bin/env bash
set -euo pipefail
# Unraid assembles root's crontab from /boot/config/plugins/<plugin>/*.cron
# fragments via update_cron, so the schedule is written as a fragment in the
# plugin's own directory instead of splicing the root crontab directly.
ROOT="${PLUGIN_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)}"
WRAPPER="${ROOT}/run-once.sh"
FRAGMENT="${ROOT}/libvirt-balloon-keeper.cron"
UPDATE_CRON="${UPDATE_CRON:-/usr/local/sbin/update_cron}"
RECONCILE_SCRIPT="/tmp/libvirt-balloon-keeper-update-cron"
printf '%s\n' "* * * * * /usr/bin/bash $WRAPPER" >"$FRAGMENT"
"$UPDATE_CRON"
# Unraid registers the PLG in /var/log/plugins after its FILE actions run.
# Defer one reconciliation so update_cron can discover the new registry entry.
cat >"$RECONCILE_SCRIPT" <<EOF
#!/usr/bin/env bash
set -eu
"$UPDATE_CRON"
rm -f -- "$RECONCILE_SCRIPT"
EOF
chmod 0700 "$RECONCILE_SCRIPT"
if command -v at >/dev/null 2>&1; then
    at -M -f "$RECONCILE_SCRIPT" now + 1 >/dev/null 2>&1 || true
else
    (sleep 2; "$RECONCILE_SCRIPT") >/dev/null 2>&1 &
fi
logger -t libvirt-balloon-keeper 'installed one-shot cron schedule'
