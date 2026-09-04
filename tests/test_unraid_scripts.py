import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
def unix_request(path, method, target):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(str(path))
        client.sendall(f"{method} {target} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
        response = b""
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            response += chunk
    header, body = response.split(b"\r\n\r\n", 1)
    status = int(header.splitlines()[0].split()[1])
    return status, body


class UnraidScriptTests(unittest.TestCase):
    def test_run_api_is_idempotent_and_owns_pid_file(self):
        repository = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugin"
            root.mkdir()
            shutil.copy(repository / "web_server.py", root / "web_server.py")
            shutil.copytree(repository / "libvirt_balloon_keeper", root / "libvirt_balloon_keeper")
            shutil.copy(repository / "config.example.toml", root / "config.toml")
            pid_file = Path(directory) / "api.pid"
            socket_path = Path(directory) / "api.sock"
            log_file = Path(directory) / "api.log"
            environment = os.environ.copy()
            environment.update(
                PLUGIN_ROOT=str(root),
                API_PID_FILE=str(pid_file),
                API_LOG_FILE=str(log_file),
                API_SOCKET=str(socket_path),
            )
            script = repository / "unraid" / "run-api.sh"
            subprocess.run(["bash", str(script)], check=True, env=environment)
            pid = int(pid_file.read_text())
            try:
                deadline = time.monotonic() + 5
                while True:
                    try:
                        status, _ = unix_request(socket_path, "GET", "/api/status")
                        self.assertEqual(status, 200)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            self.fail("API did not become ready")
                        time.sleep(0.05)
                subprocess.run(["bash", str(script)], check=True, env=environment)
                self.assertEqual(int(pid_file.read_text()), pid)
                self.assertEqual(socket_path.stat().st_mode & 0o777, 0o600)
            finally:
                os.kill(pid, signal.SIGTERM)
                for _ in range(50):
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.02)
                pid_file.unlink(missing_ok=True)

    def test_run_api_rejects_relative_socket(self):
        repository = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugin"
            root.mkdir()
            (root / "config.toml").write_text("version = 1\n\n[defaults]\n\n[[vms]]\nid = 'x'\ndomain = 'x'\nstate_file = '/tmp/x'\ndecision_log = '/tmp/l'\n")
            environment = os.environ.copy()
            environment.update(PLUGIN_ROOT=str(root), API_SOCKET="relative.sock")
            result = subprocess.run(
                ["bash", str(repository / "unraid" / "run-api.sh")],
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 64)

    def test_lifecycle_migrates_reinstalls_and_stops_cleanly(self):
        repository = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fake_bin = base / "bin"
            fake_bin.mkdir()
            cron_store = base / "crontab"
            (fake_bin / "update_cron").write_text(
                "#!/usr/bin/env bash\n"
                "set -e\n"
                "cat ${PLUGIN_ROOT}/*.cron > ${CRONTAB_STORE} 2>/dev/null || : > ${CRONTAB_STORE}\n"
            )
            (fake_bin / "virsh").write_text("#!/usr/bin/env bash\nexit 0\n")
            (fake_bin / "logger").write_text("#!/usr/bin/env bash\nexit 0\n")
            for executable in fake_bin.iterdir():
                executable.chmod(0o750)

            pool = base / "pool"
            legacy = base / "legacy"
            legacy.mkdir()
            state = pool / "state.json"
            audit = pool / "decisions.jsonl"
            (legacy / "config.toml").write_text(
                f'domain = "example-vm"\n[policy]\nmin_gib = 3\n'
                f'[runtime]\ndry_run = true\nstate_file = "{state}"\n'
                f'decision_log = "{audit}"\n'
            )
            plugin = base / "plugin"
            emhttp = base / "emhttp"
            pid_file = base / "api.pid"
            socket_path = base / "api.sock"
            log_file = base / "api.log"
            environment = os.environ.copy()
            environment.update(
                PATH=f"{fake_bin}:{environment['PATH']}",
                CRONTAB_STORE=str(cron_store),
                UPDATE_CRON=str(fake_bin / "update_cron"),
                PLUGIN_ROOT=str(plugin),
                PLUGIN_SOURCE=str(repository),
                POOL_ROOT=str(pool),
                LEGACY_ROOT=str(legacy),
                EMHTTP_ROOT=str(emhttp),
                API_PID_FILE=str(pid_file),
                API_LOG_FILE=str(log_file),
                API_SOCKET=str(socket_path),
            )
            lifecycle = repository / "unraid" / "lifecycle.sh"
            try:
                subprocess.run(["bash", str(lifecycle), "install"], check=True, env=environment)
                migrated = (plugin / "config.toml").read_text()
                self.assertIn('id = "example-vm"', migrated)
                self.assertIn(f'state_file = "{state}"', migrated)
                self.assertEqual((plugin / "config.toml").stat().st_mode & 0o777, 0o640)
                (plugin / "config.toml").write_text(migrated.replace("min_gib = 3", "min_gib = 7"))
                subprocess.run(["bash", str(lifecycle), "install"], check=True, env=environment)
                self.assertIn("min_gib = 7", (plugin / "config.toml").read_text())
                (plugin / "config.toml").write_text((plugin / "config.toml").read_text().replace("min_gib = 7", "min_gib = 9"))
                subprocess.run(["bash", str(lifecycle), "rollback"], check=True, env=environment)
                self.assertTrue(pid_file.exists(), log_file.read_text())
                self.assertIn("min_gib = 7", (plugin / "config.toml").read_text())
                deadline = time.monotonic() + 5
                while True:
                    try:
                        status, _ = unix_request(socket_path, "GET", "/api/config")
                        self.assertEqual(status, 200)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            self.fail(log_file.read_text())
                        time.sleep(0.05)
                self.assertTrue((plugin / "web_server.py").exists())
                self.assertEqual(socket_path.stat().st_mode & 0o777, 0o600)
                self.assertTrue((plugin / "lifecycle.sh").exists())
                self.assertTrue((emhttp / "libvirt-balloon-keeper.page").exists())
                fragment = (plugin / "libvirt-balloon-keeper.cron").read_text()
                self.assertEqual(fragment, f"* * * * * /usr/bin/bash {plugin / 'run-once.sh'}\n")
                self.assertEqual(cron_store.read_text(), fragment)
                status, _ = unix_request(socket_path, "GET", "/api/config")
                self.assertEqual(status, 200)
                subprocess.run(["bash", str(lifecycle), "uninstall"], check=True, env=environment)
                self.assertTrue((plugin / "config.toml").exists())
                self.assertFalse(state.parent.exists())
                self.assertFalse((base / "mnt" / "cache" / "appdata" / "libvirt-balloon-keeper").exists())
            finally:
                subprocess.run(["bash", str(lifecycle), "stop"], check=False, env=environment)
            self.assertFalse(pid_file.exists())
            self.assertFalse((plugin / "libvirt-balloon-keeper.cron").exists())
            self.assertNotIn("libvirt-balloon-keeper", cron_store.read_text())


if __name__ == "__main__":
    unittest.main()
