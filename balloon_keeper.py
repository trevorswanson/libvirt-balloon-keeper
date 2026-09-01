#!/usr/bin/env python3
"""Compatibility CLI for the layered libvirt-balloon-keeper package."""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from libvirt_balloon_keeper.adapter import LibvirtError, VirshAdapter
from libvirt_balloon_keeper.config import VMConfig, load_config as load_app_config
from libvirt_balloon_keeper.config import validate_policy
from libvirt_balloon_keeper.core import KIB_PER_GIB, PolicyConfig, State, Telemetry, decide as _decide
from libvirt_balloon_keeper.health import UnraidNotifier
from libvirt_balloon_keeper.runtime import BalloonAdapter, load_state, run_schedule, run_vm_tick

CommandError = LibvirtError


@dataclass(frozen=True)
class Config(PolicyConfig):
    domain: str = ""
    dry_run: bool = True
    state_file: Path = Path("/var/lib/libvirt-balloon-keeper/state.json")
    decision_log: Path = Path("/var/log/libvirt-balloon-keeper/decisions.jsonl")


def load_config(path: Path) -> Config:
    app = load_app_config(path)
    if len(app.vms) != 1:
        raise ValueError("compatibility CLI requires exactly one configured VM")
    vm = app.vms[0]
    return Config(domain=vm.domain, min_kib=vm.policy.min_kib, max_kib=vm.policy.max_kib,
                  step_kib=vm.policy.step_kib, low_usable_percent=vm.policy.low_usable_percent,
                  high_usable_percent=vm.policy.high_usable_percent, grow_samples=vm.policy.grow_samples,
                  shrink_samples=vm.policy.shrink_samples, cooldown_seconds=vm.policy.cooldown_seconds,
                  stale_after_seconds=vm.policy.stale_after_seconds,
                  swap_activity_threshold=vm.policy.swap_activity_threshold, dry_run=vm.dry_run,
                  state_file=vm.state_file, decision_log=vm.decision_log)


def validate_config(config: Config) -> None:
    if not config.domain.strip():
        raise ValueError("config requires a non-empty domain")
    validate_policy(config)


def decide(config: Config, state: State, stats: dict[str, int], now: float) -> tuple[str, int | None]:
    try:
        telemetry = Telemetry.from_mapping(stats)
    except ValueError as exc:
        return f"hold: {exc}", None
    return _decide(config, state, telemetry, now)


def run_tick(config: Config, virsh: BalloonAdapter, now: float | None = None) -> tuple[str, int | None]:
    vm = VMConfig(id=config.domain, domain=config.domain, policy=config, dry_run=config.dry_run,
                  state_file=config.state_file, decision_log=config.decision_log)
    return run_vm_tick(vm, virsh, now)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--notify-command", type=str, default=None,
                        help="absolute Unraid notify command; enables health alerts")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.check_config:
            print(f"configuration valid for domain {config.domain!r}; dry_run={config.dry_run}")
            return 0
        app = load_app_config(args.config)
        notifier = UnraidNotifier(args.notify_command) if args.notify_command else None
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        results = run_schedule(app, VirshAdapter(), notifier=notifier)
        for vm_id, reason in results.items():
            logging.info("vm=%s %s", vm_id, reason)
        return 0 if not any(reason.startswith("error:") for reason in results.values()) else 2
    except (ValueError, CommandError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
