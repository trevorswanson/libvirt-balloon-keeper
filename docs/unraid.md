# Unraid installation and persistence

Unraid loads its operating system into RAM. Do **not** install this controller
only under `/usr/local`, `/etc`, `/opt`, or `/var`: those are gone after a
reboot. The generic systemd files in this repository are for conventional
systemd hosts; this Unraid path uses the persistent boot device and
`/boot/config/go`.

## Storage

The controller stores only its durable runtime state in the selected storage
root: per-VM `state.json` files (sample history, cooldowns, and cumulative
swap counters), per-VM `decisions.jsonl` audit logs, and short-lived lock files.
The WebGUI prefers `/mnt/cache` only when it is an actual mounted directory;
otherwise it selects another mounted pool, then `/mnt/user` as a persistent
share fallback. It never creates a guessed `/mnt/cache` tree. If no persistent
mount is available, newly discovered VMs cannot be saved until storage exists.

An explicit `pool_root` must be a direct child of `/mnt` and an existing mount;
the configuration validator rejects ordinary directories and paths elsewhere.

## Layout

Use three locations with different jobs:

| Location | Contents | Why |
|---|---|---|
| `/boot/config/plugins/libvirt-balloon-keeper/` | Managed code, `config.toml`, scheduler fragment, and API runtime files | Persisted on the Unraid boot device and installed by `lifecycle.sh`. |
| `/mnt/<mounted-pool>/appdata/libvirt-balloon-keeper/` | Per-VM `state.json`, `decisions.jsonl`, and lock files | Persistent write-heavy state on a verified mounted pool (or `/mnt/user` fallback), never a guessed directory. |
| `/boot/config/go` | host startup integration | Managed by Unraid's plugin lifecycle; do not hand-edit generated cron state. |

The cron wrapper runs one controller tick and exits. If the array/pool or VM is
not ready, the tick fails closed and cron retries on the next minute; `go` never
blocks on a long-running process.

## Pre-flight checks

Perform these on the Unraid host before installation:

```bash
/usr/bin/python3 --version                 # Python 3.11+ required
command -v virsh flock logger
virsh dommemstat YOUR_DOMAIN --period 10 --live
virsh dommemstat YOUR_DOMAIN               # last_update must advance
```

Back up the boot device in the Unraid WebGUI before changing `config/go`:
**Main → Boot device → Boot Device Backup**.

## Install, dry-run first

From a checked-out release directory on the host (or after copying reviewed
release files to a temporary location):

```bash
install -d -m 0700 /boot/config/custom/libvirt-balloon-keeper
install -d -m 0700 /mnt/CACHE_POOL/appdata/libvirt-balloon-keeper
install -m 0644 balloon_keeper.py /boot/config/custom/libvirt-balloon-keeper/
install -m 0644 unraid/run-libvirt-balloon-keeper-cron.sh /boot/config/custom/libvirt-balloon-keeper/
install -m 0644 unraid/install-cron.sh /boot/config/custom/libvirt-balloon-keeper/
install -m 0640 config.example.toml /boot/config/custom/libvirt-balloon-keeper/config.toml
```

Edit only the local `config.toml`. Set the real VM `domain`, retain
`dry_run = true`, and set these paths to the physical cache pool selected above:

```toml
[runtime]
state_file = "/mnt/CACHE_POOL/appdata/libvirt-balloon-keeper/state.json"
decision_log = "/mnt/CACHE_POOL/appdata/libvirt-balloon-keeper/decisions.jsonl"
```

Validate a one-shot dry tick before adding startup automation:

```bash
/usr/bin/python3 /boot/config/custom/libvirt-balloon-keeper/balloon_keeper.py \
  --config /boot/config/custom/libvirt-balloon-keeper/config.toml
```

Check the decision log and confirm that `virsh dominfo YOUR_DOMAIN` reports an
unchanged target.

## Persistent startup hook

The managed plugin lifecycle uses Unraid's native
`/boot/config/plugins/libvirt-balloon-keeper/libvirt-balloon-keeper.cron`
fragment and invokes `update_cron`; it does not splice the root crontab.

The wrapper has a non-blocking lock, invokes exactly one controller decision,
and exits. Cron supplies the retry boundary after failures or process death.

## Enable actual changes

Keep `dry_run = true` through at least one boot-cycle validation. Only then set
`dry_run = false` in the **local boot-device configuration**. Restart the
runner or reboot; do not use the systemd templates on Unraid.

## Stop / rollback

```bash
pkill -f run-libvirt-balloon-keeper
```

Remove only the controller’s one bootstrap line from `/boot/config/go`. The
current balloon target remains in force until changed by libvirt or an operator.
The code, configuration, and JSONL audit trail remain available for inspection.

