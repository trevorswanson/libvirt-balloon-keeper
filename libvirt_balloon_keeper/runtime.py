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


def _open_parent(path: Path) -> int:
    """Open the runtime file's parent directory without following symlinks."""
    _reject_symlink(path)
    try:
        return os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError(f"runtime path parent unavailable: {path}") from exc


class BalloonAdapter(Protocol):
    def dommemstat(self, domain: str) -> Telemetry | dict[str, int]: ...
    def ensure_memory_stats(self, domain: str, period_seconds: int = 10) -> bool: ...
    def setmem(self, domain: str, target_kib: int) -> None: ...


def load_state(path: Path) -> State:
    parent_fd = _open_parent(path)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        with os.fdopen(fd, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return State()
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"invalid state file {path}") from exc
    finally:
        os.close(parent_fd)
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
    path.parent.mkdir(parents=True, exist_ok=True)
    parent_fd = _open_parent(path)
    temporary: Path | None = None
    temporary_name: str | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(prefix=".state-", dir=path.parent)
        temporary = Path(path.parent, temporary_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(state), handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
        os.close(parent_fd)


def append_decision(path: Path, *, now: float, vm: VMConfig, telemetry: Telemetry | None, target: int | None, reason: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parent_fd = _open_parent(path)
    entry = {
        "epoch": int(now), "vm_id": vm.id, "domain": vm.domain,
        "actual_kib": telemetry.actual if telemetry else None,
        "available_kib": telemetry.available if telemetry else None,
        "usable_kib": telemetry.usable if telemetry else None,
        "last_update": telemetry.last_update if telemetry else None,
        "requested_target_kib": target, "dry_run": vm.dry_run, "reason": reason,
    }
    try:
        fd = os.open(path.name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o640, dir_fd=parent_fd)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise ValueError(f"runtime audit file unavailable: {path}") from exc
    finally:
        os.close(parent_fd)


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
        ensure_memory_stats = getattr(adapter, "ensure_memory_stats", None)
        if ensure_memory_stats is not None:
            ensure_memory_stats(vm.domain)
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
