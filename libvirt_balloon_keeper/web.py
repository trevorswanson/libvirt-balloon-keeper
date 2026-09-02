"""Loopback-only JSON status/configuration surface for Unraid."""
from __future__ import annotations

import html
import json
import re
import time
import tomllib
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .adapter import LibvirtError, VirshAdapter
from .config import AppConfig, VMConfig, atomic_write_text, load_config, load_config_from_text, preserve_last_good_config
from .core import KIB_PER_GIB
from .runtime import load_state


def _vm_payload(vm: VMConfig, state=None, telemetry=None, capability=None, error=None) -> dict:
    p = vm.policy
    result = state.last_result if state else None
    swap_match = re.search(r"swap delta (\d+)", result or "")
    return {"id": vm.id, "domain": vm.domain, "configured": True, "enabled": vm.enabled,
            "dry_run": vm.dry_run, "interval_seconds": vm.interval_seconds,
            "min_kib": p.min_kib, "max_kib": p.max_kib, "step_kib": p.step_kib,
            "low_usable_percent": p.low_usable_percent, "high_usable_percent": p.high_usable_percent,
            "grow_samples": p.grow_samples, "shrink_samples": p.shrink_samples,
            "cooldown_seconds": p.cooldown_seconds, "stale_after_seconds": p.stale_after_seconds,
            "swap_activity_threshold": p.swap_activity_threshold,
            "state_file": str(vm.state_file), "decision_log": str(vm.decision_log),
            "last_success_epoch": state.last_success_epoch if state else None,
            "last_result": result, "swap_delta_kib": int(swap_match.group(1)) if swap_match else None,
            "low_samples": state.low_samples if state else 0,
            "high_samples": state.high_samples if state else 0,
            "telemetry": telemetry, "virtio_balloon": capability,
            "power_state": None, "error": error}


def status_payload(config: AppConfig, results: dict[str, str] | None = None, adapter=None) -> dict:
    results = results or {}
    vms = []
    for vm in config.vms:
        try:
            state = load_state(vm.state_file)
            last_result = state.last_result or results.get(vm.id)
            item = _vm_payload(vm, state=state)
            item["last_result"] = last_result
        except (OSError, ValueError):
            item = _vm_payload(vm, error="state unavailable")
            item["last_result"] = "error: state unavailable"
        vms.append(item)
    return {"version": config.version, "vms": vms}


def inventory_payload(config: AppConfig, adapter=None) -> dict:
    adapter = adapter or VirshAdapter()
    configured = {vm.domain: vm for vm in config.vms}
    try:
        domains = adapter.list_domains()
    except LibvirtError as exc:
        return {"version": config.version, "vms": [*status_payload(config)["vms"]], "inventory_error": str(exc)}
    items = []
    for domain in domains:
        vm = configured.get(domain)
        if vm is None:
            item = {"id": domain, "domain": domain, "configured": False, "enabled": False,
                    "dry_run": True, "interval_seconds": 60, "min_kib": 4 * KIB_PER_GIB,
                    "max_kib": 16 * KIB_PER_GIB, "step_kib": 512 * 1024,
                    "low_usable_percent": 20, "high_usable_percent": 60,
                    "grow_samples": 2, "shrink_samples": 20, "cooldown_seconds": 300,
                    "stale_after_seconds": 45, "swap_activity_threshold": 64 * 1024,
                    "state_file": f"/mnt/cache/appdata/libvirt-balloon-keeper/{domain}/state.json",
                    "decision_log": f"/mnt/cache/appdata/libvirt-balloon-keeper/{domain}/decisions.jsonl",
                    "virtio_balloon": None, "telemetry": None, "power_state": None, "error": None}
        else:
            item = status_payload(AppConfig(config.version, (vm,)))["vms"][0]
        try:
            item["power_state"] = adapter.domain_state(domain)
        except LibvirtError:
            item["power_state"] = None
        try:
            item["virtio_balloon"] = adapter.supports_virtio_balloon(domain)
        except LibvirtError:
            item["virtio_balloon"] = False
            item["error"] = "virtio-balloon capability unavailable"
        if item["power_state"] not in {"shut off", "shutoff", "off"}:
            try:
                telemetry = adapter.dommemstat(domain)
                item["telemetry"] = {"actual_kib": telemetry.actual, "available_kib": telemetry.available,
                                      "usable_kib": telemetry.usable, "last_update": telemetry.last_update,
                                      "swap_in_kib": telemetry.swap_in, "swap_out_kib": telemetry.swap_out}
                if vm is not None:
                    try:
                        state = load_state(vm.state_file)
                        if state.last_swap_in is not None and state.last_swap_out is not None:
                            item["swap_delta_kib"] = max(0, telemetry.swap_in - state.last_swap_in) + max(0, telemetry.swap_out - state.last_swap_out)
                    except (OSError, ValueError):
                        pass
            except LibvirtError:
                item["error"] = item.get("error") or "telemetry unavailable"
        items.append(item)
    for vm in config.vms:
        if vm.domain not in domains:
            item = status_payload(AppConfig(config.version, (vm,)))["vms"][0]
            item["error"] = "domain not found"
            items.append(item)
    return {"version": config.version, "vms": items, "inventory_error": None}


def _toml_string(value: str) -> str:
    return json.dumps(value)


