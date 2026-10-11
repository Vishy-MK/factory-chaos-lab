"""Baseline assessment: is this pre-failure window a healthy, adequately measured picture of normal operation?"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from chaoslab.analysis import evidence as evidence_module
from chaoslab.analysis.measure import METRICS, window_metrics
from chaoslab.settings import Settings

MAX_OBSERVE_S = 120


def baseline_samples(evidence: evidence_module.Evidence) -> tuple[list[dict], dict, str]:
    """The baseline window of a run. A no-fault run (baseline-observe) is baseline throughout."""
    if evidence.fault.get("type") in (None, "none"):
        samples = [s for s in evidence.samples if s.get("label") != "transition"]
        basis = "every sample of a run without a fault (transitions excluded)"
        window = {"start": samples[0]["t"] if samples else None, "end": samples[-1]["t"] if samples else None,
                  "basis": basis}
        return samples, window, basis
    window = evidence.windows.get("baseline") or {}
    return evidence.window_samples("baseline"), window, window.get("basis", "baseline window")


def assess(evidence: evidence_module.Evidence, config: dict) -> dict:
    samples, window, basis = baseline_samples(evidence)
    measured = window_metrics(samples, config, window.get("start"), window.get("end"))
    rules = config["baseline"]
    m = measured["metrics"]
    checks = []

    def check(name, ok, detail, ref=None):
        checks.append({"name": name, "ok": ok, "detail": detail, **({"ref": ref} if ref else {})})

    duration = measured["window"].get("observed_span_s") or 0
    check("sample_count", measured["window"]["adequate"],
          f"{measured['window']['samples']} samples (minimum {config['measurement']['min_samples_per_window']})")
    check("duration", duration >= float(rules["min_duration_s"]),
          f"{duration} s observed (minimum {rules['min_duration_s']} s)")
    for metric, floor in (("api_success_ratio", rules["min_api_success_ratio"]),
                          ("authenticated_success_ratio", rules["min_authenticated_success_ratio"])):
        value = (m.get(metric) or {}).get("value")
        check(metric, None if value is None else value >= float(floor),
              "not measured" if value is None else f"{value} (minimum {floor})")
    connected = m.get("machines_all_connected_ratio")
    if rules.get("require_all_machines_connected"):
        check("machines_connected", None if connected is None else connected["value"] == 1.0,
              "not measured" if connected is None else
              f"every machine connected in {connected['value'] * 100:.0f}% of samples")
    rate, stall = m.get("tag_ingest_per_second"), m.get("tag_ingest_stall_max_s")
    check("ingestion_advancing", None if rate is None else rate["value"] is not None and
          rate["value"] >= float(rules["min_ingest_per_second"]),
          "not measured" if rate is None else f"{rate['value']} tags/s (minimum {rules['min_ingest_per_second']})")
    check("ingestion_not_stalled", None if stall is None else stall["value"] <= float(rules["max_ingest_stall_s"]),
          "not measured" if stall is None else f"longest stall {stall['value']} s (limit {rules['max_ingest_stall_s']} s)")
    resets = (m.get("tag_id_resets") or {}).get("value")
    if resets:
        check("no_counter_resets", False, f"newest tag id went backwards {resets} time(s) inside the baseline")
    floor_age = m.get("floor_age_max_s")
    check("floor_progressing", None if floor_age is None else floor_age["value"] <= float(rules["max_floor_age_s"]),
          "not measured" if floor_age is None else f"floor last check at most {floor_age['value']} s ago "
                                                   f"(limit {rules['max_floor_age_s']} s)")
    db_errors = (m.get("db_errors") or {}).get("value")
    if db_errors is not None:
        check("database_errors", db_errors == 0, f"{db_errors} database errors")
    injected = (m.get("db_injected_statements") or {}).get("value")
    if injected:
        check("no_injected_delay", False, f"{injected} statements were delayed inside the baseline")
    integrity = (evidence.controller.get("initial_resources") or {}).get("database_integrity")
    if integrity:
        check("database_integrity", integrity.get("ok"), f"quick_check before the run: {integrity.get('detail')}",
              f"{evidence.ref}#initial_resources.database_integrity")
    failed = [c["name"] for c in checks if c["ok"] is False]
    unknown = [c["name"] for c in checks if c["ok"] is None]
    # Too short or too few samples says nothing about health; a failed health check does.
    adequacy = {"sample_count", "duration"}
    if [name for name in failed if name not in adequacy]:
        readiness = "unhealthy"
    elif not samples or set(failed) & adequacy or not measured["window"]["adequate"]:
        readiness = "insufficient"
    else:
        readiness = "healthy"
    return {
        "source": evidence.summary() | {"samples": len(evidence.samples)},
        "window": {**window, "basis": basis, **measured["window"]},
        "metrics": measured["metrics"],
        "metric_definitions": {k: {"unit": METRICS[k][0], "definition": METRICS[k][1]}
                               for k in measured["metrics"] if k in METRICS},
        "missing_measurements": measured["unavailable"],
        "checks": checks,
        "readiness": readiness,
        "failed_checks": failed,
        "unmeasured_checks": unknown,
        "notes": list(evidence.notes) + ([] if readiness == "healthy" else [
            f"baseline is {readiness}: comparisons against it are qualified and do not describe normal operation"]),
    }


def analysis_dir(settings: Settings) -> Path:
    return settings.results_dir / "analysis"


def new_id(kind: str, label: str) -> str:
    safe = "".join(c if c.isalnum() or c == "-" else "-" for c in label)[:48].strip("-")
    return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{kind}-{safe}-{uuid.uuid4().hex[:6]}"


def persist(settings: Settings, assessment: dict) -> Path:
    directory = analysis_dir(settings) / "baselines"
    directory.mkdir(parents=True, exist_ok=True)
    baseline_id = new_id("baseline", assessment["source"]["ref"].split(":", 1)[-1])
    assessment = {"baseline_id": baseline_id, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  **assessment}
    path = directory / f"{baseline_id}.json"
    path.write_text(json.dumps(assessment, indent=2, default=str), encoding="utf-8")
    return path


def load_assessment(settings: Settings, baseline_id: str) -> dict:
    directory = analysis_dir(settings) / "baselines"
    matches = sorted(p for p in directory.glob("*.json") if baseline_id in p.stem) if directory.is_dir() else []
    if len(matches) != 1:
        raise evidence_module.EvidenceError(f"baseline assessment {baseline_id!r}: "
                                            f"{'not found' if not matches else 'ambiguous'}")
    return json.loads(matches[0].read_text(encoding="utf-8"))


def observe(settings: Settings, seconds: float, interval_s: float | None = None) -> Path:
    """A bounded, read-only observation of the running plant with the lab's own probes; no workload, no fault.

    The probes are the controller's samplers: health, an authenticated read of the work-order summary, equipment
    states, /metrics, /proc and the database hook's counters. Nothing is written to the plant.
    """
    from chaoslab.adapters.factorysemantics import FactorySemanticsAdapter
    from chaoslab.adapters.metrics import MetricsAdapter, Sampler

    seconds = max(5.0, min(float(seconds), MAX_OBSERVE_S))
    plant = FactorySemanticsAdapter(settings)
    health = plant.health(timeout=2.0)
    if not health.ok:
        raise evidence_module.EvidenceError("the plant API is not answering; nothing to observe")
    sampler = Sampler(MetricsAdapter(plant), float(interval_s or settings.lab["sampling"]["interval_s"]))
    sampler.set_phase("baseline")
    started = time.time()
    sampler.start()
    time.sleep(seconds)
    raw = sampler.stop()
    samples = [evidence_module._lab_sample(s) for s in raw]
    application = {"plant": settings.plant_name}
    try:
        from importlib.metadata import version

        application["application_version"] = version("factorysemantics-mes")
    except Exception:
        application["application_version"] = None
    intervals = [b["t"] - a["t"] for a, b in zip(samples, samples[1:])]
    artifact = {
        "format": evidence_module.ARTIFACT_FORMAT,
        "measurement_method": "chaoslab MetricsAdapter samples v1",
        "identity": {"source": "chaoslab read-only observation", "measurement_method":
                     "chaoslab MetricsAdapter samples v1", "application": "FactorySemantics MES",
                     "deployment_mode": "lightweight: fsmes plant (SQLite, OPC UA replay), no Docker",
                     "replay_speed": settings.lab["plant"].get("sim_speed"),
                     "replay_speed_basis": "config/lab.yaml at observation time", "workload": None,
                     "sampling_interval_s": round(sorted(intervals)[len(intervals) // 2], 3) if intervals else None,
                     "started_at": started, **application},
        "fault": {"type": "none", "injected": False},
        "windows": {"baseline": {"start": samples[0]["t"], "end": samples[-1]["t"]}} if samples else {},
        "samples": samples,
        "notes": [f"read-only observation for {seconds:.0f} s without the lab workload generator"],
    }
    directory = analysis_dir(settings) / "observations"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{new_id('observation', settings.plant_name)}.json"
    path.write_text(json.dumps(artifact, default=str), encoding="utf-8")
    return path
