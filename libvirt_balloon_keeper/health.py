"""Health classification and notification boundary."""
from __future__ import annotations

import fcntl
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


@dataclass(frozen=True)
class Health:
    status: str
    detail: str
    actionable: bool


def classify(result: str, *, age_seconds: float | None = None, stale_after: int = 300) -> Health:
    if result == "disabled":
        return Health("disabled", "VM is disabled", False)
    if result.startswith("error:") or result.startswith("hold: setmem failed"):
        detail = result.removeprefix("error: ").removeprefix("hold: ").strip()
        return Health("error", detail, True)
    if age_seconds is not None and age_seconds > stale_after:
        return Health("stale", f"last successful tick is {int(age_seconds)} seconds old", True)
    return Health("healthy", result, False)


def health_from_state(path: Path, now: float, *, stale_after: int = 300) -> Health:
    """Classify durable state without exposing its path or raw contents."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Health("error", "state unavailable", True)
    heartbeat = data.get("last_success_epoch", 0)
    result = data.get("last_result", "")
    if isinstance(heartbeat, bool) or not isinstance(heartbeat, (int, float)) or heartbeat < 0:
        return Health("error", "state heartbeat invalid", True)
    if not isinstance(result, str):
        return Health("error", "state result invalid", True)
    if heartbeat <= 0:
        return Health("stale", "no successful tick recorded", True)
    return classify(result, age_seconds=max(0.0, now - heartbeat), stale_after=stale_after)


def lock_held(path: Path) -> bool:
    """Return whether another process currently owns a non-blocking lock."""
    try:
        with path.open("a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False
    except OSError:
        return False


class NotificationError(OSError):
    """The host notification command failed without exposing its output."""


class UnraidNotifier:
    """Invoke Unraid's notifier with argv, never through a shell."""

    def __init__(self, command: str = "/usr/local/emhttp/webGui/scripts/notify",
                 runner: Callable[..., object] = subprocess.run):
        if not command or not Path(command).is_absolute():
            raise ValueError("notification command must be an absolute path")
        self.command = command
        self.runner = runner

    @staticmethod
    def _bounded(value: str, name: str, limit: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"notification {name} must be non-empty")
        return value.strip()[:limit]

    def _run(self, title: str, detail: str, priority: str) -> None:
        title = self._bounded(title, "title", 120)
        detail = self._bounded(detail, "detail", 500)
        try:
            result = self.runner([self.command, "-e", "libvirt-balloon-keeper", "-s", title,
                                  "-d", detail, "-i", priority],
                                 check=False, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise NotificationError("notification command unavailable") from exc
        if getattr(result, "returncode", 1) != 0:
            raise NotificationError("notification command failed")

    def notify(self, title: str, detail: str) -> None:
        self._run(title, detail, "alert")

    def clear(self, title: str) -> None:
        self._run(title, "controller recovered", "normal")
class Notifier(Protocol):
    def notify(self, title: str, detail: str) -> None: ...
    def clear(self, title: str) -> None: ...


class NotificationGate:
    """Deduplicate repeated failures and clear them on recovery."""

    def __init__(self, notifier: Notifier, *, interval_seconds: int = 900, clock: Callable[[], float] | None = None):
        if interval_seconds <= 0:
            raise ValueError("notification interval must be positive")
        self.notifier = notifier
        self.interval_seconds = interval_seconds
        self.clock = clock or time.time
        self._last: dict[str, tuple[float, str]] = {}

    def process(self, vm_id: str, health: Health) -> bool:
        title = f"libvirt-balloon-keeper: {vm_id}"
        if not health.actionable:
            if vm_id in self._last and hasattr(self.notifier, "clear"):
                self.notifier.clear(title)
            self._last.pop(vm_id, None)
            return False
        now = self.clock()
        fingerprint = f"{health.status}:{health.detail}"
        previous = self._last.get(vm_id)
        if previous and previous[1] == fingerprint and now - previous[0] < self.interval_seconds:
            return False
        self.notifier.notify(title, health.detail[:500])
        self._last[vm_id] = (now, fingerprint)
        return True


def notify_if_actionable(health: Health, notifier: Notifier) -> bool:
    if not health.actionable:
        return False
    notifier.notify(f"libvirt-balloon-keeper: {health.status}", health.detail[:500])
    return True
