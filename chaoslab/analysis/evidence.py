"""Load and normalise evidence from a completed failure, whatever ran it. Read only.

Three input kinds:

- `lab_result`: a run under results/<id>/ (result.json plus samples.jsonl), whoever triggered it.
- `opcua_report`: a report written by the external chaos-opcua tool (injections/chaos-opcua/results/*.json).
- `measurement_artifact`: a JSON file in the documented `chaoslab-measurements/v1` format.

Each becomes an `Evidence`: identity, fault metadata with timing and activation evidence, windows, and samples in
one normalised shape. A field a source does not measure is None, never zero. Nothing here writes anywhere.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from chaoslab.settings import Settings
from chaoslab.store import ResultStore

ARTIFACT_FORMAT = "chaoslab-measurements/v1"
SAMPLE_FIELDS = ("t", "label", "api_up", "api_latency_ms", "api_error", "auth_ok", "machines_total",
                 "machines_connected", "tag_id", "floor_age_s", "telemetry_age_s", "target_reachable",
                 "production", "database", "cgroups", "host_cpu_percent", "host_available_mb", "components",
                 "container_states")


class EvidenceError(Exception):
    pass


@dataclass
class Evidence:
    kind: str
    ref: str
    path: str
    identity: dict
    fault: dict
    windows: dict            # name -> {"start": epoch|None, "end": epoch|None, "basis": str}
    samples: list[dict]
    controller: dict         # the original verdict, recovery stages and cleanup, copied, never reinterpreted
    logs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    record: dict = field(default_factory=dict)   # the original record, for resolving evidence references

    def window_samples(self, name: str) -> list[dict]:
        window = self.windows.get(name)
        if not window or window.get("start") is None or window.get("end") is None:
            return []
        return [s for s in self.samples if window["start"] <= s["t"] <= window["end"]]

    def summary(self) -> dict:
        return {"kind": self.kind, "ref": self.ref, "path": self.path, "identity": self.identity,
                "fault": self.fault, "windows": self.windows, "samples": len(self.samples),
                "controller": self.controller, "logs": self.logs, "notes": self.notes}


def _epoch(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------------------------- lab results


def _lab_sample(raw: dict) -> dict:
    api = raw.get("api") or {}
    machines = raw.get("machines") or {}
    host = raw.get("host") or {}
    return {
        "t": float(raw["t"]), "label": raw.get("phase"),
        "api_up": api.get("up"), "api_latency_ms": api.get("latency_ms") if api.get("up") else None,
        "api_error": api.get("error"), "auth_ok": raw.get("authenticated_ok"),
        "machines_total": machines.get("total"), "machines_connected": machines.get("connected"),
        "tag_id": raw.get("tag_values_max_id"), "floor_age_s": raw.get("floor_seconds_since_check"),
        "telemetry_age_s": None, "target_reachable": None, "production": None,
        "database": raw.get("database"), "cgroups": raw.get("cgroups"),
        "host_cpu_percent": host.get("cpu_percent"), "host_available_mb": host.get("available_memory_mb"),
        "components": {name: bool(c.get("alive")) for name, c in (raw.get("components") or {}).items()},
    }


def _lab_fault(result: dict, samples: list[dict]) -> tuple[dict, dict]:
    steps = (result.get("fault_injection") or {}).get("steps") or []
    definition = result.get("definition") or {}
    defined = (definition.get("steps") or [{}])
    injected = [s for s in steps if s.get("injected")]
    step = injected[-1] if injected else (steps[-1] if steps else None)
    target = (step or {}).get("target") or (defined[-1] or {}).get("target")
    fault_type = (step or {}).get("type") or ((defined[-1] or {}).get("fault") or {}).get("type")
    params = (step or {}).get("parameters") or ((defined[-1] or {}).get("fault") or {}).get("parameters") or {}
    record = (step or {}).get("record") or {}
    started = _epoch(record.get("activated_at") or record.get("applied_at")) or \
        _epoch((step or {}).get("injection_finished_at"))
    hold = (step or {}).get("hold_s")
    ended = started + float(hold) if started is not None and hold is not None else None
    cleanup_done = _epoch((step or {}).get("cleanup_finished_at"))
    fault = {
        "type": fault_type, "target": target, "parameters": params,
        "requested_duration_s": params.get("duration_s"),
        "recorded_by": "lab controller (results/<id>/result.json fault_injection)" if step else None,
        "timing": {"injection_started_at": (step or {}).get("injection_started_at"),
                   "active_from": started, "active_until": ended, "cleanup_finished": cleanup_done,
                   "basis": ("fault-specific activation timestamp" if record.get("activated_at") or
                             record.get("applied_at") else "controller injection_finished_at + hold_s")
                   if started is not None else None},
        "injected": bool(injected),
        "injection_error": (step or {}).get("error"),
        "activation": {"status": "none" if fault_type in (None, "none") else "uncertain", "evidence": []},
    }
    windows: dict[str, dict] = {}
    base = [s["t"] for s in samples if s["label"] == "baseline"]
    windows["baseline"] = {"start": min(base), "end": max(base), "basis": "samples labelled baseline"} if base \
        else {"start": None, "end": None, "basis": "no baseline samples"}
    if started is not None and ended is not None:
        windows["fault"] = {"start": started, "end": ended, "basis": fault["timing"]["basis"]}
        after = [s["t"] for s in samples if s["t"] > ended]
        windows["recovery"] = {"start": ended, "end": max(after) if after else None,
                               "basis": "from the end of the fault window to the last sample"}
    else:
        windows["fault"] = {"start": None, "end": None, "basis": "no fault timing recorded"}
        windows["recovery"] = {"start": None, "end": None, "basis": "no fault timing recorded"}
    post = [s["t"] for s in samples if s["label"] == "post"]
    windows["post"] = {"start": min(post), "end": max(post), "basis": "samples labelled post (after the "
                       "controller verified recovery)"} if post else {"start": None, "end": None,
                                                                      "basis": "no post samples"}
    return fault, windows


def load_lab_result(settings: Settings, ref: str) -> Evidence:
    store = ResultStore(settings.results_dir)
    run_id = store.resolve(ref)
    result = store.read_result(run_id)
    directory = store.directory(run_id)
    samples_path = directory / "samples.jsonl"
    samples = []
    if samples_path.is_file():
        for line in samples_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                samples.append(_lab_sample(json.loads(line)))
    notes = []
    if not samples:
        notes.append("no per-sample measurements (samples.jsonl) in this run; only summaries are available")
    if (result.get("definition") or {}).get("category") == "application":
        notes.append("application experiment: evidence is the upstream harness's scored replay, not live samples")
    fault, windows = _lab_fault(result, samples)
    application = result.get("application") or {}
    definition = result.get("definition") or {}
    intervals = [b["t"] - a["t"] for a, b in zip(samples, samples[1:])]
    identity = {
        "source": "factory-chaos-lab controller", "measurement_method": "chaoslab MetricsAdapter samples v1",
        "application": application.get("name"), "application_version": application.get("version"),
        "application_commit": application.get("commit"), "deployment_mode": application.get("deployment_mode"),
        "plant": application.get("plant"), "workload": definition.get("workload"),
        "replay_speed": settings.lab["plant"].get("sim_speed"),
        "replay_speed_basis": "current config/lab.yaml; not recorded in the result",
        "lab_version": result.get("lab_version"),
        "sampling_interval_s": round(sorted(intervals)[len(intervals) // 2], 3) if intervals else None,
        "started_at": result.get("started_at"), "finished_at": result.get("finished_at"),
    }
    recovery = result.get("recovery") or {}
    controller = {"overall_result": result.get("overall_result"), "reasons": result.get("reasons"),
                  "recovered": recovery.get("recovered"), "recovery_duration_s": recovery.get("duration_s"),
                  "stages": recovery.get("stages"), "cleanup": result.get("cleanup"),
                  "initial_resources": {k: v for k, v in (result.get("initial_resources") or {}).items()
                                        if k in ("host", "database_integrity", "plant_rss_mb_total")}}
    logs_dir = directory / "logs"
    logs = sorted(_rel(p, settings.root) for p in logs_dir.rglob("*") if p.is_file()) if logs_dir.is_dir() else []
    evidence = Evidence(kind="lab_result", ref=f"run:{run_id}", path=_rel(directory / "result.json", settings.root),
                        identity=identity, fault=fault, windows=windows, samples=samples, controller=controller,
                        logs=logs, notes=notes, record=result)
    _lab_activation(evidence, result)
    return evidence


def _lab_activation(evidence: Evidence, result: dict) -> None:
    """Activation evidence: what shows the fault actually took effect, beyond the controller saying so."""
    from chaoslab.adapters.metrics import summarise_cgroups, summarise_database

    fault = evidence.fault
    if fault["type"] in (None, "none"):
        return
    items = []
    if fault["injected"]:
        items.append({"kind": "recorded", "detail": "the controller recorded the injection",
                      "ref": f"{evidence.ref}#fault_injection.steps"})
    in_window = evidence.window_samples("fault")
    measured = False
    component = (fault.get("target") or {}).get("component")
    if fault["type"] == "database_latency":
        summary = summarise_database([{"t": s["t"], "database": s["database"]} for s in in_window])
        if summary and summary.get("injected_statements"):
            measured = True
            items.append({"kind": "measured", "detail": f"{summary['injected_statements']} database statements "
                          f"delayed in the fault window, {summary['injected_delay_ms_mean']} ms each",
                          "ref": f"{evidence.ref}#database_latency.direct_database"})
    elif fault["type"] == "resource_limit":
        summary = (summarise_cgroups([{"t": s["t"], "cgroups": s["cgroups"]} for s in in_window]) or {}).get(component)
        if summary and ((summary.get("cpu_throttled_periods") or 0) > 0 or (summary.get("memory_high_events") or 0) > 0
                        or (summary.get("memory_current_mb_max") is not None and "memory.high" in
                            ((result.get("resource_limit") or {}).get("applied") or {}))):
            measured = True
            items.append({"kind": "measured", "detail": f"cgroup counters in the window: throttled periods "
                          f"{summary.get('cpu_throttled_periods')}, memory.high events "
                          f"{summary.get('memory_high_events')}", "ref": f"{evidence.ref}#resource_limit.cgroup"})
    elif fault["type"] in ("process_outage", "process_freeze") and component:
        down = [s for s in in_window if s["components"].get(component) is False]
        if down:
            measured = True
            items.append({"kind": "measured", "detail": f"{component} not alive in {len(down)} of "
                          f"{len(in_window)} fault-window samples", "ref": f"{evidence.ref}#samples"})
        elif fault["type"] == "process_freeze":
            unresponsive = [s for s in in_window if s["api_up"] is False] if component == "api" else []
            if unresponsive:
                measured = True
                items.append({"kind": "measured", "detail": f"API unresponsive in {len(unresponsive)} samples "
                              f"while {component} was frozen", "ref": f"{evidence.ref}#samples"})
    if fault["timing"]["active_from"] is None:
        status = "uncertain"
        items.append({"kind": "missing", "detail": "no fault timing recorded: the fault window cannot be placed"})
    elif measured:
        status = "confirmed_by_measurement"
    elif fault["injected"]:
        status = "recorded_by_controller"
    else:
        status = "not_injected"
    fault["activation"] = {"status": status, "evidence": items}


# --------------------------------------------------------------------------------------- external OPC reports


def load_opcua_report(settings: Settings, path: Path) -> Evidence:
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvidenceError(f"cannot read {path}: {exc}") from exc
    if not isinstance(report, dict) or report.get("schema_version") != 1 or "samples" not in report \
            or "timestamps" not in report:
        raise EvidenceError(f"{path} is not a chaos-opcua report (schema_version 1 with samples and timestamps)")
    stamps = report["timestamps"]
    started = _epoch(stamps.get("started_utc"))
    labels = {"BASELINE": "baseline", "FAULT_OBSERVATION": "fault", "RECOVERY_VALIDATION": "recovery"}
    samples = []
    for raw in report["samples"]:
        t = _epoch(raw.get("at_utc"))
        if t is None and started is not None and raw.get("t_s") is not None:
            t = started + float(raw["t_s"])
        if t is None:
            continue
        mes = raw.get("mes") or {}
        telemetry = raw.get("telemetry") or {}
        probe = raw.get("probe") or {}
        counters = {k: v.get("value") for k, v in (probe.get("values") or {}).items()
                    if k.endswith("Count") and isinstance(v, dict)}
        samples.append({
            "t": t, "label": labels.get(raw.get("phase"), str(raw.get("phase")).lower()),
            "api_up": mes.get("ok"), "api_latency_ms": None, "api_error": None, "auth_ok": None,
            "machines_total": mes.get("machines"), "machines_connected": mes.get("connected"),
            "tag_id": telemetry.get("max_id"), "floor_age_s": None,
            "telemetry_age_s": telemetry.get("stalest_age_s"), "target_reachable": probe.get("ok"),
            "production": counters or None, "database": None, "cgroups": None,
            "host_cpu_percent": None, "host_available_mb": None, "components": {},
        })
    samples.sort(key=lambda s: s["t"])
    injection = report.get("injection") or {}
    active_from = _epoch(stamps.get("fault_confirmed_utc") or stamps.get("injection_start_utc"))
    active_until = _epoch(stamps.get("restored_utc") or injection.get("end_utc"))
    target = report.get("target") or {}
    fault = {
        "type": f"opcua_{injection.get('adapter', 'unknown')}",
        "target": {"component": injection.get("component"), "id": target.get("id"),
                   "endpoint": target.get("endpoint")},
        "parameters": {"configured_fault_duration_s": injection.get("configured_fault_duration_s")},
        "requested_duration_s": injection.get("configured_fault_duration_s"),
        "recorded_by": "external chaos-opcua tool",
        "timing": {"injection_started_at": stamps.get("injection_start_utc"), "active_from": active_from,
                   "active_until": active_until, "cleanup_finished": _epoch(stamps.get("restored_utc")),
                   "actual_duration_s": injection.get("actual_fault_duration_s"),
                   "basis": "report timestamps fault_confirmed_utc .. restored_utc"},
        "injected": (report.get("outcomes") or {}).get("injection") == "succeeded",
        "injection_error": report.get("error"),
    }
    in_fault = [s for s in samples if active_from and active_until and active_from <= s["t"] <= active_until]
    unreachable = [s for s in in_fault if s["target_reachable"] is False]
    items = [{"kind": "recorded", "detail": f"report outcome injection={fault['injected'] and 'succeeded'}",
              "ref": f"opcua:{report.get('run_id')}#outcomes"}]
    if unreachable:
        items.append({"kind": "measured", "detail": f"independent probe could not reach the target in "
                      f"{len(unreachable)} of {len(in_fault)} fault samples", "ref": f"opcua:{report.get('run_id')}#samples"})
    status = "uncertain" if active_from is None else "confirmed_by_measurement" if unreachable else \
        "recorded_by_controller" if fault["injected"] else "not_injected"
    fault["activation"] = {"status": status, "evidence": items}
    windows = {
        "baseline": {"start": _epoch(stamps.get("baseline_start_utc")), "end": _epoch(stamps.get("baseline_end_utc")),
                     "basis": "report timestamps baseline_start_utc .. baseline_end_utc"},
        "fault": {"start": active_from, "end": active_until, "basis": fault["timing"]["basis"]},
        "recovery": {"start": active_until, "end": _epoch(stamps.get("recovery_end_utc")),
                     "basis": "report timestamps restored_utc .. recovery_end_utc"},
        "post": {"start": None, "end": None, "basis": "the report has no separate post-recovery window"},
    }
    evidence_meta = report.get("evidence") or {}
    mes = evidence_meta.get("mes") or {}
    plants = {(r.get("mes") or {}).get("plant") for r in report["samples"]} - {None}
    intervals = [b["t"] - a["t"] for a, b in zip(samples, samples[1:])]
    identity = {
        "source": "external chaos-opcua tool", "measurement_method": "chaos-opcua probe + MES status v1",
        "application": "FactorySemantics MES", "application_version": mes.get("version"),
        "application_commit": mes.get("commit"), "deployment_mode": report.get("deployment_mode"),
        "plant": ",".join(sorted(plants)) or None, "workload": (report.get("workload") or {}).get("order"),
        "replay_speed": None, "replay_speed_basis": "not recorded in the report",
        "sampling_interval_s": round(sorted(intervals)[len(intervals) // 2], 3) if intervals else None,
        "started_at": stamps.get("started_utc"), "finished_at": stamps.get("finished_utc"),
    }
    controller = {"overall_result": report.get("overall_status"), "outcomes": report.get("outcomes"),
                  "recovered": (report.get("recovery") or {}).get("outcome") == "recovered",
                  "recovery_duration_s": (report.get("metrics") or {}).get("calculated", {}).get(
                      "mes_reconnect_after_restoration_s"),
                  "stages": None, "cleanup": {"ok": (report.get("outcomes") or {}).get("cleanup") == "clean"},
                  "genuine_mes_checks": evidence_meta.get("genuine_mes_checks"),
                  "calculated": (report.get("metrics") or {}).get("calculated")}
    notes = ["external report: the MES API is observed as up/down only (no request latency), and there is no "
             "authenticated probe, floor freshness or database instrumentation"]
    return Evidence(kind="opcua_report", ref=f"opcua:{report.get('run_id')}", path=_rel(Path(path), settings.root),
                    identity=identity, fault=fault, windows=windows, samples=samples, controller=controller,
                    notes=notes, record=report)


# ------------------------------------------------------------------------------------ measurement artifacts


def load_artifact(settings: Settings, path: Path) -> Evidence:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvidenceError(f"cannot read {path}: {exc}") from exc
    problems = validate_artifact(data)
    if problems:
        raise EvidenceError(f"{path} is not a valid {ARTIFACT_FORMAT} artifact: " + "; ".join(problems))
    samples = [{name: s.get(name) for name in SAMPLE_FIELDS} for s in data["samples"]]
    for sample in samples:
        sample["components"] = sample.get("components") or {}
    samples.sort(key=lambda s: s["t"])
    fault = data.get("fault") or {}
    timing = fault.get("timing") or {}
    active_from, active_until = _epoch(timing.get("active_from")), _epoch(timing.get("active_until"))
    fault = {"type": fault.get("type"), "target": fault.get("target"), "parameters": fault.get("parameters") or {},
             "requested_duration_s": (fault.get("parameters") or {}).get("duration_s"),
             "recorded_by": fault.get("recorded_by") or "measurement artifact",
             "timing": {"active_from": active_from, "active_until": active_until, "basis": "artifact fault.timing"},
             "injected": bool(fault.get("injected")), "injection_error": None,
             "activation": {"status": "uncertain" if active_from is None else "recorded_by_controller",
                            "evidence": [{"kind": "recorded", "detail": "stated in the artifact"}]}}
    windows = {name: {"start": _epoch((data.get("windows") or {}).get(name, {}).get("start")),
                      "end": _epoch((data.get("windows") or {}).get(name, {}).get("end")),
                      "basis": "artifact windows"} for name in ("baseline", "fault", "recovery", "post")}
    activation = (data.get("fault") or {}).get("activation")
    if activation and activation.get("status") in ("confirmed_by_measurement", "recorded_by_controller", "uncertain"):
        fault["activation"] = activation
    if windows["fault"]["start"] is None and active_from is not None and active_until is not None:
        windows["fault"] = {"start": active_from, "end": active_until, "basis": "artifact fault.timing"}
    if windows["recovery"]["start"] is None and windows["fault"]["end"] is not None:
        after = [s["t"] for s in samples if s["t"] > windows["fault"]["end"]]
        windows["recovery"] = {"start": windows["fault"]["end"], "end": max(after) if after else None,
                               "basis": "after the artifact's fault window"}
    identity = dict(data.get("identity") or {})
    identity.setdefault("measurement_method", data.get("measurement_method"))
    return Evidence(kind="measurement_artifact", ref=f"artifact:{Path(path).name}", path=_rel(Path(path), settings.root),
                    identity=identity, fault=fault, windows=windows, samples=samples,
                    controller=dict(data.get("controller") or {}), notes=list(data.get("notes") or []), record=data)


def validate_artifact(data: Any) -> list[str]:
    problems = []
    if not isinstance(data, dict):
        return ["the top level must be an object"]
    if data.get("format") != ARTIFACT_FORMAT:
        problems.append(f"format must be {ARTIFACT_FORMAT!r}")
    if not data.get("measurement_method"):
        problems.append("measurement_method is required")
    samples = data.get("samples")
    if not isinstance(samples, list) or not samples:
        problems.append("samples must be a non-empty list")
    else:
        for index, sample in enumerate(samples[:10000]):
            if not isinstance(sample, dict) or not isinstance(sample.get("t"), (int, float)):
                problems.append(f"samples[{index}].t must be a number (epoch seconds)")
                break
            unknown = set(sample) - set(SAMPLE_FIELDS)
            if unknown:
                problems.append(f"samples[{index}] has unknown fields {sorted(unknown)}")
                break
    for name, window in (data.get("windows") or {}).items():
        if name not in ("baseline", "fault", "recovery", "post") or not isinstance(window, dict):
            problems.append(f"windows.{name} is not a known window object")
    return problems


def load(settings: Settings, ref: str) -> Evidence:
    """A run id (or fragment), a chaos-opcua report path, or a measurement artifact path."""
    path = Path(ref)
    if path.suffix == ".json" and path.is_file():
        head = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(head, dict) and head.get("format") == ARTIFACT_FORMAT:
            return load_artifact(settings, path)
        return load_opcua_report(settings, path)
    try:
        return load_lab_result(settings, ref)
    except FileNotFoundError as exc:
        raise EvidenceError(f"no run, report or artifact for {ref!r}: {exc}") from exc


def apply_fault_metadata(evidence: Evidence, meta: dict) -> None:
    """Fill fault metadata the evidence does not record. Supplied metadata never confirms activation."""
    supplied = []
    for key in ("type", "target", "parameters"):
        if meta.get(key) is not None and not evidence.fault.get(key):
            evidence.fault[key] = meta[key]
            supplied.append(key)
    timing = meta.get("timing") or {}
    if evidence.fault["timing"].get("active_from") is None and timing.get("active_from") is not None:
        start, end = _epoch(timing.get("active_from")), _epoch(timing.get("active_until"))
        evidence.fault["timing"].update(active_from=start, active_until=end, basis="supplied fault metadata")
        evidence.windows["fault"] = {"start": start, "end": end, "basis": "supplied fault metadata"}
        after = [s["t"] for s in evidence.samples if end is not None and s["t"] > end]
        evidence.windows["recovery"] = {"start": end, "end": max(after) if after else None,
                                        "basis": "after the supplied fault window"}
        supplied.append("timing")
    if supplied:
        evidence.fault["supplied_metadata"] = supplied
        evidence.notes.append(f"fault {', '.join(supplied)} supplied by the person running the analysis, not "
                              "recorded by the injecting system")
        if evidence.fault["activation"]["status"] == "uncertain" or "timing" in supplied:
            evidence.fault["activation"]["status"] = "uncertain"
            evidence.fault["activation"]["evidence"].append(
                {"kind": "supplied", "detail": "timing comes from supplied metadata; activation is not confirmed"})
