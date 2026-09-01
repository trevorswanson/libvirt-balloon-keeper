# Design notes

## Model

Classic virtio-balloon memory has a fixed guest-visible maximum established by
QEMU/libvirt. The host asks the guest balloon to inflate (reduce usable guest
memory) or deflate (return memory to the guest). Neither QEMU nor libvirt
selects targets from memory pressure on its own.

This project is the policy layer. It executes one sample at a time:

```text
systemd timer
  -> virsh dommemstat DOMAIN
  -> validate freshness and required fields
  -> compare usable/target and new swap activity against policy
  -> preserve state, hysteresis counters, and cooldown
  -> optional virsh setmem DOMAIN TARGET --live
```

## Why a timer, not a daemon?

A one-shot process is easier to audit and less likely to become its own
incident. Systemd supplies restart behavior, cadence, logs, and overlap
control; the process additionally takes a non-blocking lock as defense in
depth.

## Decision rules

The controller reads these libvirt statistics, all in KiB except counters. It
also appends one local JSON Lines audit record per completed sampling decision;
log rotation/retention is deliberately an operator responsibility:

- `actual`: current balloon target;
- `available`: guest memory size available to the kernel;
- `usable`: estimated immediately usable guest memory;
- `swap_in` / `swap_out`: cumulative guest swap activity;
- `last_update`: epoch when balloon statistics were last refreshed.

A target can change only when all fields exist and `last_update` is fresh.

### Grow

Grow one bounded step when either:

1. `usable / actual balloon target <= low_usable_percent` for `grow_samples` consecutive
   samples, or
2. enough new guest swap activity occurred between samples.

### Shrink

Shrink one bounded step only when `usable / actual balloon target >= high_usable_percent`
for `shrink_samples` consecutive samples *and* there was no meaningful new
swap activity. The different low/high bands are hysteresis: a VM hovering near
one threshold should not bounce between two targets.

### Cooldown

After a live change, wait at least `cooldown_seconds` before another. This
allows guest reclaim and balloon accounting to settle before the next decision.

## Failure behavior

| Situation | Controller action |
|---|---|
| Missing libvirt field | Hold target; return an error/no-op reason |
| Statistics too old | Hold target |
| Current target outside configured range | Hold target |
| Overlapping timer invocation | Hold target |
| `virsh setmem` failure | Hold target, append the failed request to the audit log, preserve the prior cooldown, and retry only on a later tick |
| Service/timer stopped | Target remains unchanged |

There is intentionally no host-pressure “emergency reclaim” path in version
one. Reclaiming memory from a guest during host stress is the least forgiving
moment to discover that a heuristic was optimistic. Add it only after the
baseline guest-protection behavior has real operational history.

## Configuration and privacy

The repository contains only an example configuration. Real domain names,
hostnames, paths, telemetry, and credentials belong in the untracked deployed
configuration. The program never opens an SSH connection or leaves the host.

## Plugin architecture

The implementation is layered deliberately:

```text
Unraid lifecycle / cron / WebGUI
            |
      runtime scheduler
            |
   injected libvirt adapter
            |
       policy core
```

`libvirt_balloon_keeper.core` contains only typed telemetry, state, and pure
policy decisions. `config` validates versioned multi-VM TOML and translates the
legacy single-domain file. `adapter` bounds `virsh` calls and verifies a live
mutation by reading the target back. `runtime` owns per-VM locks, atomic state,
audit records, and failure isolation. `unraid` describes persistent storage and
safe lifecycle actions. `web` is loopback-only and validates configuration before
an explicit, atomic save confirmation. It also exposes a bounded `/api/audit?vm=<id>&limit=<1..100>` view and projects each VM's durable `last_success_epoch` and `last_result` heartbeat.

Health helpers classify durable state without exposing paths: missing or malformed
pool-backed state is actionable `error`, an absent/old heartbeat is `stale`, and
an actively held per-VM lock is detectable as duplicate execution. Notification
handling remains deduplicated and rate-limited.

The current Unraid deployment remains a compatibility path under
`/boot/config/custom`; the plugin lifecycle targets `/boot/config/plugins` and
must be migrated and reboot-tested before the compatibility path is removed.
