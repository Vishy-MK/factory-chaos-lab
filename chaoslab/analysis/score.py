"""Explainable resilience score. Deterministic, thresholds and weights from config/analysis.yaml.

overall = sum(weight_d * score_d) / sum(weight_d) over all dimensions, and only when every required dimension was
measured. A missing dimension makes the overall `insufficient_evidence`; its weight is never handed to others.
"""

from __future__ import annotations

import math


def _linear(value: float, full: float, zero: float) -> float:
    """100 at `full`, 0 at `zero`, linear between; works for either direction."""
    if full == zero:
        return 100.0 if value == full else 0.0
    fraction = (value - zero) / (full - zero)
    return round(max(0.0, min(1.0, fraction)) * 100, 1)


def _dimension(name: str, score: float | None, inputs: dict, rule: str, missing: str | None = None) -> dict:
    return {"name": name, "score": score, "measured": score is not None, "inputs": inputs, "rule": rule,
            **({"missing": missing} if missing else {})}


def compute(analysis: dict, config: dict) -> dict:
    rules = config["score"]
    windows = analysis["windows"]
    fault = (windows.get("fault") or {}).get("metrics") or {}
    base = (windows.get("baseline") or {}).get("metrics") or {}
    has_fault_window = bool((windows.get("fault") or {}).get("window", {}).get("samples"))
    dims = []

    # availability during the fault
    ratios = {m: (fault.get(m) or {}).get("value") for m in ("api_success_ratio", "authenticated_success_ratio")}
    measured = [v for v in ratios.values() if v is not None]
    if has_fault_window and measured:
        worst = min(measured)
        dims.append(_dimension("availability", _linear(worst, rules["availability"]["full"],
                                                       rules["availability"]["zero"]), ratios,
                               f"min(API, authenticated) success ratio during the fault: 100 at >= "
                               f"{rules['availability']['full']}, 0 at <= {rules['availability']['zero']}"))
    else:
        dims.append(_dimension("availability", None, ratios, "needs fault-window API measurements",
                               "no fault-active measurements of API success" if not has_fault_window else
                               "API success not measured during the fault"))

    # latency degradation
    lat, base_lat = fault.get("api_latency_ms"), base.get("api_latency_ms")
    if has_fault_window and lat and base_lat and base_lat["p95"]:
        ratio = lat["p95"] / base_lat["p95"]
        full, zero = rules["latency"]["full"], rules["latency"]["zero"]
        if ratio <= full:
            value = 100.0
        elif ratio >= zero:
            value = 0.0
        else:
            value = round((1 - (math.log(ratio) - math.log(full)) / (math.log(zero) - math.log(full))) * 100, 1)
        dims.append(_dimension("latency", value, {"baseline_p95_ms": base_lat["p95"], "fault_p95_ms": lat["p95"],
                                                  "ratio": round(ratio, 2)},
                               f"API p95 during / baseline p95: 100 at <= {full}x, 0 at >= {zero}x, log-linear"))
    else:
        dims.append(_dimension("latency", None, {}, "needs API latency in baseline and fault windows",
                               "API latency not measured in both windows"))

    # throughput / ingestion continuity
    rate, base_rate = fault.get("tag_ingest_per_second"), base.get("tag_ingest_per_second")
    if has_fault_window and rate and base_rate and base_rate.get("value"):
        fraction = (rate["value"] or 0) / base_rate["value"]
        value = _linear(fraction, rules["throughput"]["full"], rules["throughput"]["zero"])
        stall = (fault.get("tag_ingest_stall_max_s") or {}).get("value") or 0
        penalty = min(float(rules["throughput"]["stall_penalty_max"]), stall * float(rules["throughput"]["stall_penalty_per_s"]))
        dims.append(_dimension("throughput", round(max(0.0, value - penalty), 1),
                               {"ingest_fraction_of_baseline": round(fraction, 3), "longest_stall_s": stall,
                                "stall_penalty": round(penalty, 1)},
                               f"ingestion rate during / baseline: 100 at >= {rules['throughput']['full']}, 0 at <= "
                               f"{rules['throughput']['zero']}; minus {rules['throughput']['stall_penalty_per_s']} "
                               f"per stalled second (cap {rules['throughput']['stall_penalty_max']})"))
    else:
        dims.append(_dimension("throughput", None, {}, "needs ingestion in baseline and fault windows",
                               "ingestion not measured in both windows or zero baseline"))

    # recovery performance
    timing = analysis["recovery"].get("recovery_time") or {}
    if timing.get("available") and timing.get("recovered") is not None:
        seconds = timing.get("seconds_after_fault_end")
        value = 0.0 if not timing["recovered"] else _linear(seconds, rules["recovery"]["full_s"], rules["recovery"]["zero_s"])
        dims.append(_dimension("recovery", value, {"recovered": timing["recovered"], "seconds": seconds,
                                                   "criteria_unmeasured": timing.get("criteria_unmeasured")},
                               f"seconds from fault end to stable recovery: 100 at <= {rules['recovery']['full_s']} s, "
                               f"0 at >= {rules['recovery']['zero_s']} s or not recovered"))
    else:
        dims.append(_dimension("recovery", None, {}, "needs samples after a known fault window",
                               timing.get("reason") or "recovery not measurable"))

    # integrity and correctness
    evidence = {i["name"]: i for i in analysis["recovery"].get("evidence", [])}
    integrity = evidence.get("integrity_check")
    resets = ((windows.get("run") or {}).get("metrics") or {}).get("tag_id_resets")
    if integrity and integrity["ok"] is not None:
        value = 100.0 if integrity["ok"] else 0.0
        if resets and resets.get("value"):
            value = min(value, 50.0)
        dims.append(_dimension("integrity", value, {"integrity_check": integrity["ok"],
                                                    "tag_id_resets_after_fault": (resets or {}).get("value")},
                               "100 if the integrity check passed, 0 if it failed; at most 50 if the ingested tag id "
                               "went backwards. A pass is file consistency, not proof that no operation was lost"))
    else:
        dims.append(_dimension("integrity", None, {}, "needs a recorded integrity check",
                               "no integrity check recorded"))

    # cleanup / removal verification
    removal = evidence.get("removal_verified")
    if removal and removal["ok"] is not None:
        dims.append(_dimension("cleanup", 100.0 if removal["ok"] else 0.0, {"removal_verified": removal["ok"]},
                               "100 if the injecting system verified the fault was removed, 0 if not"))
    else:
        dims.append(_dimension("cleanup", None, {}, "needs a recorded removal check", "no removal check recorded"))

    weights = rules["weights"]
    required = list(rules["required"])
    missing_required = [d["name"] for d in dims if d["name"] in required and d["score"] is None]
    measured_dims = [d for d in dims if d["score"] is not None]
    coverage = round(sum(weights[d["name"]] for d in measured_dims) / sum(weights.values()), 3)
    attribution = analysis["inputs"]["experiment"]["fault"]["activation"]["status"]
    qualifications = []
    if analysis["baseline"]["readiness"] != "healthy":
        qualifications.append(f"baseline is {analysis['baseline']['readiness']}")
    if not analysis["compatibility"]["compatible"]:
        qualifications.append("baseline is not compatible with the run")
    if attribution in ("uncertain", "not_injected"):
        qualifications.append(f"fault attribution is {attribution}")
    if missing_required:
        overall = {"status": "insufficient_evidence", "score": None, "missing_dimensions": missing_required}
    elif qualifications and attribution in ("uncertain", "not_injected"):
        overall = {"status": "insufficient_evidence", "score": None, "missing_dimensions": [],
                   "reason": "the fault cannot be attributed to the window"}
    else:
        total = sum(weights[d["name"]] * d["score"] for d in dims) / sum(weights[d["name"]] for d in dims)
        overall = {"status": "qualified" if qualifications else "scored", "score": round(total, 1),
                   "missing_dimensions": []}
    return {
        "overall": overall,
        "dimensions": dims,
        "weights": weights,
        "required_dimensions": required,
        "formula": "sum(weight x dimension score) / sum(weights), over every dimension; only when all required "
                   "dimensions are measured",
        "evidence_coverage": coverage,
        "qualifications": qualifications,
        "scope": "resilience to this failure under the observed conditions; not a general resilience guarantee",
    }
