from __future__ import annotations

import threading
import time
from pathlib import Path

import yaml

from chaoslab.adapters.factorysemantics import FactorySemanticsAdapter
from chaoslab.settings import ConfigError, Settings

PROFILE_DIR = Path(__file__).resolve().parent / "profiles"


def available_profiles(settings: Settings | None = None) -> list[str]:
    return sorted(p.stem for p in PROFILE_DIR.glob("*.yaml"))


def load_profile(settings: Settings | None, name: str) -> dict:
    path = PROFILE_DIR / f"{name}.yaml"
    if not path.is_file():
        raise ConfigError(f"unknown workload profile {name!r}; available: {available_profiles()}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data["name"] = name
    return data


class WorkloadGenerator:
    def __init__(self, plant: FactorySemanticsAdapter, profile: dict, max_requests_per_second: float):
        self.plant = plant
        self.profile = profile
        requested = float(profile.get("requests_per_second", 0))
        self.rate = min(requested, float(max_requests_per_second))
        self.operations = list(profile.get("operations", []))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.counts: dict[str, dict] = {}

    def _call(self, operation: str):
        mapping = {
            "order_summary": self.plant.order_summary,
            "equipment_states": self.plant.equipment_states,
            "orders_kpi": self.plant.orders,
            "connections": self.plant.equipment_connections,
            "oee_fill": lambda: self.plant.oee("FILL01", 0.1),
            "downtime": self.plant.downtime,
            "health": self.plant.health,
        }
        if operation not in mapping:
            raise ConfigError(f"workload operation {operation!r} is not supported")
        return mapping[operation]()

    def start(self) -> None:
        if self.rate <= 0 or not self.operations:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="chaoslab-workload", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        interval = 1.0 / self.rate
        index = 0
        next_at = time.monotonic()
        while not self._stop.is_set():
            operation = self.operations[index % len(self.operations)]
            index += 1
            result = self._call(operation)
            with self._lock:
                row = self.counts.setdefault(
                    operation, {"requests": 0, "errors": 0, "latency_ms_sum": 0.0, "latency_ms_max": 0.0})
                row["requests"] += 1
                if not result.ok:
                    row["errors"] += 1
                else:
                    ms = result.latency_s * 1000
                    row["latency_ms_sum"] += ms
                    row["latency_ms_max"] = max(row["latency_ms_max"], ms)
            next_at += interval
            delay = next_at - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
            else:
                next_at = time.monotonic()

    def stop(self) -> dict:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        return self.report()

    def report(self) -> dict:
        with self._lock:
            operations = {}
            total = errors = 0
            for name, row in self.counts.items():
                good = row["requests"] - row["errors"]
                operations[name] = {
                    "requests": row["requests"],
                    "errors": row["errors"],
                    "mean_latency_ms": round(row["latency_ms_sum"] / good, 1) if good else None,
                    "max_latency_ms": round(row["latency_ms_max"], 1) if good else None,
                }
                total += row["requests"]
                errors += row["errors"]
            return {
                "profile": self.profile.get("name"),
                "description": self.profile.get("description"),
                "configured_requests_per_second": self.profile.get("requests_per_second"),
                "effective_requests_per_second": self.rate,
                "operations_cycle": self.operations,
                "total_requests": total,
                "total_errors": errors,
                "per_operation": operations,
            }