For the managed plugin lifecycle, use its recoverable rollback snapshot instead
of killing processes by pattern:

```bash
/usr/bin/bash /boot/config/plugins/libvirt-balloon-keeper/lifecycle.sh stop
/usr/bin/bash /boot/config/plugins/libvirt-balloon-keeper/lifecycle.sh rollback
```

Each install or upgrade snapshots the previous managed controller, page, and
configuration under the plugin-owned `.rollback` directory before replacing
files. `rollback` restores that snapshot, validates it, recreates the one
managed cron block, and starts the loopback API. It never deletes pool-backed
state or audit logs. Only the most recent managed generation is retained;
create a boot-device backup before upgrades when longer rollback history is
needed.

## Upgrades

1. Stop the runner.
2. Back up the boot device.
3. Replace the managed plugin from a reviewed release, or replace
   `balloon_keeper.py`, `unraid/run-libvirt-balloon-keeper-cron.sh`, and
   `unraid/install-cron.sh` when using the legacy compatibility path.
4. Run `python3 -m py_compile` and a one-shot tick.
5. Re-run the installer; keep the existing local `config.toml`, state, and audit log.

Never put the state or decision log on `/boot`: frequent writes are needless
USB wear and make diagnostics hostage to a little flash drive. Grim way to lose
an autoscaler.

The plugin lifecycle also owns an API daemon on the mode-restricted Unix socket
`/var/run/libvirt-balloon-keeper-api.sock`. `start`/`restart` create it through `run-api.sh`, using
`/var/run/libvirt-balloon-keeper-api.pid` and a bounded log file; `stop` and
`uninstall` terminate only the PID recorded there and remove the socket. The dedicated
`libvirt-balloon-keeper.page` appears under User Utilities, reads
status/configuration, and validates before saving through this API. The page never executes shell commands or writes the
TOML file directly. Browser JavaScript uses the same-origin
`/plugins/libvirt-balloon-keeper/api.php` bridge, which allowlists the API
routes and forwards them server-side through the Unix socket.

The API runner defaults to `/var/run/libvirt-balloon-keeper-api.sock`; `API_SOCKET`
is an environment override for isolated tests and temporary installations. The
socket is created with mode `0600` and removed during a managed stop.

The controller tick invokes the layered scheduler with the explicit Unraid
notifier path. Notifications use argv-only execution, cap titles/details, and
are per-VM deduplicated and rate-limited. Normal holds—including lock
overlap—do not notify; stale health, failed mutations, and runtime errors do.

## Structured WebGUI

The User Utilities page is a structured editor backed by the loopback API bridge. `GET
/api/inventory` discovers all libvirt domains and merges them with configured VMs;
newly discovered domains are review-only until explicitly added. Each entry may include
virtio-balloon capability, current/available/usable memory, swap counters, the latest
swap delta, last decision, and bounded error state. The editor exposes per-VM enable,
dry-run, interval, memory bounds, step, pressure thresholds, and sample counts.

The browser submits JSON to `/api/validate-configuration` and
`/api/configuration`; the server converts it to validated versioned TOML and performs
an atomic replacement only with `X-Confirm: apply`. Legacy TOML `/api/config` and
`/api/validate` routes remain available for compatibility. Enabling live mode in the
page requires a confirmation naming every affected VM; the deployed configuration
remains `dry_run = true` until deliberately changed.

## Plugin lifecycle preview

The reviewed plugin lifecycle entrypoint is `unraid/lifecycle.sh`. It supports
`install`, `upgrade`, `start`, `stop`, `restart`, `rollback`, `check`, and
`uninstall`.
`install`/`upgrade` copy the controller and package modules, preserve existing
pool-backed state/audit paths, validate prerequisites, and install the marked cron block.
`stop` removes only that block. `rollback` restores the previous managed
generation and preserves configuration/state history. `uninstall` stops
scheduling but intentionally preserves configuration and pool-backed state for
rollback or migration review.

The plugin path is `/boot/config/plugins/libvirt-balloon-keeper`; the current
live compatibility path under `/boot/config/custom` must not be overwritten by
this preview. State and audit paths come from the configuration; the lifecycle
does not assume or create a particular cache-pool mount. Then perform, in order:

1. back up the Unraid boot device;
2. install with `dry_run = true` and run `check`;
3. run one manual tick and inspect the audit record;
4. run `start` twice and verify exactly one cron block;
5. verify the Settings → User Utilities → Libvirt Balloon Keeper page and its same-origin
   status/configuration bridge; confirm the daemon uses the mode-restricted socket;
6. reboot during the planned Unraid update and verify boot recovery;
7. only then consider a separately approved live-mode canary.

Do not execute `unraid/lifecycle.sh` against a production host until that
review is complete; this repository change stops at the deployment-ready gate.
