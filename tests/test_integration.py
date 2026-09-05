import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from libvirt_balloon_keeper.config import load_config
from libvirt_balloon_keeper.health import Health, NotificationError, NotificationGate, UnraidNotifier, classify, health_from_state, lock_held, notify_if_actionable
from libvirt_balloon_keeper.unraid import PluginLayout, cron_entry, lifecycle_actions, validate_layout
from libvirt_balloon_keeper.web import create_server
from libvirt_balloon_keeper.adapter import LibvirtError
from libvirt_balloon_keeper.core import Telemetry
from libvirt_balloon_keeper.runtime import run_schedule, save_state
from libvirt_balloon_keeper.core import State


CONFIG = '''version = 1\n[[vms]]\nid = "example-vm"\ndomain = "example-vm"\nstate_file = "/tmp/example/state.json"\ndecision_log = "/tmp/example/decisions.jsonl"\n'''


class HealthAndUnraidTests(unittest.TestCase):
    def test_schedule_honours_each_vms_interval(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = Path(d) / "state.json"
            audit_path = Path(d) / "decisions.jsonl"
            Path(d, "config.toml").write_text(
                f'''version = 1\n[[vms]]\nid = "vm"\ndomain = "vm"\ninterval_seconds = 60\nstate_file = "{state_path}"\ndecision_log = "{audit_path}"\n'''
            )
            config = load_config(Path(d) / "config.toml")
            save_state(state_path, State(last_success_epoch=100, last_result="hold: stable"))
            class Adapter:
                calls = 0
                def dommemstat(self, domain):
                    self.calls += 1
                    return Telemetry(8 * 1024 * 1024, 7 * 1024 * 1024, 2 * 1024 * 1024, 100, 0, 0)
                def setmem(self, domain, target_kib): raise AssertionError("unexpected mutation")
            adapter = Adapter()
            self.assertEqual(run_schedule(config, adapter, now=150)["vm"], "hold: interval not elapsed")
            self.assertEqual(adapter.calls, 0)
            self.assertIn("hold:", run_schedule(config, adapter, now=161)["vm"])
            self.assertEqual(adapter.calls, 1)

    def test_health_only_notifies_actionable_states(self):
        class N:
            calls = []
            def notify(self, title, detail): self.calls.append((title, detail))
        n = N()
        self.assertFalse(notify_if_actionable(classify("hold: okay"), n))
        self.assertTrue(notify_if_actionable(classify("error: offline"), n))
        self.assertEqual(len(n.calls), 1)
        self.assertEqual(classify("disabled").status, "disabled")
        self.assertEqual(classify("hold", age_seconds=301).status, "stale")

    def test_health_reads_heartbeat_and_detects_held_lock(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state.json"
            state.write_text('{"last_success_epoch": 100, "last_result": "hold: stable"}')
            self.assertEqual(health_from_state(state, 150, stale_after=60).status, "healthy")
            self.assertEqual(health_from_state(state, 200, stale_after=60).status, "stale")
            state.write_text('{"last_success_epoch": "bad", "last_result": "hold"}')
            self.assertEqual(health_from_state(state, 150).detail, "state heartbeat invalid")
            state.write_text('{"last_success_epoch": 100, "last_result": 7}')
            self.assertEqual(health_from_state(state, 150).detail, "state result invalid")
            lock = Path(d) / "tick.lock"
            with lock.open("a+") as handle:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertTrue(lock_held(lock))
            self.assertFalse(lock_held(lock))
            self.assertEqual(health_from_state(Path(d) / "missing.json", 150).detail, "state unavailable")

    def test_notification_gate_deduplicates_rate_limits_and_clears(self):
        class N:
            calls = []
            clears = []
            def notify(self, title, detail): self.calls.append((title, detail))
            def clear(self, title): self.clears.append(title)
        now = [100.0]
        notifier = N()
        gate = NotificationGate(notifier, interval_seconds=60, clock=lambda: now[0])
        error = Health("error", "offline", True)
        self.assertTrue(gate.process("vm", error))
        self.assertFalse(gate.process("vm", error))
        now[0] += 61
        self.assertTrue(gate.process("vm", error))
        self.assertFalse(gate.process("vm", Health("healthy", "hold", False)))
        self.assertEqual(len(notifier.calls), 2)
        self.assertEqual(len(notifier.clears), 1)
        with self.assertRaises(ValueError): NotificationGate(notifier, interval_seconds=0)

    def test_unraid_notifier_uses_bounded_argv_and_hides_failures(self):
        calls = []
        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return type("Result", (), {"returncode": 0})()
        notifier = UnraidNotifier("/usr/bin/notify", runner)
        notifier.notify(" title ", "x" * 700)
        notifier.clear("recovered")
        self.assertEqual(calls[0][0][0:2], ["/usr/bin/notify", "-e"])
        self.assertEqual(len(calls[0][0][6]), 500)
        self.assertEqual(calls[0][1]["check"], False)
        self.assertNotIn("shell", calls[0][1])
        def failed(argv, **kwargs):
            return type("Result", (), {"returncode": 1, "stderr": "secret"})()
        with self.assertRaises(NotificationError): UnraidNotifier("/usr/bin/notify", failed).notify("t", "d")
        with self.assertRaises(ValueError): UnraidNotifier("notify")

        layout = PluginLayout()
        validate_layout(layout)
        self.assertEqual(cron_entry(Path("/boot/config/plugins/x/run-once.sh")), "* * * * * /usr/bin/bash /boot/config/plugins/x/run-once.sh")
        self.assertEqual(len(lifecycle_actions()), 8)
        with self.assertRaises(ValueError): cron_entry(Path("relative"))
        with self.assertRaises(ValueError): validate_layout(PluginLayout(boot_root=Path("relative")))


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"
        self.path.write_text(CONFIG.replace("/tmp/example/state.json", str(Path(self.tmp.name) / "state.json")).replace("/tmp/example/decisions.jsonl", str(Path(self.tmp.name) / "decisions.jsonl")))
        self.audit_path = Path(self.tmp.name) / "decisions.jsonl"
        self.audit_path.write_text('{"reason":"hold"}\nmalformed\n{"reason":"grow"}\n')
        class InventoryAdapter:
            def list_domains(self): return ["example-vm", "shutoff-vm"]
            def domain_state(self, domain): return "shut off" if domain == "shutoff-vm" else "running"
            def supports_virtio_balloon(self, domain): return domain == "example-vm"
            def dommemstat(self, domain):
                if domain == "shutoff-vm": raise LibvirtError("offline")
                return Telemetry(8 * 1024 * 1024, 7 * 1024 * 1024, 2 * 1024 * 1024, 100, 10, 20)
        self.server = create_server(self.path, adapter=InventoryAdapter())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = http.client.HTTPConnection(*self.server.server_address)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        self.client.request(method, path, body=body, headers=headers or {})
        return self.client.getresponse()

    def test_status_is_escaped_and_validate_is_non_mutating(self):
        response = self.request("GET", "/api/config")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read().decode(), self.path.read_text())
        response = self.request("GET", "/api/status")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read())["vms"][0]["id"], "example-vm")
        response = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn("Libvirt Balloon Keeper", response.read().decode())
        response = self.request("GET", "/missing")
        self.assertEqual(response.status, 404)
        response = self.request("POST", "/api/validate", self.path.read_text())
        self.assertEqual(response.status, 200)
        response = self.request("POST", "/api/validate", "bad [toml")
        self.assertEqual(response.status, 400)

    def test_inventory_projects_configured_and_discovered_domains(self):
        response = self.request("GET", "/api/inventory")
        self.assertEqual(response.status, 200)
        payload = json.loads(response.read())
        self.assertEqual([vm["domain"] for vm in payload["vms"]], ["example-vm", "shutoff-vm"])
        self.assertTrue(payload["vms"][0]["configured"])
        self.assertTrue(payload["vms"][0]["virtio_balloon"])
        self.assertEqual(payload["vms"][0]["telemetry"]["usable_kib"], 2 * 1024 * 1024)
        self.assertFalse(payload["vms"][1]["configured"])
        self.assertEqual(payload["vms"][1]["power_state"], "shut off")
        self.assertIsNone(payload["vms"][1]["telemetry"])

    def test_structured_configuration_requires_confirmation_and_round_trips(self):
        response = self.request("POST", "/api/validate-configuration", json.dumps({"version": 1, "vms": []}), {"Content-Type": "application/json"})
        self.assertEqual(response.status, 400)
        response = self.request("POST", "/api/validate-configuration", json.dumps({"version": 1, "vms": [{"id": "bad", "domain": "bad", "interval_seconds": 0, "state_file": "/etc/passwd", "decision_log": "/tmp/log"}]}), {"Content-Type": "application/json"})
        self.assertEqual(response.status, 400)
        body = json.loads(self.request("GET", "/api/inventory").read())
        vm = body["vms"][0]
        response = self.request("POST", "/api/configuration", json.dumps({"version": 1, "vms": [vm]}), {"Content-Type": "application/json"})
        self.assertEqual(response.status, 428)
        headers = {"Content-Type": "application/json", "X-Confirm": "apply"}
        response = self.request("POST", "/api/configuration", json.dumps({"version": 1, "vms": [vm]}), headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(load_config(self.path).vms[0].id, "example-vm")

    def test_audit_route_is_bounded_and_vm_scoped(self):
        response = self.request("GET", "/api/audit?vm=example-vm&limit=3")
        self.assertEqual(response.status, 200)
        payload = json.loads(response.read())
        self.assertEqual([entry["reason"] for entry in payload["entries"]], ["hold", "grow"])
        response = self.request("GET", "/api/audit?vm=unknown")
        self.assertEqual(response.status, 404)

    def test_audit_reader_skips_oversized_entries(self):
        audit = Path(self.tmp.name) / "decisions.jsonl"
        audit.write_text('{"reason":"ok"}\n' + '{"reason":"' + ('x' * 20000) + '"}\n')
        response = self.request("GET", "/api/audit?vm=example-vm")
        self.assertEqual(response.status, 200)
        self.assertEqual(len(json.loads(response.read())["entries"]), 1)
        response = self.request("GET", "/api/audit?vm=example-vm&limit=101")
        self.assertEqual(response.status, 400)

    def test_native_page_uses_loopback_api_without_shell_writes(self):
        page = Path(__file__).parents[1] / "unraid" / "libvirt-balloon-keeper.page"
        content = page.read_text()
        self.assertIn("/plugins/libvirt-balloon-keeper/api.php", content)
        self.assertIn('id="lbk-state-file" data-key="state_file"', content)
        self.assertIn('id="lbk-decision-log" data-key="decision_log"', content)
        self.assertIn('Menu="Utilities"', content)
        self.assertIn('Icon="libvirt-balloon-keeper.png"', content)
        self.assertIn("req('inventory')", content)
        self.assertIn("Swap activity", content)
        self.assertIn("v.id===v.domain", content)
        self.assertIn('<form markdown="1"', content)
        self.assertIn("\n\n> ", content)
        self.assertNotIn("_plug:", content)
        self.assertIn("> Select the virtual machine whose telemetry", content)
        self.assertIn("> The latest controller result", content)
        self.assertIn("> Unmanaged saves the VM without controlling it", content)
        self.assertNotIn("lbk-tooltip", content)
        self.assertNotIn("Add to configuration", content)
        self.assertIn("if(key.endsWith('_kib'))value*=1024;", content)
        self.assertNotIn("value*=key==='step_kib'?1024:1024*1024", content)
        self.assertIn("100*t.usable_kib/t.actual_kib", content)
        self.assertIn("fmtSwap", content)
        self.assertIn("hideElement(warningEl,!warning)", content)
        self.assertIn('<p id="lbk-warning" hidden></p>', content)
        self.assertNotIn("_(Warning)_", content)
        self.assertIn("function setHidden(id,hidden)", content)
        self.assertIn("closest('dd')", content)
        self.assertIn("style.setProperty('display','none','important')", content)
        self.assertIn("function hideHelpAfter(row,hidden)", content)
        self.assertIn("next.querySelector('blockquote.inline_help')", content)
        self.assertIn("const dl=el.closest('dl')", content)
        self.assertIn('data-key="mode"', content)
        self.assertIn('value="unmanaged"', content)
        self.assertIn('value="dry-run"', content)
        self.assertIn('value="apply"', content)
        self.assertIn("const mode=control('mode').value", content)
        self.assertIn("control('mode').onchange=function(){sync(false);show(selected);}", content)
        self.assertNotIn("row(id).hidden", content)
        self.assertIn("Grow below (% free)", content)
        self.assertIn("Shrink above (% free)", content)
        self.assertNotIn('<strong id="lbk-decision">', content)
        self.assertNotIn("VM powered off</span>", content)
        self.assertNotIn("lbk-help", content)
        self.assertNotIn("<style>", content)
        self.assertNotIn("panel panel-default", content)
        self.assertNotIn(' title="', content)
        self.assertIn("dry_run", content)
        self.assertIn("X-Confirm", content)
        self.assertNotIn('http://127.0.0.1:8765', content)
        self.assertNotIn("shell_exec", content)
        self.assertNotIn("/update.php", content)

        proxy = Path(__file__).parents[1] / "unraid" / "api.php"
        proxy_content = proxy.read_text()
        self.assertIn("same-origin request required", proxy_content)
        self.assertIn("/var/run/libvirt-balloon-keeper-api.sock", proxy_content)
        self.assertIn("stream_socket_client", proxy_content)
        self.assertIn("X-Confirm: apply", proxy_content)
        self.assertNotIn("127.0.0.1:8765", proxy_content)
        self.assertIn("'status'", proxy_content)
        self.assertIn("'inventory'", proxy_content)
        self.assertIn("'save-configuration'", proxy_content)
        self.assertIn("array('save', 'save-configuration')", proxy_content)
        self.assertNotIn("if ($route === 'save'", proxy_content)
        self.assertNotIn("shell_exec", proxy_content)
        self.assertNotIn("file_get_contents('http://", proxy_content)
        self.assertIn("Cache-Control: no-store", proxy_content)

    def test_native_page_disables_inventory_cache(self):
        page = (Path(__file__).resolve().parents[1] / "unraid" / "libvirt-balloon-keeper.page").read_text()
        self.assertIn("opt.cache='no-store'", page)
        self.assertIn("X-CSRF-Token", page)
        self.assertIn("csrf_token", page)

    def test_manifest_is_immutable_and_integrity_pinned(self):
        manifest = (Path(__file__).resolve().parents[1] / "unraid" / "libvirt-balloon-keeper.plg").read_text()
        self.assertIn("<URL>https://github.com/trevorswanson/libvirt-balloon-keeper/releases/download/&version;/&name;-&version;.tar.gz</URL>", manifest)
        self.assertIn("<!ENTITY sha256    \"96797f94ab6b7ed0cfdbd777afcd4929f2f40d85e2582b4036f8ba347f853e19\">", manifest)
        self.assertIn("<SHA256>&sha256;</SHA256>", manifest)
        self.assertNotIn("releases/latest", manifest)
        self.assertNotIn("curl --fail", manifest)

    def test_config_save_requires_confirmation_and_is_atomic(self):
        updated = self.path.read_text().replace('id = "example-vm"', 'id = "updated-vm"').replace('domain = "example-vm"', 'domain = "updated-vm"')
        response = self.request("POST", "/api/config", updated)
        self.assertEqual(response.status, 428)
        outside = updated.replace(str(Path(self.tmp.name) / "state.json"), "/etc/passwd")
        response = self.request("POST", "/api/config", outside, {"X-Confirm": "apply"})
        self.assertEqual(response.status, 400)
        response = self.request("POST", "/api/config", updated, {"X-Confirm": "apply"})
        self.assertEqual(response.status, 200)
        self.assertEqual(load_config(self.path).vms[0].id, "updated-vm")
        response = self.request("POST", "/api/config", "version=1\n", {"X-Confirm": "apply"})
        self.assertEqual(response.status, 400)
        self.assertEqual(load_config(self.path).vms[0].id, "updated-vm")

    def test_server_rejects_non_loopback_binding(self):
        with self.assertRaises(ValueError): create_server(self.path, host="0.0.0.0")


if __name__ == "__main__": unittest.main()
