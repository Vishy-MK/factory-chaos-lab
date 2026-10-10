from __future__ import annotations

import json
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from chaoslab import resources, settings as settings_module
from chaoslab.adapters.factorysemantics import FactorySemanticsAdapter
from chaoslab.audit import Lease
from chaoslab.store import ResultStore

STATE_CODES = {"running": 1, "stopped": 0, "starved": 2, "blocked": 3, "down": 4, "changeover": 5}


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class Collector:
    def __init__(self, settings: settings_module.Settings):
        self.settings = settings
        self.plant = FactorySemanticsAdapter(settings)
        self.lease = Lease(settings.lease_file, settings.stop_flag)
        self.store = ResultStore(settings.results_dir)
        self._lock = threading.Lock()
        self._cache: tuple[float, str] | None = None
        self.cache_s = float(settings.lab["exporter"].get("cache_s", 1.0))

    def render(self) -> str:
        with self._lock:
            now = time.monotonic()
            if self._cache and now - self._cache[0] < self.cache_s:
                return self._cache[1]
            text = self._build()
            self._cache = (now, text)
            return text

    def _build(self) -> str:
        plant = self.settings.plant_name
        lines: list[str] = []

        def gauge(name: str, help_text: str, rows: list[tuple[dict, float]]) -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            for labels, value in rows:
                if labels:
                    rendered = ",".join(f'{k}="{_escape(str(v))}"' for k, v in labels.items())
                    lines.append(f"{name}{{{rendered}}} {value:g}")
                else:
                    lines.append(f"{name} {value:g}")

        health = self.plant.health(timeout=2.0)
        body = health.body if health.ok and isinstance(health.body, dict) else {}
        up = bool(health.ok and body.get("status") == "ok")
        gauge("chaoslab_plant_up", "1 when the plant API answers /health with status ok",
              [({"plant": plant}, 1.0 if up else 0.0)])
        if health.ok:
            gauge("chaoslab_api_health_latency_seconds", "latency of the last /health probe",
                  [({"plant": plant}, health.latency_s)])
        watching = body.get("watching") or {}
        if watching:
            gauge("chaoslab_machines_connected", "machines the MES can currently see",
                  [({"plant": plant}, float(watching.get("connected", 0)))])
            gauge("chaoslab_machines_total", "machines the MES is configured to watch",
                  [({"plant": plant}, float(watching.get("machines", 0)))])
        floor = body.get("floor") or {}
        if floor.get("seconds_since") is not None:
            gauge("chaoslab_floor_seconds_since_check", "seconds since the simulated floor recorded a quality check",
                  [({"plant": plant}, float(floor["seconds_since"]))])

        if up:
            summary = self.plant.order_summary()
            if summary.ok and isinstance(summary.body, dict):
                rows_good = [({"plant": plant}, float(summary.body.get("good_qty") or 0))]
                rows_scrap = [({"plant": plant}, float(summary.body.get("scrap_qty") or 0))]
                if summary.body.get("good_qty") is not None:
                    gauge("chaoslab_good_quantity", "good units booked across work orders", rows_good)
                if summary.body.get("scrap_qty") is not None:
                    gauge("chaoslab_scrap_quantity", "scrap units booked across work orders", rows_scrap)
            states = self.plant.equipment_states()
            if states.ok and isinstance(states.body, list):
                gauge("chaoslab_equipment_state_code",
                      "machine state code: 0 stopped, 1 running, 2 starved, 3 blocked, 4 down, 5 changeover",
                      [({"plant": plant, "equipment": row["equipment"]}, float(STATE_CODES[row["state"]]))
                       for row in states.body if row["state"] in STATE_CODES])

        table = self.plant.process_table()
        gauge("chaoslab_component_up", "1 when the plant component process is running",
              [({"plant": plant, "component": name}, 1.0 if row["alive"] else 0.0) for name, row in table.items()])
        gauge("chaoslab_component_rss_bytes", "resident memory of the plant component",
              [({"plant": plant, "component": name}, row["rss_mb"] * 1048576) for name, row in table.items()
               if row["rss_mb"] is not None])
        gauge("chaoslab_component_cpu_percent", "CPU percent of the plant component since the last scrape",
              [({"plant": plant, "component": name}, row["cpu_percent"]) for name, row in table.items()
               if row["cpu_percent"] is not None])

        host = resources.host_snapshot(cpu_interval=0.0)
        gauge("chaoslab_host_memory_available_bytes", "host MemAvailable", [({}, host.available_mb * 1048576)])
        gauge("chaoslab_host_cpu_percent", "host CPU percent", [({}, host.cpu_percent)])
        gauge("chaoslab_host_swap_used_bytes", "host swap in use", [({}, host.swap_used_mb * 1048576)])

        active = self.lease.active()
        gauge("chaoslab_experiment_active", "1 while an experiment holds the lease",
              [({}, 1.0 if active else 0.0)])
        if active:
            gauge("chaoslab_experiment_info", "the active experiment", [(
                {"name": active.get("name", ""), "state": active.get("state", "")}, 1.0)])
            gauge("chaoslab_experiment_start_timestamp_seconds", "start of the active experiment",
                  [({}, float(active.get("started", 0)))])

        latest = self.store.latest()
        if latest:
            try:
                result = self.store.read_result(latest)
            except (OSError, json.JSONDecodeError):
                result = {}
            name = (result.get("definition") or {}).get("name", "")
            overall = result.get("overall_result", "")
            gauge("chaoslab_last_experiment_info", "the most recent finished experiment",
                  [({"name": name, "result": overall}, 1.0)])
            recovery = result.get("recovery") or {}
            if recovery.get("duration_s") is not None:
                gauge("chaoslab_last_experiment_recovery_seconds", "recovery duration of the last experiment",
                      [({"name": name}, float(recovery["duration_s"]))])
            gauge("chaoslab_last_experiment_recovered", "1 when the last experiment fully recovered",
                  [({"name": name}, 1.0 if overall == "recovered" else 0.0)])
            gauge("chaoslab_last_experiment_cleanup_ok", "1 when the last experiment's cleanup was verified",
                  [({"name": name}, 1.0 if (result.get("cleanup") or {}).get("ok") else 0.0)])
        gauge("chaoslab_experiments_recorded", "number of experiment results on disk",
              [({}, float(len(self.store.list_runs())))])
        gauge("chaoslab_exporter_scrape_timestamp_seconds", "time of this scrape", [({}, time.time())])
        return "\n".join(lines) + "\n"


def make_handler(collector: Collector):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] == "/metrics":
                try:
                    payload = collector.render().encode()
                    status = 200
                except Exception as exc:
                    payload = f"# exporter error: {type(exc).__name__}\n".encode()
                    status = 500
            elif self.path == "/healthz":
                payload, status = b"ok\n", 200
            else:
                payload, status = b"not found\n", 404
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            return

    return Handler


def serve(root: Path | None = None) -> int:
    settings = settings_module.load(root)
    exporter = settings.lab["exporter"]
    host = exporter["host"]
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise settings_module.ConfigError("the exporter may only bind to a loopback address")
    collector = Collector(settings)
    server = ThreadingHTTPServer((host, int(exporter["port"])), make_handler(collector))
    server.daemon_threads = True

    def stop(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    server.serve_forever()
    server.server_close()
    return 0