def config_from_payload(payload: dict) -> str:
    if not isinstance(payload, dict) or not isinstance(payload.get("vms"), list) or not payload["vms"]:
        raise ValueError("configuration requires at least one VM")
    lines = ["version = 1", ""]
    for raw in payload["vms"]:
        if not isinstance(raw, dict):
            raise ValueError("each VM must be an object")
        for key in ("id", "domain"):
            if not isinstance(raw.get(key), str):
                raise ValueError(f"{key} must be a string")
        lines += ["[[vms]]", f"id = {_toml_string(raw['id'])}", f"domain = {_toml_string(raw['domain'])}"]
        for key in ("enabled", "dry_run"):
            if not isinstance(raw.get(key), bool): raise ValueError(f"{key} must be boolean")
            lines.append(f"{key} = {str(raw[key]).lower()}")
        fields = (("interval_seconds", "interval_seconds"), ("min_kib", "min_gib"), ("max_kib", "max_gib"),
                  ("step_kib", "step_mib"), ("low_usable_percent", "low_usable_percent"),
                  ("high_usable_percent", "high_usable_percent"), ("grow_samples", "grow_samples"),
                  ("shrink_samples", "shrink_samples"), ("cooldown_seconds", "cooldown_seconds"),
                  ("stale_after_seconds", "stale_after_seconds"), ("swap_activity_threshold", "swap_activity_threshold"))
        for source, target in fields:
            if source in raw:
                value = raw[source]
                if not isinstance(value, int) or isinstance(value, bool): raise ValueError(f"{source} must be integer")
                if target == "min_gib" or target == "max_gib": value = max(1, value // (1024 * 1024))
                if target == "step_mib": value = max(1, value // 1024)
                lines.append(f"{target} = {value}")
        for key in ("state_file", "decision_log"):
            if not isinstance(raw.get(key), str): raise ValueError(f"{key} must be a string")
            lines.append(f"{key} = {_toml_string(raw[key])}")
        lines.append("")
    return "\n".join(lines)


def read_audit(path: Path, limit: int = 50) -> list[dict]:
    if not 1 <= limit <= 100: raise ValueError("audit limit must be between 1 and 100")
    try:
        with path.open(encoding="utf-8") as handle: lines = deque(handle, maxlen=limit)
    except FileNotFoundError: return []
    except OSError as exc: raise ValueError("audit log unavailable") from exc
    entries = []
    for line in lines:
        try: entry = json.loads(line)
        except json.JSONDecodeError: continue
        if isinstance(entry, dict): entries.append(entry)
    return entries


def create_server(config_path: Path, host: str = "127.0.0.1", port: int = 0, adapter=None) -> ThreadingHTTPServer:
    config = load_config(config_path)
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body, content_type="application/json"):
            self.send_response(status); self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/config":
                try: self._send(200, config_path.read_bytes(), "text/plain; charset=utf-8")
                except OSError: self.send_error(503, "configuration unavailable")
            elif parsed.path in {"/api/status", "/api/inventory"}:
                payload = inventory_payload(config, adapter) if parsed.path.endswith("inventory") else status_payload(config, adapter=adapter)
                self._send(200, json.dumps(payload).encode())
            elif parsed.path == "/api/audit":
                query = parse_qs(parsed.query); ids = query.get("vm", [])
                selected = next((vm for vm in config.vms if vm.id in ids), None)
                if selected is None: self.send_error(404, "unknown VM"); return
                try: body = json.dumps({"vm": selected.id, "entries": read_audit(selected.decision_log, int(query.get("limit", ["50"])[0]))}).encode()
                except (ValueError, TypeError): self.send_error(400, "invalid audit limit"); return
                self._send(200, body)
            elif parsed.path == "/": self._send(200, ("<!doctype html><h1>Libvirt Balloon Keeper</h1><p>Use /api/inventory.</p>").encode(), "text/html; charset=utf-8")
            else: self.send_error(404)
        def do_POST(self):  # noqa: N802
            nonlocal config
            route = urlparse(self.path).path
            try: length = int(self.headers.get("Content-Length", "-1"))
            except ValueError: self.send_error(400, "invalid content length"); return
            if length < 0 or length > 256 * 1024: self.send_error(413); return
            raw = self.rfile.read(length)
            if route == "/api/validate":
                try:
                    if self.headers.get("Content-Type", "").startswith("application/json"):
                        load_config_from_json(raw)
                    else:
                        import tomllib; tomllib.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError, tomllib.TOMLDecodeError): self.send_error(400, "invalid configuration"); return
                self._send(200, b'{"valid": true}'); return
            if route == "/api/validate-configuration":
                try: load_config_from_json(raw)
                except (UnicodeDecodeError, ValueError, json.JSONDecodeError): self.send_error(400, "invalid configuration"); return
                self._send(200, b'{"valid": true}'); return
            if route == "/api/config":
                if self.headers.get("X-Confirm") != "apply": self.send_error(428, "send X-Confirm: apply to save configuration"); return
                try: new_config = load_config_from_text(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError): self.send_error(400, "configuration rejected"); return
                try: atomic_write_config(config_path, raw.decode("utf-8"))
                except OSError: self.send_error(400, "configuration rejected"); return
                config = new_config; self._send(200, b'{"saved": true}'); return
            if route != "/api/configuration" or self.headers.get("X-Confirm") != "apply": self.send_error(428 if route == "/api/configuration" else 404); return
            try:
                text = load_config_from_json(raw); new_config = load_config_from_text(text)
                atomic_write_config(config_path, text); config = new_config
            except (ValueError, OSError, json.JSONDecodeError): self.send_error(400, "configuration rejected"); return
            self._send(200, b'{"saved": true}')
        def log_message(self, format, *args): return
    if host not in {"127.0.0.1", "::1", "localhost"}: raise ValueError("status server must bind to loopback")
    return ThreadingHTTPServer((host, port), Handler)


def load_config_from_json(raw): return config_from_payload(json.loads(raw.decode("utf-8")))


def atomic_write_config(path, text):
    load_config_from_text(text)
    preserve_last_good_config(path)
    atomic_write_text(path, text)
