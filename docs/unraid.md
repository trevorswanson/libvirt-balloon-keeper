# Unraid installation and persistence

Unraid loads its operating system into RAM. Do **not** install this controller
only under `/usr/local`, `/etc`, `/opt`, or `/var`: those are gone after a
reboot. The generic systemd files in this repository are for conventional
systemd hosts; this Unraid path uses the persistent boot device and
`/boot/config/go`.

## Layout

Use three locations with different jobs:

| Location | Contents | Why |
|---|---|---|
| `/boot/config/custom/libvirt-balloon-keeper/` | `balloon_keeper.py`, `run-libvirt-balloon-keeper-cron.sh`, `install-cron.sh`, local `config.toml` | Persisted on the Unraid boot device and available during boot. Files are run through `python3`/`bash`; current Unraid boot media does not support executable bits. |
| `/mnt/<cache-pool>/appdata/libvirt-balloon-keeper/` | `state.json`, `decisions.jsonl` | Persistent write-heavy state on a real cache pool, not on the USB boot device. Choose the actual cache-pool mount, not `/mnt/user`, so a mover operation cannot relocate an active state file. |
| `/boot/config/go` | one cron installer line | Persisted startup hook. It idempotently recreates the cron entry after every reboot. |

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

The installer adds a marked, once-per-minute root crontab entry. Add one line to
the existing `/boot/config/go`:

```bash
/usr/bin/bash /boot/config/custom/libvirt-balloon-keeper/install-cron.sh
```

The installer removes and recreates only its own marked block, so running it
repeatedly is safe and cannot create duplicate entries. Invoke the wrapper
manually for an immediate tick without starting a daemon:

```bash
/usr/bin/bash /boot/config/custom/libvirt-balloon-keeper/run-libvirt-balloon-keeper-cron.sh
crontab -l | grep -A2 -B1 libvirt-balloon-keeper
```

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
3. Replace `balloon_keeper.py`, `unraid/run-libvirt-balloon-keeper-cron.sh`, and `unraid/install-cron.sh` in the boot configuration directory from a reviewed release.
4. Run `python3 -m py_compile` and a one-shot tick.
5. Re-run the installer; keep the existing local `config.toml`, state, and audit log.

Never put the state or decision log on `/boot`: frequent writes are needless
USB wear and make diagnostics hostage to a little flash drive. Grim way to lose
an autoscaler.

The plugin lifecycle also owns a loopback-only API daemon on
`127.0.0.1:8765`. `start`/`restart` create it through `run-api.sh`, using
`/var/run/libvirt-balloon-keeper-api.pid` and a bounded log file; `stop` and
`uninstall` terminate only the PID recorded there. The dedicated
`libvirt-balloon-keeper.page` appears under User Utilities, reads
status/configuration, and validates before saving through this API. The page never executes shell commands or writes the
TOML file directly. Browser JavaScript uses the same-origin
`/plugins/libvirt-balloon-keeper/api.php` bridge, which allowlists the API
routes and forwards them server-side to loopback; browser `127.0.0.1` would
refer to the operator’s workstation, not the Unraid host.

The API runner defaults to port `8765`; `API_PORT` is an environment override
for isolated tests only. It still always binds the daemon to `127.0.0.1`.

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
`install`, `upgrade`, `start`, `stop`, `restart`, `rollback`, `check`, `migrate`,
and `uninstall`.
`install`/`upgrade` copy the controller and package modules, migrate an existing
legacy single-domain config once when the plugin config is absent, preserve its
pool-backed state/audit paths, validate prerequisites, and install the marked cron block.
The explicit `migrate` action performs only that non-destructive config migration.
`stop` removes only that block. `rollback` restores the previous managed
generation and preserves configuration/state history. `uninstall` stops
scheduling but intentionally preserves configuration and pool-backed state for
rollback or migration review.

The plugin path is `/boot/config/plugins/libvirt-balloon-keeper`; the current
live compatibility path under `/boot/config/custom` must not be overwritten by
this preview. Before deployment, review the rendered paths and set `POOL_ROOT`
to the actual physical cache-pool mount. Then perform, in order:

1. back up the Unraid boot device;
2. install with `dry_run = true` and run `check`;
3. run one manual tick and inspect the audit record;
4. run `start` twice and verify exactly one cron block;
5. verify the Tools → Libvirt Balloon Keeper page and its same-origin
   status/configuration bridge; confirm the daemon binds only to loopback;
6. reboot during the planned Unraid update and verify boot recovery;
7. only then consider a separately approved live-mode canary.

Do not execute `unraid/lifecycle.sh` against a production host until that
review is complete; this repository change stops at the deployment-ready gate.
