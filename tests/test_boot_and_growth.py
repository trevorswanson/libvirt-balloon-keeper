import tempfile
import os
import shutil
import signal
import socket
import subprocess
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from balloon_keeper import load_config as load_compat_config
from libvirt_balloon_keeper.config import load_config
from libvirt_balloon_keeper.core import KIB_PER_GIB, PolicyConfig, State, Telemetry, decide
from libvirt_balloon_keeper.web import create_server, config_from_payload, status_payload, load_config_from_text


class BootAndGrowthTests(unittest.TestCase):
    def test_lifecycle_boot_install_reaches_real_unix_api_without_controller(self):
        repository = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, root = base / 'source', base / 'plugin'
            source.mkdir()
            root.mkdir()
            shutil.copytree(repository / 'libvirt_balloon_keeper', source / 'libvirt_balloon_keeper')
            shutil.copytree(repository / 'unraid', source / 'unraid')
            for name in ('balloon_keeper.py', 'web_server.py', 'config.example.toml'):
                shutil.copy(repository / name, source / name)
            # No host scheduler, at job, live state, or libvirt calls in this fixture.
            installer = source / 'unraid/install-cron.sh'
            installer.write_text('#!/bin/bash\nprintf scheduled > "$PLUGIN_ROOT/scheduled"\n')
            fake_bin = base / 'bin'
            fake_bin.mkdir()
            virsh = fake_bin / 'virsh'
            virsh.write_text('#!/bin/bash\nexit 99\n')
            virsh.chmod(0o750)
            (root / 'sitecustomize.py').write_text('import os.path\nos.path.ismount = lambda path: False\n')
            config = 'version=1\npool_root="/mnt/user"\n[[vms]]\nid="x"\ndomain="x"\ndry_run=true\n'
            (root / 'config.toml').write_text(config)
            socket_path, pid_file = base / 'api.sock', base / 'api.pid'
            env = dict(os.environ, PLUGIN_ROOT=str(root), PLUGIN_SOURCE=str(source),
                       EMHTTP_ROOT=str(base / 'emhttp'), API_SOCKET=str(socket_path),
                       API_PID_FILE=str(pid_file), API_LOG_FILE=str(base / 'api.log'),
                       PYTHONPATH=str(root), PATH=f"{fake_bin}:{os.environ['PATH']}")
            script = source / 'unraid/lifecycle.sh'
            try:
                result = subprocess.run(['bash', str(script), 'install'], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((root / 'config.toml').read_text(), config)
                self.assertTrue((root / 'scheduled').exists())
                deadline = time.monotonic() + 5
                while not socket_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(2)
                    client.connect(str(socket_path))
                    client.sendall(b'GET /api/status HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
                    self.assertIn(b'200', client.recv(4096).splitlines()[0])
                strict = subprocess.run(['bash', str(script), 'check'], env=env, capture_output=True, text=True)
                self.assertNotEqual(strict.returncode, 0)
                self.assertIn('mounted pool', strict.stderr)
            finally:
                if pid_file.exists():
                    os.kill(int(pid_file.read_text()), signal.SIGTERM)

    def test_boot_config_and_api_do_not_require_mounted_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text('version=1\npool_root="/mnt/user"\n[[vms]]\nid="x"\ndomain="x"\n')
            with patch('libvirt_balloon_keeper.unraid._mounted_directory', return_value=False):
                with self.assertRaisesRegex(ValueError, 'mounted pool'):
                    load_config(path)
                with self.assertRaisesRegex(ValueError, 'mounted pool'):
                    load_config_from_text(path.read_text())
                config = load_config(path, require_mounted_pool=False)
                self.assertEqual(config.pool_root, Path('/mnt/user'))
                self.assertTrue(config.vms[0].dry_run)
                server = create_server(path, port=0)
                server.server_close()
                for invalid in ('/boot', '/mnt/user/child', '/mnt/../boot'):
                    path.write_text(f'version=1\npool_root="{invalid}"\n[[vms]]\nid="x"\ndomain="x"\n')
                    with self.assertRaises(ValueError):
                        load_config(path, require_mounted_pool=False)

    def test_growth_cooldown_is_asymmetric_and_bounded(self):
        policy = PolicyConfig(grow_samples=1, shrink_samples=1,
                              cooldown_seconds=300, grow_cooldown_seconds=60)
        def decision(elapsed, usable, actual=8*KIB_PER_GIB, swap=0):
            return decide(policy, State(last_change_epoch=1000, last_swap_in=0, last_swap_out=0),
                          Telemetry(actual, actual, usable, 1000+elapsed, swap, 0), 1000+elapsed)
        self.assertIsNone(decision(59, 0)[1])
        self.assertEqual(decision(60, 0)[1], 8*KIB_PER_GIB + policy.step_kib)
        self.assertIsNone(decision(299, 7*KIB_PER_GIB)[1])
        self.assertEqual(decision(300, 7*KIB_PER_GIB)[1], 8*KIB_PER_GIB - policy.step_kib)
        self.assertEqual(decision(60, 4*KIB_PER_GIB, swap=65536)[1], 8*KIB_PER_GIB + policy.step_kib)
        self.assertIsNone(decision(60, 0, actual=policy.max_kib)[1])
        self.assertIsNone(decide(policy, State(), Telemetry(8*KIB_PER_GIB, 8*KIB_PER_GIB, 0, 1, 0, 0), 1060)[1])
        self.assertIsNone(decide(replace(policy, grow_cooldown_seconds=None), State(last_change_epoch=1000),
                                 Telemetry(8*KIB_PER_GIB, 8*KIB_PER_GIB, 0, 1060, 0, 0), 1060)[1])

    def test_growth_config_validation_compatibility_and_api_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            base = 'version=1\n[[vms]]\nid="x"\ndomain="x"\n'
            path.write_text(base + 'grow_cooldown_seconds=60\n')
            config = load_config(path)
            self.assertEqual(load_compat_config(path).grow_cooldown_seconds, 60)
            payload = status_payload(config)
            self.assertEqual(payload['vms'][0]['grow_cooldown_seconds'], 60)
            path.write_text(config_from_payload(payload))
            self.assertEqual(load_config(path).vms[0].policy.grow_cooldown_seconds, 60)
            for value in ('0', '-1', '604801', 'true', '"invalid"'):
                path.write_text(base + f'grow_cooldown_seconds={value}\n')
                with self.assertRaises(ValueError):
                    load_config(path)
            path.write_text(base)
            self.assertIsNone(load_config(path).vms[0].policy.grow_cooldown_seconds)
            path.write_text(config_from_payload(status_payload(load_config(path))))
            self.assertIsNone(load_config(path).vms[0].policy.grow_cooldown_seconds)


if __name__ == '__main__':
    unittest.main()
