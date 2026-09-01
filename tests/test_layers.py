import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from libvirt_balloon_keeper.adapter import LibvirtError, VirshAdapter
from libvirt_balloon_keeper.config import load_config, migrate_legacy_config
from libvirt_balloon_keeper.core import KIB_PER_GIB, State, Telemetry
from libvirt_balloon_keeper.runtime import load_state, run_schedule, run_vm_tick, save_state


NOW = 1_800_000_000.0


def telemetry(**changes):
    value = {"actual": 8 * KIB_PER_GIB, "available": 7 * KIB_PER_GIB, "usable": 1 * KIB_PER_GIB,
             "last_update": int(NOW), "swap_in": 1000, "swap_out": 2000}
    value.update(changes)
    return value


class FakeAdapter:
    def __init__(self, sample=None, error=None):
        self.sample = sample or telemetry()
        self.error = error
        self.calls = []

    def dommemstat(self, domain):
        self.calls.append(("stats", domain))
        if self.error:
            raise self.error
        return self.sample

    def setmem(self, domain, target):
        self.calls.append(("setmem", domain, target))


def vm_config(tmp, name="vm", **kwargs):
    from libvirt_balloon_keeper.config import VMConfig
    from libvirt_balloon_keeper.core import PolicyConfig
    policy = PolicyConfig(grow_samples=1, shrink_samples=2, cooldown_seconds=1, swap_activity_threshold=256)
    values = dict(id=name, domain=name, policy=policy, dry_run=True,
                  state_file=tmp / f"{name}.json", decision_log=tmp / f"{name}.jsonl")
    values.update(kwargs)
    return VMConfig(**values)


