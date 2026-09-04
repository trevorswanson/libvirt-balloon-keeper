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

# /boot/config/go runs before the array/pools and auto-start VMs. Read every
# configured state path and wait until each path's storage is available. The
# file and parent directory may not exist yet; use the nearest existing
# ancestor to detect an actual /mnt mount.
until [[ -f "${CONFIG}" ]]; do
    sleep 5
done
if [[ -n "${LBK_PERSISTENT_DIR:-}" ]]; then
    until [[ -d "${LBK_PERSISTENT_DIR}" ]]; do sleep 5; done
else
    until /usr/bin/python3 - "${CONFIG}" <<'PY'
import os
import sys
import tomllib
from pathlib import Path

try:
    data = tomllib.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    vms = data["vms"]
    if not isinstance(vms, list) or not vms:
        raise ValueError
    for raw in vms:
        if not isinstance(raw, dict):
            raise ValueError
        runtime = raw.get("runtime", {})
        if not isinstance(runtime, dict):
            raise ValueError
        identifier = raw.get("id", "")
        value = raw.get("state_file", runtime.get("state_file", f"/var/lib/libvirt-balloon-keeper/{identifier}/state.json"))
        path = Path(value)
        ancestor = path.parent
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        if not ancestor.exists():
            raise ValueError
        if path.is_relative_to(Path("/mnt")):
            current = ancestor
            while current != current.parent and current.is_relative_to(Path("/mnt")) and not os.path.ismount(current):
                current = current.parent
            if not os.path.ismount(current):
                raise ValueError
except (KeyError, OSError, TypeError, ValueError, tomllib.TOMLDecodeError):
    raise SystemExit(1)
PY
    do
        sleep 5
    done
fi

while true; do
    if ! /usr/bin/python3 "${ROOT}/balloon_keeper.py" --config "${CONFIG}"; then
        # A stopped/not-yet-defined domain is non-destructive.  Keep the runner
        # alive for the next interval and leave detailed errors in syslog.
        logger -t libvirt-balloon-keeper 'controller tick failed; will retry'
    fi
    sleep "${INTERVAL_SECONDS}"
done
