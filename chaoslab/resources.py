from __future__ import annotations

import time
from dataclasses import dataclass

import psutil


@dataclass(frozen=True)
class HostSnapshot:
    available_mb: float
    total_mb: float
    swap_used_mb: float
    cpu_percent: float
    load_1m: float

    def as_dict(self) -> dict:
        return {
            "available_memory_mb": round(self.available_mb, 1),
            "total_memory_mb": round(self.total_mb, 1),
            "swap_used_mb": round(self.swap_used_mb, 1),
            "cpu_percent": round(self.cpu_percent, 1),
            "load_1m": round(self.load_1m, 2),
        }


def host_snapshot(cpu_interval: float = 0.3) -> HostSnapshot:
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return HostSnapshot(
        available_mb=memory.available / 1048576,
        total_mb=memory.total / 1048576,
        swap_used_mb=swap.used / 1048576,
        cpu_percent=psutil.cpu_percent(interval=cpu_interval),
        load_1m=psutil.getloadavg()[0],
    )


def process_stats(pids: dict[str, int]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name, pid in pids.items():
        try:
            proc = psutil.Process(pid)
            with proc.oneshot():
                out[name] = {
                    "pid": pid,
                    "alive": proc.status() != psutil.STATUS_ZOMBIE,
                    "status": proc.status(),
                    "cpu_percent": proc.cpu_percent(interval=None),
                    "rss_mb": round(proc.memory_info().rss / 1048576, 1),
                }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            out[name] = {"pid": pid, "alive": False, "status": "gone", "cpu_percent": None, "rss_mb": None}
    return out


class ResourceGate:
    def __init__(self, resources_policy: dict):
        self.policy = resources_policy

    def check(self) -> tuple[bool, list[str], HostSnapshot]:
        snapshot = host_snapshot()
        reasons: list[str] = []
        floor = float(self.policy["min_available_memory_mb"])
        if snapshot.available_mb < floor:
            reasons.append(
                f"available memory {snapshot.available_mb:.0f} MB is below the safety threshold {floor:.0f} MB"
            )
        ceiling = float(self.policy.get("max_host_cpu_percent", 100))
        if snapshot.cpu_percent > ceiling:
            reasons.append(f"host CPU {snapshot.cpu_percent:.0f}% is above the safety ceiling {ceiling:.0f}%")
        return (not reasons, reasons, snapshot)



def gate_report(policy: dict) -> dict:
    allowed, reasons, snapshot = ResourceGate(policy["resources"]).check()
    return {"experiments_allowed": allowed, "reasons": reasons, "host": snapshot.as_dict(),
            "threshold_available_mb": policy["resources"]["min_available_memory_mb"]}

def monotonic() -> float:
    return time.monotonic()
