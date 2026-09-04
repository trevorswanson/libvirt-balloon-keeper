#!/usr/bin/env bash
# Managed Unraid plugin lifecycle. Run as root from the extracted release tree.
set -euo pipefail
ROOT="${PLUGIN_ROOT:-/boot/config/plugins/libvirt-balloon-keeper}"
SOURCE="${PLUGIN_SOURCE:-$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)}"
CONFIG="${ROOT}/config.toml"
WRAPPER="${ROOT}/run-once.sh"
INSTALLER="${ROOT}/install-cron.sh"
CRON_FRAGMENT="${ROOT}/libvirt-balloon-keeper.cron"
UPDATE_CRON="${UPDATE_CRON:-/usr/local/sbin/update_cron}"
API_RUNNER="${ROOT}/run-api.sh"
API_PID_FILE="${API_PID_FILE:-/var/run/libvirt-balloon-keeper-api.pid}"
API_SOCKET="${API_SOCKET:-/var/run/libvirt-balloon-keeper-api.sock}"
[[ "$API_SOCKET" = /* ]] || { printf 'invalid API socket\n' >&2; exit 64; }
EMHTTP_ROOT="${EMHTTP_ROOT:-/usr/local/emhttp/plugins/libvirt-balloon-keeper}"
ROLLBACK_ROOT="${ROOT}/.rollback"

backup_current() {
    [[ -f "$CONFIG" ]] || return 0
    install -d -m 0750 "$ROLLBACK_ROOT"
    rm -rf "$ROLLBACK_ROOT/libvirt_balloon_keeper"
    cp -a "$ROOT/libvirt_balloon_keeper" "$ROLLBACK_ROOT/" 2>/dev/null || true
    for file in balloon_keeper.py web_server.py run-once.sh install-cron.sh run-api.sh config.toml; do
        [[ -e "$ROOT/$file" ]] && cp -a "$ROOT/$file" "$ROLLBACK_ROOT/$file"
    done
    for file in libvirt-balloon-keeper.page api.php libvirt-balloon-keeper.png; do
        if [[ -f "$EMHTTP_ROOT/$file" ]]; then
            cp -a "$EMHTTP_ROOT/$file" "$ROLLBACK_ROOT/"
        fi
    done
    chmod 0640 "$ROLLBACK_ROOT/config.toml" 2>/dev/null || true
}

rollback() {
    [[ -f "$ROLLBACK_ROOT/config.toml" && -f "$ROLLBACK_ROOT/libvirt-balloon-keeper.page" && -f "$ROLLBACK_ROOT/api.php" && -f "$ROLLBACK_ROOT/libvirt-balloon-keeper.png" ]] || { printf 'no rollback snapshot available\n' >&2; return 1; }
    bash "$0" stop
    install -d -m 0750 "$ROOT"
    rm -rf "$ROOT/libvirt_balloon_keeper"
    cp -a "$ROLLBACK_ROOT/libvirt_balloon_keeper" "$ROOT/"
    for file in balloon_keeper.py web_server.py run-once.sh install-cron.sh run-api.sh config.toml; do
        cp -a "$ROLLBACK_ROOT/$file" "$ROOT/$file"
    done
    install -d -m 0750 "$EMHTTP_ROOT"
    cp -a "$ROLLBACK_ROOT/libvirt-balloon-keeper.page" "$EMHTTP_ROOT/"
    cp -a "$ROLLBACK_ROOT/api.php" "$EMHTTP_ROOT/"
    cp -a "$ROLLBACK_ROOT/libvirt-balloon-keeper.png" "$EMHTTP_ROOT/"
    chmod 0640 "$CONFIG"
    check
    "$INSTALLER"
    start_api
}

install_files() {
    backup_current
    install -d -m 0750 "$ROOT"

    install -m 0644 "$SOURCE/balloon_keeper.py" "$ROOT/"
    install -m 0644 "$SOURCE/web_server.py" "$ROOT/"
    cp -R "$SOURCE/libvirt_balloon_keeper" "$ROOT/"
    chmod -R go-w "$ROOT/libvirt_balloon_keeper"
    install -m 0750 "$SOURCE/unraid/run-once.sh" "$WRAPPER"
    install -m 0750 "$SOURCE/unraid/install-cron.sh" "$INSTALLER"
    install -m 0750 "$SOURCE/unraid/run-api.sh" "$API_RUNNER"
    install -m 0750 "$SOURCE/unraid/lifecycle.sh" "$ROOT/lifecycle.sh"
    install -d -m 0750 "$EMHTTP_ROOT"
    install -m 0644 "$SOURCE/unraid/api.php" "$EMHTTP_ROOT/api.php"
    install -m 0644 "$SOURCE/unraid/libvirt-balloon-keeper.png" "$EMHTTP_ROOT/libvirt-balloon-keeper.png"
    install -m 0644 "$SOURCE/unraid/libvirt-balloon-keeper.page" "$EMHTTP_ROOT/"
    if [[ ! -e "$CONFIG" ]]; then install -m 0640 "$SOURCE/config.example.toml" "$CONFIG"; fi
}

check() {
    command -v /usr/bin/python3 >/dev/null
    command -v virsh >/dev/null
    command -v flock >/dev/null
    [[ -f "$CONFIG" ]]
    /usr/bin/python3 "$ROOT/balloon_keeper.py" --config "$CONFIG" --check-config
}

stop_api() {
    if [[ -f "$API_PID_FILE" ]]; then
        pid=$(cat "$API_PID_FILE")
        if [[ "$pid" =~ ^[0-9]+$ ]]; then
            kill "$pid" 2>/dev/null || true
            for _ in {1..50}; do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.02
            done
        fi
        rm -f "$API_PID_FILE"
    fi
}

start_api() {
    API_PID_FILE="$API_PID_FILE" API_SOCKET="$API_SOCKET" "$API_RUNNER"
}

case "${1:-}" in
    install|upgrade) install_files; check; UPDATE_CRON="$UPDATE_CRON" "$INSTALLER"; start_api ;;
    start) UPDATE_CRON="$UPDATE_CRON" "$INSTALLER"; start_api ;;
    restart) stop_api; UPDATE_CRON="$UPDATE_CRON" "$INSTALLER"; start_api ;;
    rollback) rollback ;;
    stop) stop_api; [[ ! -e "$API_SOCKET" || -S "$API_SOCKET" ]] && rm -f "$API_SOCKET"; rm -f "$CRON_FRAGMENT"; "$UPDATE_CRON" ;;
    check) check ;;
    uninstall) bash "$0" stop; logger -t libvirt-balloon-keeper "stopped; configuration and state preserved" ;;
    *) printf 'usage: %s {install|upgrade|start|stop|restart|rollback|check|uninstall}\n' "$0" >&2; exit 64 ;;
esac
