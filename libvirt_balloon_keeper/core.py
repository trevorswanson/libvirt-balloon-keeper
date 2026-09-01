"""Pure policy decisions; no filesystem, subprocess, or Unraid dependencies."""
from __future__ import annotations

from dataclasses import dataclass

KIB_PER_GIB = 1024 * 1024


@dataclass(frozen=True)
class PolicyConfig:
    min_kib: int = 4 * KIB_PER_GIB
    max_kib: int = 16 * KIB_PER_GIB
    step_kib: int = 512 * 1024
    low_usable_percent: int = 20
    high_usable_percent: int = 60
    grow_samples: int = 2
    shrink_samples: int = 20
    cooldown_seconds: int = 300
    stale_after_seconds: int = 45
    swap_activity_threshold: int = 64 * 1024


@dataclass(frozen=True)
class Telemetry:
    actual: int
    available: int
    usable: int
    last_update: int
    swap_in: int
    swap_out: int

    @classmethod
    def from_mapping(cls, stats: dict[str, int]) -> "Telemetry":
        missing = sorted(set(cls.__dataclass_fields__) - stats.keys())
        if missing:
            raise ValueError(f"missing telemetry fields {', '.join(missing)}")
        try:
            values = {name: int(stats[name]) for name in cls.__dataclass_fields__}
        except (TypeError, ValueError) as exc:
            raise ValueError("telemetry values must be integers") from exc
        return cls(**values)


@dataclass
class State:
    low_samples: int = 0
    high_samples: int = 0
    last_change_epoch: float = 0.0
    last_success_epoch: float = 0.0
    last_result: str = ""
    last_swap_in: int | None = None
    last_swap_out: int | None = None


def _valid_telemetry(policy: PolicyConfig, telemetry: Telemetry, now: float) -> str | None:
    if telemetry.last_update <= 0 or telemetry.last_update > now or now - telemetry.last_update > policy.stale_after_seconds:
        return "hold: telemetry is stale"
    if telemetry.available <= 0 or telemetry.usable < 0 or telemetry.usable > telemetry.available:
        return "hold: invalid available/usable telemetry"
    if telemetry.swap_in < 0 or telemetry.swap_out < 0:
        return "hold: invalid swap telemetry"
    if not policy.min_kib <= telemetry.actual <= policy.max_kib:
        return f"hold: current target {telemetry.actual} KiB outside configured range"
    return None


def decide(policy: PolicyConfig, state: State, telemetry: Telemetry, now: float) -> tuple[str, int | None]:
    invalid = _valid_telemetry(policy, telemetry, now)
    if invalid:
        return invalid, None

    swap_delta = 0
    if state.last_swap_in is not None and state.last_swap_out is not None:
        swap_delta = max(0, telemetry.swap_in - state.last_swap_in) + max(0, telemetry.swap_out - state.last_swap_out)
    state.last_swap_in, state.last_swap_out = telemetry.swap_in, telemetry.swap_out
    # Compare reclaimable guest memory with the balloon's current target.
    # `available` remains a useful validity check, but it is guest-dependent
    # and would make the configured percentage move as guest overhead changes.
    usable_percent = 100.0 * telemetry.usable / telemetry.actual
    low_pressure = usable_percent <= policy.low_usable_percent or swap_delta >= policy.swap_activity_threshold
    high_headroom = usable_percent >= policy.high_usable_percent and swap_delta < policy.swap_activity_threshold
    state.low_samples = state.low_samples + 1 if low_pressure else 0
    state.high_samples = state.high_samples + 1 if high_headroom else 0

    if now - state.last_change_epoch < policy.cooldown_seconds:
        if low_pressure:
            return f"hold: pending increase ({state.low_samples}/{policy.grow_samples} rounds; cooldown; {usable_percent:.1f}% of target, swap delta {swap_delta})", None
        if high_headroom:
            return f"hold: pending decrease ({state.high_samples}/{policy.shrink_samples} rounds; cooldown; {usable_percent:.1f}% of target)", None
        return f"hold: stable (cooldown; {usable_percent:.1f}% of target, swap delta {swap_delta})", None
    if state.low_samples >= policy.grow_samples and telemetry.actual < policy.max_kib:
        target = min(policy.max_kib, telemetry.actual + policy.step_kib)
        return f"grow: pressure after {state.low_samples} samples ({usable_percent:.1f}% of target, swap delta {swap_delta})", target
    if state.high_samples >= policy.shrink_samples and telemetry.actual > policy.min_kib:
        target = max(policy.min_kib, telemetry.actual - policy.step_kib)
        return f"shrink: sustained headroom for {state.high_samples} samples ({usable_percent:.1f}% of target)", target
    if low_pressure:
        return f"hold: pending increase ({state.low_samples}/{policy.grow_samples} rounds; {usable_percent:.1f}% of target, swap delta {swap_delta})", None
    if high_headroom:
        return f"hold: pending decrease ({state.high_samples}/{policy.shrink_samples} rounds; {usable_percent:.1f}% of target)", None
    return f"hold: stable ({usable_percent:.1f}% of target, swap delta {swap_delta})", None
