# libvirt-balloon-keeper

A conservative controller for **libvirt/QEMU virtio-balloon** memory. It turns the otherwise-manual `virsh setmem` target into a small, inspectable policy loop and now provides the core layers needed for a managed Unraid plugin.

It is intentionally narrow:

- versioned multi-VM configuration with legacy single-domain translation;
- a loopback-only status/configuration API and Unraid lifecycle helpers;
- a fixed minimum and maximum target;
- grow on sustained low usable memory or new swap activity;
- shrink only after sustained high headroom;
- hold allocation on missing or stale telemetry;
- append every completed decision to a local JSON Lines audit log;
- dry-run by default;
- no cloud account, database, agent, or dependency beyond Python 3.11, `virsh`, and the standard library.

This project does **not** create memory from nothing. It tells a guest with an already-configured virtio balloon to release or reclaim memory inside a preconfigured maximum.

## Why this exists

Libvirt exposes the mechanism but does not supply automatic policy: a host administrator must explicitly set the balloon target. Generic projects exist, but the common small daemons are old, alpha-labelled, or too broad for a one-host setup. This project keeps the policy local, explicit, and easy to turn off.

## Safety model

The controller fails closed:

1. It requires `actual`, `available`, `usable`, `last_update`, `swap_in`, and `swap_out` from `virsh dommemstat`.
2. It refuses to act if statistics are older than `stale_after_seconds`.
3. It never requests a target outside `min_gib..max_gib`.
4. It changes at most one `step_mib` per run.
5. It requires multiple pressure/headroom samples and imposes a cooldown after an actual change.
6. It uses a non-blocking lock so timer overlap becomes a logged no-op.
7. It appends a JSON Lines audit record for every completed sampling decision.
8. `dry_run = true` is the default. Dry run records the policy decision and state, but never calls `virsh setmem`.

Do not confuse the *allocation target* with host RSS. QEMU can lazily back guest pages; ballooning operates at the guest’s memory-management layer.

## Prerequisites

On the host:

- libvirt and `virsh`;
- Python **3.11+** (uses stdlib `tomllib`);
- permission for the service user to query the target domain; `setmem` permission only after leaving dry run.

For the VM:

- QEMU/libvirt maximum memory set to the chosen ceiling;
- a `virtio` memory-balloon device;
- a guest with the virtio-balloon driver;
- current balloon statistics. Enable and validate a sample period before trusting a controller:

```bash
virsh dommemstat YOUR_DOMAIN --period 10 --live
virsh dommemstat YOUR_DOMAIN
```

`last_update` must advance and `available`/`usable` must be plausible before proceeding. If it does not, stop there; an autoscaler driven by stale statistics is merely a randomized hostage negotiator.

## Quick start: dry run

```bash
install -d -m 0750 /etc/libvirt-balloon-keeper /var/lib/libvirt-balloon-keeper /var/log/libvirt-balloon-keeper
install -m 0640 config.example.toml /etc/libvirt-balloon-keeper/config.toml
$EDITOR /etc/libvirt-balloon-keeper/config.toml

# Validate syntax and bounds without contacting libvirt.
python3 balloon_keeper.py --config /etc/libvirt-balloon-keeper/config.toml --check-config

# Execute one non-mutating policy tick.
python3 balloon_keeper.py --config /etc/libvirt-balloon-keeper/config.toml
```

Inspect the output and state file over enough idle and active samples to verify the threshold behavior. Leave `dry_run = true` through that review.

## Policy defaults

| Setting | Default | Purpose |
|---|---:|---|
| `min_gib` | 4 | Absolute floor for the balloon target |
| `max_gib` | 16 | Absolute ceiling (must fit the domain maximum) |
| `step_mib` | 512 | Maximum change per tick |
| `low_usable_percent` | 20 | Sustained pressure threshold |
| `high_usable_percent` | 60 | Sustained idle/headroom threshold |
| `grow_samples` | 2 | Pressure samples needed before growth |
| `shrink_samples` | 20 | Headroom samples needed before shrinking |
| `cooldown_seconds` | 300 | Minimum time between real target changes |
| `stale_after_seconds` | 45 | Freshness requirement for `last_update` |
| `swap_activity_threshold` | 65,536 KiB (64 MiB) | New swap-counter activity that counts as pressure |

At a 30-second timer cadence, the defaults require roughly one minute of pressure to grow and ten minutes of headroom to shrink. A 512 MiB step and five-minute cooldown deliberately make it slow to reclaim; avoiding guest swap is more important than winning a spreadsheet contest against the host.

## Service installation

The repository includes `systemd` service and timer templates for conventional
Linux hosts. **Do not use them on Unraid**: its OS runs from RAM and its startup
model is `/boot/config/go`. Use the persistent Unraid layout in
[docs/unraid.md](docs/unraid.md) instead.

For a conventional systemd host, install them only after dry-run validation:

```bash
install -m 0755 balloon_keeper.py /usr/local/sbin/libvirt-balloon-keeper
install -m 0644 systemd/libvirt-balloon-keeper.service /etc/systemd/system/
install -m 0644 systemd/libvirt-balloon-keeper.timer /etc/systemd/system/
systemctl daemon-reload

# Review decisions first. This timer still honors dry_run=true.
systemctl enable --now libvirt-balloon-keeper.timer
journalctl -u libvirt-balloon-keeper.service -f
```

When review is satisfactory, change only the local deployed config from `dry_run = true` to `dry_run = false`. Do not commit deployed files with real domain names or host paths.

To stop automation immediately:

```bash
systemctl disable --now libvirt-balloon-keeper.timer
```

The current balloon target remains unchanged when the timer is disabled.

## Testing

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile balloon_keeper.py
python3 balloon_keeper.py --config config.example.toml --check-config
```

The tests use fake adapters and temporary state. They cover stale/missing telemetry, hysteresis, cooldown, min/max bounds, swap-triggered growth, dry-run/live mutation, target read-back, versioned multi-VM config, migration, atomic persistence, lock contention, scheduler failure isolation, lifecycle helpers, health classification, and the loopback WebGUI/API.

For coverage and static/security checks:

```bash
python3 -m coverage run --branch -m unittest discover -s tests
python3 -m coverage report --fail-under=90
python3 -m py_compile balloon_keeper.py libvirt_balloon_keeper/*.py
bash -n unraid/*.sh
```

Build a deterministic transfer bundle (the output is ignored by Git):

```bash
VERSION=2026.09.01 bash unraid/build-package.sh
sha256sum -c dist/libvirt-balloon-keeper.tar.gz.sha256
```

## Repository hygiene

- `config.example.toml` is intentionally generic.
- Deployed configuration, state, logs, local virtualenvs, and test caches are ignored.
- Do not commit VM names, hosts, SSH configuration, IPs, credentials, or telemetry dumps from a real machine.

For contribution rules and Git Flow, see [CONTRIBUTING.md](CONTRIBUTING.md).
For release and Community Applications submission workflow, see
[docs/release.md](docs/release.md).

## License

MIT. See [LICENSE](LICENSE).
