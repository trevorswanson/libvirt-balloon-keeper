"""Unraid-specific paths and lifecycle command planning."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PluginLayout:
    boot_root: Path = Path("/boot/config/plugins/libvirt-balloon-keeper")
    pool_root: Path = Path("/mnt/cache/appdata/libvirt-balloon-keeper")
    cron_marker: str = "libvirt-balloon-keeper"

    @property
    def config(self) -> Path:
        return self.boot_root / "config.toml"

    @property
    def state_root(self) -> Path:
        return self.pool_root / "state"

    @property
    def log_root(self) -> Path:
        return self.pool_root / "logs"


def validate_layout(layout: PluginLayout) -> None:
    for name, path in (("boot_root", layout.boot_root), ("pool_root", layout.pool_root)):
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError(f"{name} must be an absolute non-traversing path")
    if layout.boot_root == layout.pool_root:
        raise ValueError("boot and pool storage must be separate")


def cron_entry(wrapper: Path, interval_minutes: int = 1) -> str:
    if interval_minutes <= 0 or 60 % interval_minutes:
        raise ValueError("interval_minutes must be a positive divisor of 60")
    if not wrapper.is_absolute() or ".." in wrapper.parts:
        raise ValueError("wrapper must be an absolute non-traversing path")
    schedule = "*" if interval_minutes == 1 else f"*/{interval_minutes}"
    return f"{schedule} * * * * /usr/bin/bash {wrapper}"


def lifecycle_actions() -> tuple[str, ...]:
    return ("install", "start", "stop", "restart", "upgrade", "rollback", "uninstall", "check", "migrate", "recover")
