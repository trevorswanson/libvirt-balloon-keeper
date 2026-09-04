"""One-shot runtime, durable state, audit, and scheduler orchestration."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from .adapter import LibvirtError
from .config import AppConfig, VMConfig
from .core import State, Telemetry, decide
from .health import NotificationError, NotificationGate, Notifier, classify, health_from_state


def _reject_symlink(path: Path) -> None:
    """Reject symlink components before touching runtime-owned files."""
    current = Path(path.anchor)
    for part in path.parts[1:-1]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"runtime path contains symlink: {path}")
    if path.is_symlink():
        raise ValueError(f"runtime path is a symlink: {path}")


class BalloonAdapter(Protocol):
    def dommemstat(self, domain: str) -> Telemetry | dict[str, int]: ...
    def setmem(self, domain: str, target_kib: int) -> None: ...


def load_state(path: Path) -> State:
    _reject_symlink(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return State()
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"invalid state file {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"invalid state file {path}")
    fields = asdict(State())
    try:
        values = {key: raw[key] for key in fields if key in raw}
        state = State(**values)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid state file {path}") from exc
    if state.low_samples < 0 or state.high_samples < 0 or state.last_change_epoch < 0 or state.last_success_epoch < 0:
        raise ValueError(f"invalid state file {path}")
    if not isinstance(state.last_result, str):
        raise ValueError(f"invalid state file {path}")
    return state


def save_state(path: Path, state: State) -> None:
    _reject_symlink(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".state-", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(asdict(state), handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def append_decision(path: Path, *, now: float, vm: VMConfig, telemetry: Telemetry | None, target: int | None, reason: str) -> None:
    _reject_symlink(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "epoch": int(now), "vm_id": vm.id, "domain": vm.domain,
        "actual_kib": telemetry.actual if telemetry else None,
        "available_kib": telemetry.available if telemetry else None,
        "usable_kib": telemetry.usable if telemetry else None,
        "last_update": telemetry.last_update if telemetry else None,
        "requested_target_kib": target, "dry_run": vm.dry_run, "reason": reason,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def run_vm_tick(vm: VMConfig, adapter: BalloonAdapter, now: float | None = None) -> tuple[str, int | None]:
    now = time.time() if now is None else now
    lock_path = vm.state_file.with_suffix(vm.state_file.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "hold: another invocation owns the lock", None
        state = load_state(vm.state_file)
        telemetry_raw = adapter.dommemstat(vm.domain)
        telemetry = telemetry_raw if isinstance(telemetry_raw, Telemetry) else Telemetry.from_mapping(telemetry_raw)
        reason, target = decide(vm.policy, state, telemetry, now)
        if target is not None:
            if vm.dry_run:
                reason = f"dry-run {reason}; would set {target} KiB"
            else:
                try:
                    adapter.setmem(vm.domain, target)
                except (LibvirtError, OSError) as exc:
                    reason = f"hold: setmem failed; would set {target} KiB: {exc}"
                else:
                    state.last_change_epoch = now
                    reason = f"applied {reason}; set {target} KiB"
        append_decision(vm.decision_log, now=now, vm=vm, telemetry=telemetry, target=target, reason=reason)
        state.last_success_epoch = now
        state.last_result = reason
        save_state(vm.state_file, state)
        return reason, target


def run_schedule(config: AppConfig, adapter: BalloonAdapter, now: float | None = None,
                 notifier: Notifier | None = None, notification_interval_seconds: int = 900) -> dict[str, str]:
    now = time.time() if now is None else now
    gate = NotificationGate(notifier, interval_seconds=notification_interval_seconds, clock=lambda: now) if notifier else None
    results: dict[str, str] = {}
    for vm in config.vms:
        if not vm.enabled:
            results[vm.id] = "disabled"
            health = classify("disabled")
        else:
            try:
                prior_state = load_state(vm.state_file)
                if prior_state.last_success_epoch > 0 and now < prior_state.last_success_epoch + vm.interval_seconds:
                    results[vm.id] = "hold: interval not elapsed"
                else:
                    results[vm.id] = run_vm_tick(vm, adapter, now)[0]
                if results[vm.id].startswith("hold: another invocation"):
                    health = classify(results[vm.id])
                else:
                    health = health_from_state(vm.state_file, now, stale_after=vm.policy.stale_after_seconds)
            except (LibvirtError, OSError, ValueError) as exc:
                results[vm.id] = f"error: {exc}"
                health = classify(results[vm.id])
        if gate:
            try:
                gate.process(vm.id, health)
            except (NotificationError, OSError, ValueError) as exc:
                results[vm.id] = f"{results[vm.id]}; error: notification failed"
                try:
                    append_decision(vm.decision_log, now=now, vm=vm, telemetry=None, target=None,
                                    reason=f"error: notification failed: {type(exc).__name__}")
                except OSError:
                    pass
    return results
