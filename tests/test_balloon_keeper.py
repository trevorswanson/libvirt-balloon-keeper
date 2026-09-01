import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from balloon_keeper import CommandError, Config, State, decide, load_state, main, run_tick

NOW = 1_800_000_000.0
KIB_PER_GIB = 1024 * 1024


def stats(**changes):
    base = {
        "actual": 8 * KIB_PER_GIB,
        "available": 7 * KIB_PER_GIB,
        "usable": 4 * KIB_PER_GIB,
        "last_update": int(NOW),
        "swap_in": 1000,
        "swap_out": 2000,
    }
    base.update(changes)
    return base


class FakeVirsh:
    def __init__(self, telemetry):
        self.telemetry = telemetry
        self.set_targets = []

    def dommemstat(self, domain):
        self.domain = domain
        return self.telemetry

    def setmem(self, domain, target_kib):
        self.set_targets.append((domain, target_kib))


class FailingSetmemVirsh(FakeVirsh):
    def setmem(self, domain, target_kib):
        self.set_targets.append((domain, target_kib))
        raise CommandError("virsh setmem example-vm 8912896 --live failed: simulated failure")


class CliTests(unittest.TestCase):
    def config_file(self, directory: str) -> Path:
        path = Path(directory) / "config.toml"
        path.write_text(
            "version = 1\n\n[defaults]\ninterval_seconds = 60\n\n"
            "[[vms]]\nid = \"example-vm\"\ndomain = \"example-vm\"\n"
            "state_file = \"/tmp/example-vm-state.json\"\n"
            "decision_log = \"/tmp/example-vm-decisions.jsonl\"\n"
        )
        return path

    def test_main_compatibility_path_runs_without_notifier(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config_file(directory)
            with patch.object(sys, "argv", ["balloon_keeper.py", "--config", str(config)]), \
                 patch("balloon_keeper.run_schedule", return_value={"example-vm": "hold: telemetry stale"}) as schedule, \
                 patch("balloon_keeper.VirshAdapter"):
                self.assertEqual(main(), 0)
            self.assertIsNone(schedule.call_args.kwargs["notifier"])

    def test_main_notifier_option_and_error_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config_file(directory)
            with patch.object(sys, "argv", ["balloon_keeper.py", "--config", str(config),
                                               "--notify-command", "/usr/bin/notify"]), \
                 patch("balloon_keeper.UnraidNotifier") as notifier, \
                 patch("balloon_keeper.run_schedule", return_value={"example-vm": "error: offline"}) as schedule, \
                 patch("balloon_keeper.VirshAdapter"):
                self.assertEqual(main(), 2)
            notifier.assert_called_once_with("/usr/bin/notify")
            self.assertIs(schedule.call_args.kwargs["notifier"], notifier.return_value)


class PolicyTests(unittest.TestCase):
    def config(self, **changes):
        base = dict(
            domain="example-vm",
            min_kib=4 * KIB_PER_GIB,
            max_kib=16 * KIB_PER_GIB,
            step_kib=512 * 1024,
            low_usable_percent=20,
            high_usable_percent=60,
            grow_samples=2,
            shrink_samples=3,
            cooldown_seconds=60,
            stale_after_seconds=45,
            swap_activity_threshold=256,
        )
        base.update(changes)
        return Config(**base)

    def test_stale_telemetry_holds(self):
        state = State()
        reason, target = decide(self.config(), state, stats(last_update=int(NOW - 46)), NOW)
        self.assertEqual(target, None)
        self.assertIn("stale", reason)

    def test_missing_telemetry_holds(self):
        sample = stats()
        del sample["usable"]
        reason, target = decide(self.config(), State(), sample, NOW)
        self.assertEqual(target, None)
        self.assertIn("missing", reason)

    def test_pressure_requires_consecutive_samples_then_grows(self):
        config = self.config()
        state = State(last_change_epoch=NOW - 120)
        pressured = stats(usable=1 * KIB_PER_GIB)
        reason, target = decide(config, state, pressured, NOW)
        self.assertIsNone(target)
        self.assertIn("pending increase (1/2 rounds", reason)
        reason, target = decide(config, state, pressured, NOW + 30)
        self.assertEqual(target, int(8.5 * KIB_PER_GIB))
        self.assertIn("grow", reason)

    def test_pressure_percentage_uses_balloon_target(self):
        config = self.config(grow_samples=1)
        state = State(last_change_epoch=NOW - 120)
        reason, target = decide(config, state, stats(actual=8 * KIB_PER_GIB,
                                                     available=4 * KIB_PER_GIB,
                                                     usable=int(1.6 * KIB_PER_GIB)), NOW)
        self.assertEqual(target, int(8.5 * KIB_PER_GIB))
        self.assertIn("20.0% of target", reason)

    def test_headroom_reports_pending_decrease_rounds(self):
        config = self.config()
        state = State(last_change_epoch=NOW - 120, last_swap_in=1000, last_swap_out=2000)
        reason, target = decide(config, state, stats(usable=5 * KIB_PER_GIB), NOW)
        self.assertIsNone(target)
        self.assertIn("pending decrease (1/3 rounds", reason)

    def test_swap_activity_grows_even_when_usable_is_high(self):
        config = self.config()
        state = State(last_change_epoch=NOW - 120, last_swap_in=1000, last_swap_out=2000, low_samples=1)
        reason, target = decide(config, state, stats(usable=5 * KIB_PER_GIB, swap_out=2300), NOW)
        self.assertEqual(target, int(8.5 * KIB_PER_GIB))
        self.assertIn("swap delta 300", reason)

    def test_sustained_headroom_shrinks_but_not_past_floor(self):
        config = self.config()
        state = State(last_change_epoch=NOW - 120, high_samples=2, last_swap_in=1000, last_swap_out=2000)
        reason, target = decide(config, state, stats(usable=5 * KIB_PER_GIB), NOW)
        self.assertEqual(target, int(7.5 * KIB_PER_GIB))
        self.assertIn("shrink", reason)
        floor_state = State(last_change_epoch=NOW - 120, high_samples=2, last_swap_in=1000, last_swap_out=2000)
        _, at_floor = decide(config, floor_state, stats(actual=4 * KIB_PER_GIB, usable=5 * KIB_PER_GIB), NOW)
        self.assertIsNone(at_floor)

    def test_invalid_telemetry_holds(self):
        for sample in (
            stats(usable=8 * KIB_PER_GIB),
            stats(last_update=int(NOW + 1)),
            stats(swap_in=-1),
            stats(actual=17 * KIB_PER_GIB),
        ):
            with self.subTest(sample=sample):
                reason, target = decide(self.config(), State(), sample, NOW)
                self.assertIsNone(target)
                self.assertTrue(reason.startswith("hold:"))

    def test_cooldown_blocks_action(self):
        config = self.config()
        state = State(last_change_epoch=NOW - 10, low_samples=1, last_swap_in=1000, last_swap_out=2000)
        reason, target = decide(config, state, stats(usable=1 * KIB_PER_GIB), NOW)
        self.assertIsNone(target)
        self.assertIn("cooldown", reason)

    def test_dry_run_never_mutates_but_saves_state(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(
                grow_samples=1,
                cooldown_seconds=1,
                dry_run=True,
                state_file=Path(directory) / "state.json",
                decision_log=Path(directory) / "decisions.jsonl",
            )
            fake = FakeVirsh(stats(usable=1 * KIB_PER_GIB))
            reason, target = run_tick(config, fake, NOW)
            self.assertEqual(target, int(8.5 * KIB_PER_GIB))
            self.assertEqual(fake.set_targets, [])
            self.assertIn("dry-run", reason)
            self.assertTrue(config.state_file.exists())
            self.assertIn('"dry_run": true', config.decision_log.read_text())

    def test_live_mode_calls_setmem_and_records_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(
                grow_samples=1,
                cooldown_seconds=1,
                dry_run=False,
                state_file=Path(directory) / "state.json",
                decision_log=Path(directory) / "decisions.jsonl",
            )
            fake = FakeVirsh(stats(usable=1 * KIB_PER_GIB))
            reason, target = run_tick(config, fake, NOW)
            self.assertEqual(target, int(8.5 * KIB_PER_GIB))
            self.assertEqual(fake.set_targets, [("example-vm", target)])
            self.assertIn("applied", reason)

    def test_setmem_failure_is_audited_and_does_not_start_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(
                grow_samples=1,
                cooldown_seconds=60,
                dry_run=False,
                state_file=Path(directory) / "state.json",
                decision_log=Path(directory) / "decisions.jsonl",
            )
            fake = FailingSetmemVirsh(stats(usable=1 * KIB_PER_GIB))
            reason, target = run_tick(config, fake, NOW)
            self.assertEqual(target, int(8.5 * KIB_PER_GIB))
            self.assertIn("setmem failed", reason)
            self.assertEqual(load_state(config.state_file).last_change_epoch, 0.0)
            audit = json.loads(config.decision_log.read_text())
            self.assertEqual(audit["requested_target_kib"], target)
            self.assertIn("setmem failed", audit["reason"])

    def test_invalid_state_stops_before_collecting_telemetry(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            state_file.write_text("this is not JSON")
            config = self.config(state_file=state_file, decision_log=Path(directory) / "decisions.jsonl")
            fake = FakeVirsh(stats())
            with self.assertRaisesRegex(ValueError, "invalid state file"):
                run_tick(config, fake, NOW)
            self.assertFalse(hasattr(fake, "domain"))
            self.assertEqual(fake.set_targets, [])


if __name__ == "__main__":
    unittest.main()
