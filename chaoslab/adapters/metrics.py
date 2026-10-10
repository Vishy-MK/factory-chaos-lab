from __future__ import annotations

import statistics
import threading
import time
from typing import Any

from chaoslab import resources
from chaoslab.adapters.factorysemantics import FactorySemanticsAdapter, parse_prometheus


class MetricsAdapter:
    def __init__(self, plant: FactorySemanticsAdapter):
        self.plant = plant
        self.workload_counters: dict[str, Any] = {}

    def sample(self, phase: str = "") -> dict:
        wall = time.time()
        sample: dict[str, Any] = {"t": wall, "phase": phase}
        health = self.plant.health(timeout=2.0)
        body = health.body if health.ok and isinstance(health.body, dict) else {}
        sample["api"] = {
            "up": bool(health.ok and body.get("status") == "ok"),
            "latency_ms": round(health.latency_s * 1000, 1) if health.ok else None,
            "status": health.status,
            "error": health.error,
        }
        watching = body.get("watching") if body else None
        sample["machines"] = {
            "total": watching.get("machines") if watching else None,
            "connected": watching.get("connected") if watching else None,
            "disconnected": watching.get("disconnected") if watching else None,
            "unknown": watching.get("unknown") if watching else None,
        }
        floor = body.get("floor") if body else None
        sample["floor_seconds_since_check"] = floor.get("seconds_since") if floor else None

        sample["authenticated_ok"] = None
        sample["work_orders"] = {"good_qty": None, "scrap_qty": None, "running": None}
        sample["equipment_states"] = None
        sample["tag_values_max_id"] = None
        sample["audit_entries_max_id"] = None

        if sample["api"]["up"]:
            summary = self.plant.order_summary()
            sample["authenticated_ok"] = bool(summary.ok)
            if summary.ok and isinstance(summary.body, dict):
                by_status = summary.body.get("by_status") or {}
                sample["work_orders"] = {
                    "good_qty": summary.body.get("good_qty"),
                    "scrap_qty": summary.body.get("scrap_qty"),
                    "running": by_status.get("running"),
                }
            states = self.plant.equipment_states()
            if states.ok and isinstance(states.body, list):
                sample["equipment_states"] = {row["equipment"]: row["state"] for row in states.body}
            metrics = self.plant.metrics_text()
            if metrics.ok and isinstance(metrics.body, str):
                for name, _labels, value in parse_prometheus(metrics.body):
                    if name == "mes_tag_values_max_id":
                        sample["tag_values_max_id"] = int(value)
                    elif name == "mes_audit_entries_max_id":
                        sample["audit_entries_max_id"] = int(value)

        table = self.plant.process_table()
        sample["components"] = {
            name: {"alive": row["alive"], "status": row["status"], "rss_mb": row["rss_mb"],
                   "cpu_percent": row["cpu_percent"]}
            for name, row in table.items()
        }
        host = resources.host_snapshot(cpu_interval=0.0)
        sample["host"] = host.as_dict()
        return sample

    def summarise(self, samples: list[dict]) -> dict:
        if not samples:
            return {"samples": 0}
        up = [s for s in samples if s["api"]["up"]]
        latencies = [s["api"]["latency_ms"] for s in samples if s["api"]["latency_ms"] is not None]
        probed = [s for s in samples if s["authenticated_ok"] is not None]
        connected = [s["machines"]["connected"] for s in samples if s["machines"]["connected"] is not None]
        floor_age = [s["floor_seconds_since_check"] for s in samples if s["floor_seconds_since_check"] is not None]
        ids = [(s["t"], s["tag_values_max_id"]) for s in samples if s["tag_values_max_id"] is not None]
        good = [s["work_orders"]["good_qty"] for s in samples if s["work_orders"]["good_qty"] is not None]
        scrap = [s["work_orders"]["scrap_qty"] for s in samples if s["work_orders"]["scrap_qty"] is not None]
        rss = {}
        for name in samples[0]["components"]:
            values = [s["components"][name]["rss_mb"] for s in samples
                      if name in s["components"] and s["components"][name]["rss_mb"] is not None]
            rss[name] = round(max(values), 1) if values else None
        alive_ratio = {}
        for name in samples[0]["components"]:
            flags = [bool(s["components"].get(name, {}).get("alive")) for s in samples]
            alive_ratio[name] = round(sum(flags) / len(flags), 3)
        ingest = None
        if len(ids) >= 2 and ids[-1][0] > ids[0][0]:
            ingest = round((ids[-1][1] - ids[0][1]) / (ids[-1][0] - ids[0][0]), 3)
        return {
            "samples": len(samples),
            "duration_s": round(samples[-1]["t"] - samples[0]["t"], 2),
            "api_success_ratio": round(len(up) / len(samples), 3),
            "authenticated_success_ratio": (
                round(sum(1 for s in probed if s["authenticated_ok"]) / len(probed), 3) if probed else None
            ),
            "api_latency_ms": _distribution(latencies),
            "machines_connected_min": min(connected) if connected else None,
            "machines_total": next((s["machines"]["total"] for s in samples if s["machines"]["total"]), None),
            "floor_seconds_since_check_max": round(max(floor_age), 1) if floor_age else None,
            "tag_ingest_per_second": ingest,
            "tag_values_max_id_start": ids[0][1] if ids else None,
            "tag_values_max_id_end": ids[-1][1] if ids else None,
            "good_qty_end": good[-1] if good else None,
            "scrap_qty_end": scrap[-1] if scrap else None,
            "component_alive_ratio": alive_ratio,
            "component_rss_mb_max": rss,
            "host_available_memory_mb_min": round(min(s["host"]["available_memory_mb"] for s in samples), 1),
            "host_cpu_percent_max": round(max(s["host"]["cpu_percent"] for s in samples), 1),
        }


