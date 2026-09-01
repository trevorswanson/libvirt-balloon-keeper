#!/usr/bin/env bash
# Unraid bootstrap runner. Invoke from /boot/config/go with bash; files on the
# boot device cannot be marked executable on current Unraid releases.
set -u

ROOT="${LBK_ROOT:-/boot/config/custom/libvirt-balloon-keeper}"
CONFIG="${LBK_CONFIG:-${ROOT}/config.toml}"
INTERVAL_SECONDS="${LBK_INTERVAL_SECONDS:-30}"
LOCK_FILE="/var/run/libvirt-balloon-keeper-runner.lock"

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    logger -t libvirt-balloon-keeper 'runner already active; exiting duplicate bootstrap'
    exit 0
fi

# /boot/config/go runs before the array/pools and auto-start VMs. Read the
# locally configured state directory, then wait for its cache-pool mount.
until [[ -f "${CONFIG}" ]]; do
    sleep 5
done
PERSISTENT_DIR="${LBK_PERSISTENT_DIR:-$(/usr/bin/python3 -c 'import sys, tomllib; from pathlib import Path; print(Path(tomllib.load(open(sys.argv[1], "rb"))["runtime"]["state_file"]).parent)' "${CONFIG}")}"
until [[ -d "${PERSISTENT_DIR}" ]]; do
    sleep 5
done

while true; do
    if ! /usr/bin/python3 "${ROOT}/balloon_keeper.py" --config "${CONFIG}"; then
        # A stopped/not-yet-defined domain is non-destructive.  Keep the runner
        # alive for the next interval and leave detailed errors in syslog.
        logger -t libvirt-balloon-keeper 'controller tick failed; will retry'
    fi
    sleep "${INTERVAL_SECONDS}"
done
