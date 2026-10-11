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
        # The plant API is probed on its own thread: a slow API (database latency, a frozen process) must not
        # make a scrape outlast Prometheus's timeout and lose the local metrics with it.
        self.api_interval_s = 2.0
        self.api_stale_s = 15.0
        self._api: tuple[float, list[tuple[str, str, list]]] | None = None
        self._api_lock = threading.Lock()
        self._api_thread: threading.Thread | None = None
        self._api_stop = threading.Event()

    def render(self) -> str:
        with self._lock:
            now = time.monotonic()
            if self._cache and now - self._cache[0] < self.cache_s:
                return self._cache[1]
            text = self._build()
            self._cache = (now, text)
            return text

    def _database(self, gauge, lines: list[str], plant: str) -> None:
        """The lab's database latency hook (chaoslab_dbhook) inside the plant's database clients."""
        from chaoslab.plant_names import chaoslab_dbhook

        rows = _database_rows(chaoslab_dbhook.read_stats(str(self.settings.db_hook_dir)), plant)
        if not rows:
            return
        control = chaoslab_dbhook.read_control(str(self.settings.db_hook_dir)) or {}
        gauge("chaoslab_db_delay_active", "1 while the database latency hook is delaying this component's statements",
              [({"plant": plant, "component": c}, 1.0 if r["active"] else 0.0) for c, r in rows.items()])
        if any(r["active"] for r in rows.values()) and isinstance(control.get("delay_ms"), int):
            gauge("chaoslab_db_delay_configured_seconds", "delay the active activation adds per statement",
                  [({"plant": plant}, control["delay_ms"] / 1000.0)])
        gauge("chaoslab_db_statements_total", "plant database statements executed, by kind (lab hook)",
              [({"plant": plant, "component": c, "kind": k}, float(v)) for c, r in rows.items()
               for k, v in r["statements"].items()], kind="counter")
        gauge("chaoslab_db_errors_total", "plant database errors seen by the lab hook",
              [({"plant": plant, "component": c}, float(r["errors"])) for c, r in rows.items()], kind="counter")
        gauge("chaoslab_db_lock_errors_total", "'database is locked' errors seen by the lab hook",
              [({"plant": plant, "component": c}, float(r["lock_errors"])) for c, r in rows.items()], kind="counter")
        gauge("chaoslab_db_statement_seconds_total", "time spent in statements, including any injected delay",
              [({"plant": plant, "component": c}, r["observed_ms"] / 1000.0) for c, r in rows.items()], kind="counter")
        gauge("chaoslab_db_driver_seconds_total", "time spent in the database driver, excluding injected delay",
              [({"plant": plant, "component": c}, r["driver_ms"] / 1000.0) for c, r in rows.items()], kind="counter")
        gauge("chaoslab_db_injected_statements_total", "statements the hook delayed",
              [({"plant": plant, "component": c}, float(r["injected"])) for c, r in rows.items()], kind="counter")
        gauge("chaoslab_db_injected_delay_seconds_total", "delay the hook added",
              [({"plant": plant, "component": c}, r["injected_ms"] / 1000.0) for c, r in rows.items()], kind="counter")
        name = "chaoslab_db_statement_duration_seconds"
        lines.append(f"# HELP {name} statement duration including injected delay (lab hook histogram)")
        lines.append(f"# TYPE {name} histogram")
        for component, r in rows.items():
            buckets, histogram = r["buckets"] or [], r["histogram"] or []
            running = 0
            for bound, count in zip(buckets, histogram):
                running += count
                lines.append(f'{name}_bucket{{plant="{_escape(plant)}",component="{_escape(component)}",'
                             f'le="{bound / 1000.0:g}"}} {running}')
            total = sum(histogram)
            lines.append(f'{name}_bucket{{plant="{_escape(plant)}",component="{_escape(component)}",le="+Inf"}} {total}')
            lines.append(f'{name}_sum{{plant="{_escape(plant)}",component="{_escape(component)}"}} {r["observed_ms"] / 1000.0:.15g}')
            lines.append(f'{name}_count{{plant="{_escape(plant)}",component="{_escape(component)}"}} {total}')

    def _cgroups(self, gauge, plant: str, table: dict[str, dict]) -> None:
        """cgroup v2 counters of components running in their lab scope (resource_limit)."""
        from chaoslab.chaos import cgroups

        scoped = {}
        for component, row in table.items():
            unit = cgroups.process_unit(row["pid"]) if row.get("pid") else None
            stats = cgroups.stats(unit) if unit else None
            if stats:
                scoped[component] = stats
        if not scoped:
            return

        def rows(getter):
            out = []
            for component, stats in scoped.items():
                value = getter(stats)
                if value is not None:
                    out.append(({"plant": plant, "component": component}, float(value)))
            return out

        def limit_bytes(stats, name):
            value = (stats["limits"] or {}).get(name)
            return None if value in (None, "max") else int(value)

        def quota(stats):
            value = (stats["limits"] or {}).get("cpu.max") or "max"
            parts = value.split()
            return None if parts[0] == "max" else int(parts[0]) / int(parts[1])

        gauge("chaoslab_cgroup_limited", "1 while any cgroup limit is set on the component's lab scope",
              rows(lambda s: 0.0 if cgroups.is_unlimited(s["limits"]) else 1.0))
        gauge("chaoslab_cgroup_memory_current_bytes", "memory.current of the component's lab scope",
              rows(lambda s: s["memory_current"]))
        gauge("chaoslab_cgroup_memory_peak_bytes", "memory.peak of the component's lab scope",
              rows(lambda s: s["memory_peak"]))
        gauge("chaoslab_cgroup_memory_high_bytes", "memory.high while set (absent when unlimited)",
              rows(lambda s: limit_bytes(s, "memory.high")))
        gauge("chaoslab_cgroup_memory_max_bytes", "memory.max while set (absent when unlimited)",
              rows(lambda s: limit_bytes(s, "memory.max")))
        gauge("chaoslab_cgroup_cpu_quota_cpus", "cpu.max quota in CPUs while set (absent when unlimited)",
              rows(quota))
        gauge("chaoslab_cgroup_cpu_usage_seconds_total", "CPU time used by the lab scope",
              rows(lambda s: None if s["cpu_usage_us"] is None else s["cpu_usage_us"] / 1e6), kind="counter")
        gauge("chaoslab_cgroup_cpu_periods_total", "CFS scheduler periods of the lab scope",
              rows(lambda s: s["cpu_nr_periods"]), kind="counter")
        gauge("chaoslab_cgroup_cpu_throttled_periods_total", "CFS periods in which the scope was throttled",
              rows(lambda s: s["cpu_nr_throttled"]), kind="counter")
        gauge("chaoslab_cgroup_cpu_throttled_seconds_total", "time the scope was throttled (summed over CPUs)",
              rows(lambda s: None if s["cpu_throttled_us"] is None else s["cpu_throttled_us"] / 1e6), kind="counter")
        gauge("chaoslab_cgroup_memory_events_total", "memory.events of the lab scope (high, max, oom_kill)",
              [({"plant": plant, "component": c, "event": e}, float(s["memory_events"].get(e, 0)))
               for c, s in scoped.items() for e in ("high", "max", "oom_kill")], kind="counter")
        gauge("chaoslab_cgroup_pressure_stall_seconds_total", "PSI 'some' stall time of the lab scope",
              [({"plant": plant, "component": c, "resource": r},
                ((s.get(f"{r}_pressure") or {}).get("some") or {}).get("total_us", 0) / 1e6)
               for c, s in scoped.items() for r in ("cpu", "memory") if s.get(f"{r}_pressure")], kind="counter")

    def _probe_api(self) -> list[tuple[str, str, list]]:
        """One round of plant API probes, as (metric, help, rows) for the scrape to render."""
        plant = self.settings.plant_name
        out: list[tuple[str, str, list]] = []
        health = self.plant.health(timeout=2.0)
        body = health.body if health.ok and isinstance(health.body, dict) else {}
        up = bool(health.ok and body.get("status") == "ok")
        out.append(("chaoslab_plant_up", "1 when the plant API answers /health with status ok",
                    [({"plant": plant}, 1.0 if up else 0.0)]))
        if health.ok:
            out.append(("chaoslab_api_health_latency_seconds", "latency of the last /health probe",
                        [({"plant": plant}, health.latency_s)]))
        watching = body.get("watching") or {}
        if watching:
            out.append(("chaoslab_machines_connected", "machines the MES can currently see",
                        [({"plant": plant}, float(watching.get("connected", 0)))]))
            out.append(("chaoslab_machines_total", "machines the MES is configured to watch",
                        [({"plant": plant}, float(watching.get("machines", 0)))]))
        floor = body.get("floor") or {}
        if floor.get("seconds_since") is not None:
            out.append(("chaoslab_floor_seconds_since_check", "seconds since the simulated floor recorded a quality "
                        "check", [({"plant": plant}, float(floor["seconds_since"]))]))
        if up:
            summary = self.plant.order_summary()
            if summary.ok and isinstance(summary.body, dict):
                if summary.body.get("good_qty") is not None:
                    out.append(("chaoslab_good_quantity", "good units booked across work orders",
                                [({"plant": plant}, float(summary.body.get("good_qty") or 0))]))
                if summary.body.get("scrap_qty") is not None:
                    out.append(("chaoslab_scrap_quantity", "scrap units booked across work orders",
                                [({"plant": plant}, float(summary.body.get("scrap_qty") or 0))]))
            states = self.plant.equipment_states()
            if states.ok and isinstance(states.body, list):
                out.append(("chaoslab_equipment_state_code",
                            "machine state code: 0 stopped, 1 running, 2 starved, 3 blocked, 4 down, 5 changeover",
                            [({"plant": plant, "equipment": row["equipment"]}, float(STATE_CODES[row["state"]]))
                             for row in states.body if row["state"] in STATE_CODES]))
        return out

    def _api_loop(self) -> None:
        while not self._api_stop.is_set():
            started = time.monotonic()
            try:
                probed = self._probe_api()
                with self._api_lock:
                    self._api = (time.monotonic(), probed)
            except Exception:
                pass
            self._api_stop.wait(max(0.0, self.api_interval_s - (time.monotonic() - started)))

    def _api_snapshot(self) -> tuple[float | None, list]:
        if self._api_thread is None:
            # The first scrape probes synchronously, so a fresh exporter never starts with nothing.
            try:
                self._api = (time.monotonic(), self._probe_api())
            except Exception:
                self._api = None
            self._api_thread = threading.Thread(target=self._api_loop, name="chaoslab-api-probe", daemon=True)
            self._api_thread.start()
        with self._api_lock:
            snapshot = self._api
        if snapshot is None:
            return None, []
        return time.monotonic() - snapshot[0], snapshot[1]

    def stop(self) -> None:
        self._api_stop.set()

    def _build(self) -> str:
        plant = self.settings.plant_name
        lines: list[str] = []

        def gauge(name: str, help_text: str, rows: list[tuple[dict, float]], kind: str = "gauge") -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {kind}")
            for labels, value in rows:
                if labels:
                    rendered = ",".join(f'{k}="{_escape(str(v))}"' for k, v in labels.items())
                    lines.append(f"{name}{{{rendered}}} {value:.15g}")
                else:
                    lines.append(f"{name} {value:.15g}")

        age, probed = self._api_snapshot()
        if age is None or age > self.api_stale_s:
            # No API answer for longer than api_stale_s: the API is not answering, whatever it last said.
            gauge("chaoslab_plant_up", "1 when the plant API answers /health with status ok",
                  [({"plant": plant}, 0.0)])
        else:
            for name, help_text, rows in probed:
                gauge(name, help_text, rows)
        if age is not None:
            gauge("chaoslab_api_probe_age_seconds", "age of the newest plant API probe in this scrape",
                  [({"plant": plant}, age)])

        table = self.plant.process_table()
        gauge("chaoslab_component_up", "1 when the plant component process is running",
              [({"plant": plant, "component": name}, 1.0 if row["alive"] else 0.0) for name, row in table.items()])
        gauge("chaoslab_component_rss_bytes", "resident memory of the plant component",
              [({"plant": plant, "component": name}, row["rss_mb"] * 1048576) for name, row in table.items()
               if row["rss_mb"] is not None])
        gauge("chaoslab_component_cpu_percent", "CPU percent of the plant component since the last scrape",
              [({"plant": plant, "component": name}, row["cpu_percent"]) for name, row in table.items()
               if row["cpu_percent"] is not None])

        self._database(gauge, lines, plant)
        self._cgroups(gauge, plant, table)

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