def _distribution(values: list[float]) -> dict | None:
    if not values:
        return None
    ordered = sorted(values)
    p95_index = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
    return {
        "count": len(ordered),
        "min": round(ordered[0], 1),
        "p50": round(statistics.median(ordered), 1),
        "p95": round(ordered[p95_index], 1),
        "max": round(ordered[-1], 1),
    }


class Sampler:
    def __init__(self, metrics: MetricsAdapter, interval_s: float):
        self.metrics = metrics
        self.interval_s = interval_s
        self.samples: list[dict] = []
        self.phase = "idle"
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def set_phase(self, phase: str) -> None:
        with self._lock:
            self.phase = phase

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="chaoslab-sampler", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            with self._lock:
                phase = self.phase
            sample = self.metrics.sample(phase)
            with self._lock:
                if self.phase != phase:
                    sample["phase"] = "transition"
                    sample["phase_before"] = phase
                    sample["phase_after"] = self.phase
                self.samples.append(sample)
            remaining = self.interval_s - (time.monotonic() - started)
            if remaining > 0:
                self._stop.wait(remaining)

    def stop(self) -> list[dict]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=15)
        with self._lock:
            return list(self.samples)

    def by_phase(self, phase: str) -> list[dict]:
        with self._lock:
            return [s for s in self.samples if s["phase"] == phase]

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self.samples)


class HostSampler:
    def __init__(self, interval_s: float = 2.0):
        self.interval_s = interval_s
        self.samples: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        import psutil

        psutil.cpu_percent(interval=None)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="chaoslab-host-sampler", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        import psutil

        while not self._stop.is_set():
            rss = 0.0
            processes = 0
            for proc in psutil.process_iter(["cmdline", "memory_info"]):
                try:
                    cmdline = proc.info["cmdline"] or []
                    if len(cmdline) >= 3 and cmdline[1].endswith("/fsmes"):
                        rss += proc.info["memory_info"].rss / 1048576
                        processes += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            host = resources.host_snapshot(cpu_interval=0.0)
            self.samples.append({"t": time.time(), "host": host.as_dict(), "fsmes_processes": processes,
                                 "fsmes_rss_mb": round(rss, 1)})
            self._stop.wait(self.interval_s)

    def stop(self) -> list[dict]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        return list(self.samples)

    @staticmethod
    def summarise(samples: list[dict]) -> dict:
        if not samples:
            return {"samples": 0}
        return {
            "samples": len(samples),
            "host_available_memory_mb_min": round(min(s["host"]["available_memory_mb"] for s in samples), 1),
            "host_cpu_percent_mean": round(statistics.fmean(s["host"]["cpu_percent"] for s in samples), 1),
            "host_cpu_percent_max": round(max(s["host"]["cpu_percent"] for s in samples), 1),
            "fsmes_rss_mb_max": round(max(s["fsmes_rss_mb"] for s in samples), 1),
            "fsmes_processes_max": max(s["fsmes_processes"] for s in samples),
        }