class ConfigTests(unittest.TestCase):
    def test_loads_versioned_multiple_vms_and_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text('''version = 1\n[defaults]\nmin_gib = 2\ninterval_seconds = 30\n[[vms]]\nid = "one"\ndomain = "example-one"\nstate_file = "/tmp/one/state.json"\ndecision_log = "/tmp/one/log.jsonl"\n[[vms]]\nid = "two"\ndomain = "example-two"\nenabled = false\nstate_file = "/tmp/two/state.json"\ndecision_log = "/tmp/two/log.jsonl"\n''')
            config = load_config(path)
            self.assertEqual([vm.id for vm in config.vms], ["one", "two"])
            self.assertEqual(config.vms[0].policy.min_kib, 2 * KIB_PER_GIB)
            self.assertFalse(config.vms[1].enabled)
            self.assertEqual(config.vms[0].interval_seconds, 30)

    def test_legacy_config_translates(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "legacy.toml"
            path.write_text('''domain = "example-vm"\n[policy]\nmin_gib = 3\n[runtime]\ndry_run = false\nstate_file = "/tmp/state.json"\ndecision_log = "/tmp/log.jsonl"\n''')
            config = load_config(path)
            self.assertEqual(config.vms[0].id, "example-vm")
            self.assertFalse(config.vms[0].dry_run)
            self.assertEqual(config.vms[0].policy.min_kib, 3 * KIB_PER_GIB)

    def test_legacy_config_migrates_atomically_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "legacy.toml"
            destination = root / "plugin" / "config.toml"
            source.write_text('''domain = "example-vm"\n[policy]\nmin_gib = 3\nstep_mib = 256\n[runtime]\ndry_run = false\nstate_file = "/pool/state.json"\ndecision_log = "/pool/log.jsonl"\n''')
            self.assertTrue(migrate_legacy_config(source, destination))
            migrated = load_config(destination)
            vm = migrated.vms[0]
            self.assertEqual(vm.id, "example-vm")
            self.assertFalse(vm.dry_run)
            self.assertEqual(vm.policy.step_kib, 256 * 1024)
            self.assertEqual(vm.state_file, Path("/pool/state.json"))
            self.assertFalse(migrate_legacy_config(source, destination))
            self.assertEqual(destination.stat().st_mode & 0o777, 0o640)

    def test_rejects_malformed_version_empty_vms_and_domain_collisions(self):
        cases = [
            "version=2\n", "version=1\nvms=[]\n", "version=1\n[[vms]]\nid=\"a\"\ndomain=\"x\"\nstate_file=\"/tmp/a\"\ndecision_log=\"/tmp/l\"\n[[vms]]\nid=\"b\"\ndomain=\"x\"\nstate_file=\"/tmp/b\"\ndecision_log=\"/tmp/m\"\n",
            "version=1\ndefaults=\"bad\"\n",
        ]
        for text in cases:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as d:
                path = Path(d) / "bad.toml"
                path.write_text(text)
                with self.assertRaises(ValueError): load_config(path)

    def test_rejects_bad_policy_types_paths_and_intervals(self):
        cases = [
            'version=1\n[[vms]]\nid="a"\ndomain="a"\nmin_gib="x"\nstate_file="/tmp/a"\ndecision_log="/tmp/l"\n',
            'version=1\n[[vms]]\nid="a"\ndomain="a"\nstate_file="/tmp/../a"\ndecision_log="/tmp/l"\n',
            'version=1\n[[vms]]\nid="a"\ndomain="a"\ninterval_seconds=0\nstate_file="/tmp/a"\ndecision_log="/tmp/l"\n',
            'version=1\n[[vms]]\nid="bad id"\ndomain="a"\nstate_file="/tmp/a"\ndecision_log="/tmp/l"\n',
            'version=1\n[[vms]]\nid="a"\ndomain="bad domain"\nstate_file="/tmp/a"\ndecision_log="/tmp/l"\n',
            'version=1\n[[vms]]\nid="a"\ndomain=7\nstate_file="/tmp/a"\ndecision_log="/tmp/l"\n',
        ]
        for text in cases:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as d:
                path = Path(d) / "bad.toml"
                path.write_text(text)
                with self.assertRaises(ValueError): load_config(path)

    def test_rejects_relative_bool_and_policy_boundaries(self):
        cases = [
            'version=1\n[[vms]]\nid="a"\ndomain="a"\nenabled="false"\nstate_file="/tmp/a"\ndecision_log="/tmp/l"\n',
            'version=1\n[[vms]]\nid="a"\ndomain="a"\nstate_file="relative"\ndecision_log="/tmp/l"\n',
            'version=1\n[[vms]]\nid="a"\ndomain="a"\nlow_usable_percent=0\nstate_file="/tmp/a"\ndecision_log="/tmp/l"\n',
        ]
        for text in cases:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as d:
                path = Path(d) / "bad.toml"
                path.write_text(text)
                with self.assertRaises(ValueError): load_config(path)


class AdapterTests(unittest.TestCase):
    def test_parses_valid_stats_and_rejects_missing(self):
        def run(command, **kwargs):
            return type("Result", (), {"returncode": 0, "stdout": "actual 10\navailable 20\nusable 5\nlast_update 100\nswap_in 2\nswap_out 3\n", "stderr": ""})()
        adapter = VirshAdapter(run=run)
        self.assertEqual(adapter.dommemstat("example-vm").actual, 10)

        def missing(command, **kwargs):
            return type("Result", (), {"returncode": 0, "stdout": "actual 10\n", "stderr": ""})()
        with self.assertRaises(LibvirtError):
            VirshAdapter(run=missing).dommemstat("example-vm")

    def test_setmem_reads_back_target_and_reports_mismatch(self):
        outputs = ["", "actual 9\navailable 20\nusable 5\nlast_update 100\nswap_in 2\nswap_out 3\n",
                   "actual 10\navailable 20\nusable 5\nlast_update 100\nswap_in 2\nswap_out 3\n"]
        def run(command, **kwargs):
            return type("Result", (), {"returncode": 0, "stdout": outputs.pop(0), "stderr": ""})()
        VirshAdapter(run=run, sleep=lambda _: None).setmem("example-vm", 10)
        outputs = ["", "actual 9\navailable 20\nusable 5\nlast_update 100\nswap_in 2\nswap_out 3\n"]
        with self.assertRaises(LibvirtError):
            VirshAdapter(run=run, sleep=lambda _: None, readback_timeout_seconds=0).setmem("example-vm", 10)

    def test_command_failure_and_invalid_domain_are_bounded(self):
        def fail(command, **kwargs):
            return type("Result", (), {"returncode": 1, "stdout": "secret", "stderr": "secret"})()
        with self.assertRaisesRegex(LibvirtError, "operation failed"):
            VirshAdapter(run=fail).dommemstat("example-vm")
        with self.assertRaisesRegex(LibvirtError, "invalid domain"):
            VirshAdapter(run=fail).dommemstat("bad domain")

    def test_timeout_and_capability_probe_are_bounded(self):
        def timeout(command, **kwargs):
            raise TimeoutError("timeout")
        with self.assertRaises(LibvirtError): VirshAdapter(run=timeout).dommemstat("example-vm")
        def xml(command, **kwargs):
            return type("Result", (), {"returncode": 0, "stdout": "<memballoon model='virtio'/>", "stderr": ""})()
        adapter = VirshAdapter(run=xml)
        self.assertTrue(adapter.supports_virtio_balloon("example-vm"))
        def state(command, **kwargs):
            return type("Result", (), {"returncode": 0, "stdout": "shut off\n", "stderr": ""})()
        self.assertEqual(VirshAdapter(run=state).domain_state("example-vm"), "shut off")
        with self.assertRaises(LibvirtError): adapter.setmem("example-vm", 0)


class RuntimeTests(unittest.TestCase):
    def test_atomic_state_round_trip_and_schedule_isolates_failures(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            good = vm_config(tmp, "good")
            disabled = vm_config(tmp, "disabled", enabled=False)
            failing = vm_config(tmp, "bad")
            save_state(good.state_file, State(low_samples=2))
            self.assertEqual(load_state(good.state_file).low_samples, 2)
            from libvirt_balloon_keeper.config import AppConfig
            result = run_schedule(AppConfig(1, (good, disabled, failing)), FakeAdapter(), NOW)
            self.assertIn("good", result)
            self.assertEqual(load_state(good.state_file).last_success_epoch, NOW)
            self.assertEqual(result["disabled"], "disabled")
            self.assertIn("bad", result)
            self.assertTrue(good.decision_log.exists())

    def test_live_and_dry_run_paths_and_lock_contention(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            dry = vm_config(tmp, "dry")
            adapter = FakeAdapter(telemetry(usable=0))
            reason, target = run_vm_tick(dry, adapter, NOW)
            self.assertIn("dry-run", reason)
            self.assertEqual([x[0] for x in adapter.calls], ["stats"])
            live = vm_config(tmp, "live", dry_run=False)
            adapter = FakeAdapter(telemetry(usable=0))
            reason, target = run_vm_tick(live, adapter, NOW)
            self.assertIn("applied", reason)
            self.assertEqual(adapter.calls[-1][0], "setmem")
            lock = live.state_file.with_suffix(".json.lock")
            lock.parent.mkdir(parents=True, exist_ok=True)
            with lock.open("a+") as handle:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertIn("another invocation", run_vm_tick(live, adapter, NOW)[0])

    def test_provider_error_is_reported_per_vm(self):
        with tempfile.TemporaryDirectory() as d:
            from libvirt_balloon_keeper.config import AppConfig
            vm = vm_config(Path(d), "broken")
            result = run_schedule(AppConfig(1, (vm,)), FakeAdapter(error=LibvirtError("offline")), NOW)
            self.assertTrue(result["broken"].startswith("error:"))

    def test_schedule_wires_actionable_health_to_notifier(self):
        with tempfile.TemporaryDirectory() as d:
            from libvirt_balloon_keeper.config import AppConfig
            vm = vm_config(Path(d), "broken")
            calls = []
            class N:
                def notify(self, title, detail): calls.append((title, detail))
                def clear(self, title): pass
            result = run_schedule(AppConfig(1, (vm,)), FakeAdapter(error=LibvirtError("offline")), NOW, N())
            self.assertIn("error:", result["broken"])
            self.assertEqual(len(calls), 1)
            self.assertIn("broken", calls[0][0])


if __name__ == "__main__":
    unittest.main()