COMPONENT_BY_COMMAND = {"run-api": "api", "run-opc-agent": "opc-agent", "run-opc-sim": "opc-sim",
                        "run-operations": "floor"}


def _database_rows(stats: list[dict], plant: str) -> dict[str, dict]:
    """Cumulative hook counters summed per component (a restarted component's counters start again at zero)."""
    rows: dict[str, dict] = {}
    for row in stats:
        if row.get("plant") != plant:
            continue
        component = COMPONENT_BY_COMMAND.get(row.get("component"), row.get("component"))
        agg = rows.setdefault(component, {"statements": {}, "errors": 0, "lock_errors": 0, "observed_ms": 0.0,
                                          "driver_ms": 0.0, "injected": 0, "injected_ms": 0.0, "histogram": None,
                                          "active": False, "buckets": row.get("buckets_ms")})
        for kind, count in (row.get("statements") or {}).items():
            agg["statements"][kind] = agg["statements"].get(kind, 0) + count
        agg["errors"] += row.get("errors", 0)
        agg["lock_errors"] += row.get("lock_errors", 0)
        agg["observed_ms"] += row.get("observed_ms_total", 0.0)
        agg["driver_ms"] += row.get("db_ms_total", 0.0)
        agg["injected"] += (row.get("injected") or {}).get("statements", 0)
        agg["injected_ms"] += (row.get("injected") or {}).get("ms_total", 0.0)
        histogram = row.get("observed_histogram") or []
        agg["histogram"] = histogram if agg["histogram"] is None else [a + b for a, b in zip(agg["histogram"], histogram)]
        agg["active"] = agg["active"] or bool((row.get("control") or {}).get("active"))
    return rows


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
