#!/usr/bin/env bash
# Run exactly one libvirt-balloon-keeper decision tick from Unraid cron.
set -u
ROOT="${LBK_ROOT:-/boot/config/custom/libvirt-balloon-keeper}"
CONFIG="${LBK_CONFIG:-${ROOT}/config.toml}"
LOCK_FILE=/var/run/libvirt-balloon-keeper-tick.lock

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    logger -t libvirt-balloon-keeper "tick already active; skipping overlap"
    exit 0
fi

if [[ ! -f "${CONFIG}" ]]; then
    logger -t libvirt-balloon-keeper "config missing: ${CONFIG}"
    exit 1
fi

if ! /usr/bin/python3 "${ROOT}/balloon_keeper.py" --config "${CONFIG}"; then
    logger -t libvirt-balloon-keeper "controller tick failed; cron will retry"
    exit 1
fi
