"""Versioned configuration with backward-compatible single-domain loading."""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import KIB_PER_GIB, PolicyConfig


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


def _path(value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty path")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    if ".." in path.parts:
        raise ValueError(f"{name} must not contain parent traversal")
    return path


def last_good_path(path: Path) -> Path:
    """Return the sidecar containing the last successfully validated config."""
    return path.with_name(f"{path.name}.last-good")


def atomic_write_text(path: Path, text: str) -> None:
    """Replace text atomically with the config file's restricted permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o640
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def preserve_last_good_config(path: Path) -> None:
    """Snapshot the currently valid config before replacing it."""
    if not path.exists():
        return
    try:
        load_config(path)
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return
    snapshot = last_good_path(path)
    atomic_write_text(snapshot, text)


def recover_config(path: Path) -> AppConfig:
    """Restore the explicit last-good snapshot, refusing valid live config."""
    try:
        load_config(path)
    except (OSError, ValueError):
        pass
    else:
        raise ValueError("current configuration is valid; refusing recovery")
    snapshot = last_good_path(path)
    text = snapshot.read_text(encoding="utf-8")
    config = load_config_from_text(text)
    atomic_write_text(path, text)
    return config


def load_config_from_text(text: str) -> AppConfig:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".toml") as handle:
        handle.write(text)
        handle.flush()
        return load_config(Path(handle.name))


def _vm(raw: dict[str, Any], index: int, defaults: dict[str, Any]) -> VMConfig:
    merged = {**defaults, **raw}
    identifier_value = merged.get("id", "")
    domain_value = merged.get("domain", "")
    if not isinstance(identifier_value, str) or not isinstance(domain_value, str):
        raise ValueError(f"vm[{index}] id and domain must be strings")
    identifier = identifier_value.strip()
    domain = domain_value.strip()
    if not identifier or not domain:
        raise ValueError(f"vm[{index}] requires non-empty id and domain")
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
        state_file=_path(merged.get("state_file", runtime.get("state_file", f"/var/lib/libvirt-balloon-keeper/{identifier}/state.json")), f"vm[{index}].state_file"),
        decision_log=_path(merged.get("decision_log", runtime.get("decision_log", f"/var/log/libvirt-balloon-keeper/{identifier}/decisions.jsonl")), f"vm[{index}].decision_log"),
        interval_seconds=_int(merged, "interval_seconds", 60),
        enabled=_bool(enabled, f"vm[{index}].enabled"),
    )


def migrate_legacy_config(source: Path, destination: Path) -> bool:
    """Translate a legacy config once; never overwrite a plugin config."""
    if destination.exists():
        return False
    config = load_config(source)
    if len(config.vms) != 1:
        raise ValueError("legacy migration requires exactly one VM")
    vm = config.vms[0]
    text = """version = 1

[defaults]
min_gib = {min_gib}
max_gib = {max_gib}
step_mib = {step_mib}
low_usable_percent = {low}
high_usable_percent = {high}
grow_samples = {grow}
shrink_samples = {shrink}
cooldown_seconds = {cooldown}
stale_after_seconds = {stale}
swap_activity_threshold = {swap}
interval_seconds = {interval}

[[vms]]
id = {id}
domain = {domain}
dry_run = {dry_run}
enabled = {enabled}
state_file = {state}
decision_log = {log}
""".format(
        min_gib=vm.policy.min_kib // KIB_PER_GIB,
        max_gib=vm.policy.max_kib // KIB_PER_GIB,
        step_mib=vm.policy.step_kib // 1024,
        low=vm.policy.low_usable_percent,
        high=vm.policy.high_usable_percent,
        grow=vm.policy.grow_samples,
        shrink=vm.policy.shrink_samples,
        cooldown=vm.policy.cooldown_seconds,
        stale=vm.policy.stale_after_seconds,
        swap=vm.policy.swap_activity_threshold,
        interval=vm.interval_seconds,
        id=json.dumps(vm.id),
        domain=json.dumps(vm.domain),
        dry_run=str(vm.dry_run).lower(),
        enabled=str(vm.enabled).lower(),
        state=json.dumps(str(vm.state_file)),
        log=json.dumps(str(vm.decision_log)),
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(destination, text)
    atomic_write_text(last_good_path(destination), text)
    return True


def load_config(path: Path) -> AppConfig:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML configuration: {exc}") from exc
    version = data.get("version", 1)
    if version != 1:
        raise ValueError(f"unsupported configuration version: {version}")
    raw_vms = data.get("vms")
    if raw_vms is None:
        # Legacy configuration is intentionally translated, not silently ignored.
        raw_vms = [{"id": str(data.get("domain", "")).strip(), "domain": data.get("domain", ""), **data.get("policy", {}), **data.get("runtime", {})}]
    if not isinstance(raw_vms, list) or not raw_vms:
        raise ValueError("configuration requires at least one VM")
    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError("defaults must be a table")
    vms = tuple(_vm(item, index, defaults) for index, item in enumerate(raw_vms) if isinstance(item, dict))
    if len(vms) != len(raw_vms):
        raise ValueError("each VM entry must be a table")
    ids = [vm.id for vm in vms]
    domains = [vm.domain for vm in vms]
    if len(set(ids)) != len(ids):
        raise ValueError("VM ids must be unique")
    if len(set(domains)) != len(domains):
        raise ValueError("libvirt domains must be unique")
    if any(vm.interval_seconds <= 0 for vm in vms):
        raise ValueError("interval_seconds must be positive")
    pool_root = data.get("pool_root")
    return AppConfig(version=version, vms=vms, pool_root=_path(pool_root, "pool_root") if pool_root is not None else None)
