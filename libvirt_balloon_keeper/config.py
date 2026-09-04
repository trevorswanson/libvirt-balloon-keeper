"""Versioned multi-VM configuration loading and validation."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import KIB_PER_GIB, PolicyConfig

# Directories the controller may write state and audit files into when a
# configuration arrives from an untrusted source such as the loopback API.
DEFAULT_STATE_ROOTS: tuple[Path, ...] = (
    Path("/var/lib/libvirt-balloon-keeper"),
    Path("/var/log/libvirt-balloon-keeper"),
    Path("/mnt/cache/appdata/libvirt-balloon-keeper"),
)
MAX_VMS = 128
MAX_NAME_LENGTH = 128
MAX_INTERVAL_SECONDS = 7 * 24 * 60 * 60
MAX_SAMPLE_COUNT = 1000


@dataclass(frozen=True)
class VMConfig:
    id: str
    domain: str
    policy: PolicyConfig
    dry_run: bool = True
    state_file: Path = Path("state.json")
    decision_log: Path = Path("decisions.jsonl")
    interval_seconds: int = 60
    enabled: bool = True


@dataclass(frozen=True)
class AppConfig:
    version: int
    vms: tuple[VMConfig, ...]
    pool_root: Path | None = None


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _int(data: dict[str, Any], name: str, default: int) -> int:
    value = data.get(name, default)
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _policy(data: dict[str, Any]) -> PolicyConfig:
    min_gib = _int(data, "min_gib", 4)
    max_gib = _int(data, "max_gib", 16)
    policy = PolicyConfig(
        min_kib=min_gib * KIB_PER_GIB,
        max_kib=max_gib * KIB_PER_GIB,
        step_kib=_int(data, "step_mib", 512) * 1024,
        low_usable_percent=_int(data, "low_usable_percent", 20),
        high_usable_percent=_int(data, "high_usable_percent", 60),
        grow_samples=_int(data, "grow_samples", 2),
        shrink_samples=_int(data, "shrink_samples", 20),
        cooldown_seconds=_int(data, "cooldown_seconds", 300),
        stale_after_seconds=_int(data, "stale_after_seconds", 45),
        swap_activity_threshold=_int(data, "swap_activity_threshold", 64 * 1024),
    )
    validate_policy(policy)
    return policy


def validate_policy(policy: PolicyConfig) -> None:
    if not 0 < policy.min_kib <= policy.max_kib:
        raise ValueError("min_gib must be positive and no larger than max_gib")
    if policy.step_kib <= 0:
        raise ValueError("step_mib must be positive")
    if not 0 < policy.low_usable_percent < policy.high_usable_percent < 100:
        raise ValueError("usable thresholds must satisfy 0 < low < high < 100")
    for name in ("grow_samples", "shrink_samples", "cooldown_seconds", "stale_after_seconds", "swap_activity_threshold"):
        if getattr(policy, name) <= 0:
            raise ValueError(f"{name} must be positive")
    if policy.grow_samples > MAX_SAMPLE_COUNT or policy.shrink_samples > MAX_SAMPLE_COUNT:
        raise ValueError("sample counts are too large")
    if policy.cooldown_seconds > MAX_INTERVAL_SECONDS or policy.stale_after_seconds > MAX_INTERVAL_SECONDS:
        raise ValueError("timing values are too large")
    if policy.swap_activity_threshold > 2**63:
        raise ValueError("swap_activity_threshold is too large")


def _path(value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty path")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    if ".." in path.parts:
        raise ValueError(f"{name} must not contain parent traversal")
    return path


def _confined_path(value: Any, name: str, roots: tuple[Path, ...] | None) -> Path:
    """Like _path, but when roots are given the resolved path must live under one of them."""
    path = _path(value, name)
    if roots is None:
        return path
    resolved = path.resolve()
    if not any(resolved.is_relative_to(root.resolve()) for root in roots):
        raise ValueError(f"{name} must be inside an allowed state directory")
    return path


def _vm(raw: dict[str, Any], index: int, defaults: dict[str, Any], state_roots: tuple[Path, ...] | None = None) -> VMConfig:
    merged = {**defaults, **raw}
    identifier_value = merged.get("id", "")
    domain_value = merged.get("domain", "")
    if not isinstance(identifier_value, str) or not isinstance(domain_value, str):
        raise ValueError(f"vm[{index}] id and domain must be strings")
    identifier = identifier_value.strip()
    domain = domain_value.strip()
    if not identifier or not domain:
        raise ValueError(f"vm[{index}] requires non-empty id and domain")
    if len(identifier) > MAX_NAME_LENGTH or len(domain) > MAX_NAME_LENGTH:
        raise ValueError(f"vm[{index}] id and domain are too long")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", identifier):
        raise ValueError(f"vm[{index}] id contains invalid characters")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", domain):
        raise ValueError(f"vm[{index}] domain contains invalid characters")
    runtime = merged.get("runtime", {})
    if not isinstance(runtime, dict):
        raise ValueError(f"vm[{index}].runtime must be a table")
    # A VM entry may override runtime values directly or through [runtime].
    dry_run = merged.get("dry_run", runtime.get("dry_run", True))
    enabled = merged.get("enabled", True)
    return VMConfig(
        id=identifier,
        domain=domain,
        policy=_policy(merged),
        dry_run=_bool(dry_run, f"vm[{index}].dry_run"),
        state_file=_confined_path(merged.get("state_file", runtime.get("state_file", f"/var/lib/libvirt-balloon-keeper/{identifier}/state.json")), f"vm[{index}].state_file", state_roots),
        decision_log=_confined_path(merged.get("decision_log", runtime.get("decision_log", f"/var/log/libvirt-balloon-keeper/{identifier}/decisions.jsonl")), f"vm[{index}].decision_log", state_roots),
        interval_seconds=_int(merged, "interval_seconds", 60),
        enabled=_bool(enabled, f"vm[{index}].enabled"),
    )


def load_config(path: Path, state_roots: tuple[Path, ...] | None = None) -> AppConfig:
    """Load a configuration; with state_roots, every VM state/audit path must resolve under one of them."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML configuration: {exc}") from exc
    version = data.get("version", 1)
    if version != 1:
        raise ValueError(f"unsupported configuration version: {version}")
    raw_vms = data.get("vms")
    if raw_vms is None:
        raise ValueError("configuration requires a vms table")
    if not isinstance(raw_vms, list) or not raw_vms:
        raise ValueError("configuration requires at least one VM")
    if len(raw_vms) > MAX_VMS:
        raise ValueError(f"configuration supports at most {MAX_VMS} VMs")
    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError("defaults must be a table")
    vms = tuple(_vm(item, index, defaults, state_roots) for index, item in enumerate(raw_vms) if isinstance(item, dict))
    if len(vms) != len(raw_vms):
        raise ValueError("each VM entry must be a table")
    ids = [vm.id for vm in vms]
    domains = [vm.domain for vm in vms]
    if len(set(ids)) != len(ids):
        raise ValueError("VM ids must be unique")
    if len(set(domains)) != len(domains):
        raise ValueError("libvirt domains must be unique")
    if any(vm.interval_seconds <= 0 or vm.interval_seconds > MAX_INTERVAL_SECONDS for vm in vms):
        raise ValueError(f"interval_seconds must be between 1 and {MAX_INTERVAL_SECONDS}")
    pool_root = data.get("pool_root")
    selected_pool = _path(pool_root, "pool_root") if pool_root is not None else None
    if selected_pool is not None:
        from .unraid import validate_pool_root
        validate_pool_root(selected_pool)
    return AppConfig(version=version, vms=vms, pool_root=selected_pool)
