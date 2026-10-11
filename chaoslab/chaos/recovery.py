from __future__ import annotations

import time
from typing import Callable

from chaoslab.adapters.factorysemantics import FactorySemanticsAdapter

STAGES = ("fault_removed", "service_available", "operation_succeeds", "manufacturing_behaviour",
          "data_consistency")


class RecoveryVerifier:
    def __init__(self, plant: FactorySemanticsAdapter, components: list[str], floor_age_limit_s: float = 45.0,
                 stable_samples: int = 3, ingest_window_s: float = 3.0):
        self.plant = plant
        self.components = components
        self.floor_age_limit_s = floor_age_limit_s
        self.stable_samples = stable_samples
        self.ingest_window_s = ingest_window_s

    def evaluate_sample(self, sample: dict, reference_ids: list[tuple[float, int]],
                        baseline_max_id: int | None) -> dict[str, dict]:
        stages: dict[str, dict] = {}
        api_up = bool(sample["api"]["up"])
        components_alive = {c: bool(sample["components"].get(c, {}).get("alive")) for c in self.components}
        stages["service_available"] = {
            "ok": api_up and all(components_alive.values()),
            "detail": f"api up: {api_up}; components alive: {components_alive}",
        }
        auth = sample["authenticated_ok"]
        stages["operation_succeeds"] = {
            "ok": auth is True,
            "detail": "authenticated work-order summary request succeeded" if auth else
                      f"authenticated request result: {'unavailable' if auth is None else 'failed'}",
        }
        machines = sample["machines"]
        connected_ok = machines["total"] is not None and machines["connected"] == machines["total"]
        age = sample["floor_seconds_since_check"]
        # A deployment without a floor simulator (the Docker stack) has nothing to judge there.
        floor_ok = "floor" not in self.components or (age is not None and age <= self.floor_age_limit_s)
        current_id = sample["tag_values_max_id"]
        advancing = False
        advance_detail = "no tag id available"
        if current_id is not None:
            older = [(t, i) for t, i in reference_ids if sample["t"] - t >= self.ingest_window_s]
            if older:
                t0, i0 = older[-1]
                advancing = current_id > i0
                advance_detail = f"tag id {i0} -> {current_id} over {sample['t'] - t0:.1f}s"
            else:
                advance_detail = "not enough history yet"
        stages["manufacturing_behaviour"] = {
            "ok": connected_ok and floor_ok and advancing,
            "detail": (f"machines connected {machines['connected']}/{machines['total']}; floor last recorded "
                       f"{age}s ago (limit {self.floor_age_limit_s:g}s); {advance_detail}"),
        }
        stages["_ids_monotonic"] = {
            "ok": current_id is None or baseline_max_id is None or current_id >= baseline_max_id,
            "detail": f"tag id {current_id} vs baseline max {baseline_max_id}",
        }
        return stages

    def wait(self, samples_source: Callable[[], list[dict]], fault_removed: dict, timeout_s: float,
             baseline_max_id: int | None, started_monotonic: float | None = None,
             should_stop: Callable[[], bool] = lambda: False) -> dict:
        origin = started_monotonic or time.monotonic()
        wall_origin = time.time()
        deadline = origin + timeout_s
        seen_t: float = 0.0
        consecutive = 0
        first_ok: dict[str, float | None] = {s: None for s in ("service_available", "operation_succeeds",
                                                                  "manufacturing_behaviour")}
        timeline: list[dict] = []
        run_start: float | None = None
        last_stages: dict[str, dict] = {}
        recovered_at: float | None = None
        while time.monotonic() < deadline:
            if should_stop():
                break
            snapshot = samples_source()
            fresh = [s for s in snapshot if s["t"] > seen_t and s["t"] >= wall_origin]
            if not fresh:
                time.sleep(0.25)
                continue
            for sample in fresh:
                seen_t = sample["t"]
                reference = [(s["t"], s["tag_values_max_id"]) for s in snapshot
                             if s["tag_values_max_id"] is not None and s["t"] < sample["t"]]
                stages = self.evaluate_sample(sample, reference, baseline_max_id)
                last_stages = stages
                offset = sample["t"] - wall_origin
                for stage in first_ok:
                    if stages[stage]["ok"] and first_ok[stage] is None:
                        first_ok[stage] = round(offset, 2)
                ok_now = all(stages[s]["ok"] for s in ("service_available", "operation_succeeds",
                                                        "manufacturing_behaviour")) and fault_removed["ok"]
                timeline.append({"offset_s": round(offset, 2), "service_available": stages["service_available"]["ok"],
                                 "operation_succeeds": stages["operation_succeeds"]["ok"],
                                 "manufacturing_behaviour": stages["manufacturing_behaviour"]["ok"]})
                if ok_now:
                    if consecutive == 0:
                        run_start = offset
                    consecutive += 1
                else:
                    consecutive = 0
                    run_start = None
                if consecutive >= self.stable_samples:
                    recovered_at = round(run_start, 2)
                    break
            if recovered_at is not None:
                break
            time.sleep(0.25)

        integrity_ok, integrity_detail = self.plant.database_integrity()
        monotonic_ok = last_stages.get("_ids_monotonic", {"ok": None, "detail": "no samples"})
        stages_out = {
            "fault_removed": {"ok": bool(fault_removed["ok"]), "detail": fault_removed.get("detail", "")},
            "service_available": last_stages.get("service_available", {"ok": False, "detail": "no samples"}),
            "operation_succeeds": last_stages.get("operation_succeeds", {"ok": False, "detail": "no samples"}),
            "manufacturing_behaviour": last_stages.get("manufacturing_behaviour",
                                                        {"ok": False, "detail": "no samples"}),
            "data_consistency": {
                "ok": bool(integrity_ok) and bool(monotonic_ok["ok"]),
                "detail": f"sqlite quick_check: {integrity_detail}; tag id monotonic: {monotonic_ok['detail']}",
                "integrity_available": integrity_ok is not None,
            },
        }
        recovered = recovered_at is not None and stages_out["data_consistency"]["ok"]
        return {
            "recovered": recovered,
            "duration_s": recovered_at,
            "timeout_s": timeout_s,
            "stable_samples_required": self.stable_samples,
            "first_ok_after_cleanup_s": first_ok,
            "stages": stages_out,
            "timeline": timeline,
        }
