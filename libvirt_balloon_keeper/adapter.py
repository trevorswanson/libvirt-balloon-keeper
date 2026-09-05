"""Narrow, injectable libvirt command adapter."""
from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable

from .core import Telemetry


class LibvirtError(RuntimeError):
    """A bounded, non-sensitive libvirt operation failure."""


class VirshAdapter:
    def __init__(self, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run, executable: str = "virsh",
                 sleep: Callable[[float], None] = time.sleep, readback_timeout_seconds: float = 15.0,
                 readback_interval_seconds: float = 1.0):
        self._run = run
        self._executable = executable
        self._sleep = sleep
        self._readback_timeout_seconds = readback_timeout_seconds
        self._readback_interval_seconds = readback_interval_seconds

    def _call(self, *args: str) -> str:
        command = [self._executable, *args]
        try:
            result = self._run(command, text=True, capture_output=True, check=False, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LibvirtError(f"libvirt command unavailable or timed out: {args[0]}") from exc
        if result.returncode:
            raise LibvirtError(f"libvirt operation failed: {args[0]}")
        return result.stdout

    def dommemstat(self, domain: str) -> Telemetry:
        if not domain or any(char.isspace() for char in domain):
            raise LibvirtError("invalid domain identifier")
        values: dict[str, int] = {}
        for line in self._call("dommemstat", domain).splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            try:
                values[parts[0]] = int(parts[1])
            except ValueError:
                continue
        try:
            return Telemetry.from_mapping(values)
        except ValueError as exc:
            raise LibvirtError(str(exc)) from exc

    def list_domains(self) -> list[str]:
        """Return libvirt domain names, one per line, without shell interpolation."""
        return [line.strip() for line in self._call("list", "--all", "--name").splitlines() if line.strip()]

    def domain_state(self, domain: str) -> str:
        """Return the normalized libvirt power state for a domain."""
        return self._call("domstate", domain).strip().lower()

    def supports_virtio_balloon(self, domain: str) -> bool:
        xml = self._call("dumpxml", domain)
        return "<memballoon" in xml and "model='virtio'" in xml or 'model="virtio"' in xml

    def memory_stats_period(self, domain: str) -> int | None:
        """Return the configured live balloon stats period, if present."""
        if not domain or any(char.isspace() for char in domain):
            raise LibvirtError("invalid domain identifier")
        xml = self._call("dumpxml", domain)
        match = re.search(r"<memballoon\b[^>]*>.*?<stats\s+period=['\"](\d+)['\"]\s*/?>", xml, re.DOTALL)
        return int(match.group(1)) if match else None

    def ensure_memory_stats(self, domain: str, period_seconds: int = 10) -> bool:
        """Enable balloon stats collection when the domain has no period."""
        current = self.memory_stats_period(domain)
        if current is not None and current > 0:
            return False
        self._call("dommemstat", domain, "--period", str(period_seconds), "--live", "--config")
        return True

    def setmem(self, domain: str, target_kib: int) -> None:
        if target_kib <= 0:
            raise LibvirtError("target memory must be positive")
        self._call("setmem", domain, str(target_kib), "--live")
        deadline = time.monotonic() + self._readback_timeout_seconds
        actual = None
        while True:
            actual = self.dommemstat(domain).actual
            if actual == target_kib:
                return
            if time.monotonic() >= deadline:
                break
            self._sleep(min(self._readback_interval_seconds, max(0.0, deadline - time.monotonic())))
        raise LibvirtError(f"libvirt accepted target but read-back did not confirm it (last actual {actual} KiB)")
