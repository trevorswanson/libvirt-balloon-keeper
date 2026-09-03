"""Unraid-specific paths and lifecycle command planning."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


APPDATA_NAME = "appdata/libvirt-balloon-keeper"
PREFERRED_POOL_NAMES = ("cache",)
SHARE_FALLBACK_NAMES = ("user",)


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


def _mounted_directory(path: Path) -> bool:
    """Return true only for an existing mount, never a merely creatable path."""
    return path.is_dir() and os.path.ismount(path)


def discover_storage_root(mount_root: Path = Path("/mnt"), preferred: Path | None = None) -> Path | None:
    """Find persistent Unraid storage without creating a guessed pool tree."""
    candidates: list[Path] = []
    if preferred is not None:
        candidates.append(preferred)
    candidates.extend(mount_root / name for name in PREFERRED_POOL_NAMES)
    try:
        candidates.extend(sorted(
            child for child in mount_root.iterdir()
            if child.is_dir() and child.name not in {*PREFERRED_POOL_NAMES, *SHARE_FALLBACK_NAMES}
        ))
    except OSError:
        return None
    candidates.extend(mount_root / name for name in SHARE_FALLBACK_NAMES)
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        if _mounted_directory(candidate):
            return candidate / APPDATA_NAME
    return None


def validate_pool_root(path: Path) -> None:
    """Validate an explicitly selected Unraid pool before accepting config."""
    if path.parent != Path("/mnt") or not _mounted_directory(path):
        raise ValueError("pool_root must be an existing mounted pool below /mnt")


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
    return ("install", "start", "stop", "restart", "upgrade", "rollback", "uninstall", "check", "migrate")
