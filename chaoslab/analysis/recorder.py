"""Record the lab's Docker stack, read-only, while something else injects a fault into it.

Samples once a second with the lab's normal probes (health, an authenticated read, equipment states, /metrics) and
each container's state, network attachment, restart count and limits. Nothing is changed. The fault window is
detected from observed container changes, so attribution rests on measurement, not on what anyone says was done.
The result is a `chaoslab-measurements/v1` artifact for `chaoslab analysis analyze`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from chaoslab.analysis import baseline as baseline_module
from chaoslab.analysis.evidence import ARTIFACT_FORMAT, _lab_sample

MAX_RECORD_S = 600


def _container_states() -> dict:
    from chaoslab.chaos import containers

    states = {}
    for name in containers.lab_components():
        try:
            info = containers.inspect(name)
        except containers.ContainerError:
            continue
        states[name] = {k: info[k] for k in ("status", "running", "paused", "on_lab_network", "health",
                                              "restart_count", "nano_cpus", "memory")}
    return states


def _disturbance(first: dict, current: dict) -> dict[str, str]:
    """component -> what is different from the first sample (stopped, paused, off_network, restarted, limits)."""
    out = {}
    for name, state in current.items():
        start = first.get(name) or {}
        if not state["running"]:
            out[name] = "stopped"
        elif state["paused"]:
            out[name] = "paused"
        elif not state["on_lab_network"]:
            out[name] = "off_network"
        elif state["restart_count"] > start.get("restart_count", state["restart_count"]):
            out[name] = "restarted"
        elif (state["nano_cpus"], state["memory"]) != (start.get("nano_cpus"), start.get("memory")) and start:
            out[name] = "limits_changed"
    for name in first:
        if name not in current:
            out[name] = "removed"
    return out


KIND_FAULT = {"stopped": "container_stop", "paused": "container_pause", "off_network": "container_network_disconnect",
              "restarted": "container_restart", "limits_changed": "container_resource_limit",
              "removed": "container_removed"}


def detect(samples: list[dict]) -> dict:
    """Fault window and attribution from container state changes in the recorded samples."""
    if not samples:
        return {"detected": False}
    first = samples[0].get("container_states") or {}
    disturbed = [(s["t"], _disturbance(first, s.get("container_states") or {})) for s in samples]
    hits = [(t, d) for t, d in disturbed if d]
    if not hits:
        return {"detected": False, "reason": "no container state change was observed during the recording"}
    start, end = hits[0][0], hits[-1][0]
    components = sorted({c for _, d in hits for c in d})
    kinds = sorted({k for _, d in hits for k in d.values()})
    primary_component, primary_kind = next(iter(hits[0][1].items()))
    after = [t for t in (s["t"] for s in samples) if t > end]
    # End halfway to the next sample, so the first undisturbed sample is not counted inside the fault.
    end = end + (after[0] - end) / 2 if after else end
    return {"detected": True, "start": start, "end": end, "components": components,
            "kinds": kinds, "primary": {"component": primary_component, "kind": primary_kind},
            "samples_disturbed": len(hits)}


def record(settings, seconds: float, label: str = "external", interval_s: float = 1.0) -> Path:
    from chaoslab.adapters.docker_plant import DockerPlantAdapter
    from chaoslab.adapters.metrics import MetricsAdapter
    from chaoslab.chaos import containers

    if settings.deployment != "docker":
        raise ValueError("record observes the Docker stack: run it with CHAOSLAB_DEPLOYMENT=docker")
    ok, detail = containers.available()
    if not ok:
        raise ValueError(detail)
    if not containers.lab_components():
        raise ValueError("the lab's Docker stack is not running; start it with scripts/docker-up.sh")
    seconds = max(10.0, min(float(seconds), MAX_RECORD_S))
    plant = DockerPlantAdapter(settings)
    plant.collect_stats = False
    metrics = MetricsAdapter(plant)
    samples = []
    started = time.time()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        tick = time.monotonic()
        raw = metrics.sample("recording")
        sample = _lab_sample(raw)
        sample["container_states"] = _container_states()
        samples.append(sample)
        time.sleep(max(0.0, interval_s - (time.monotonic() - tick)))
    found = detect(samples)
    fault: dict = {"type": None, "injected": False, "recorded_by": "chaoslab analysis record (observed, read-only)"}
    windows: dict = {}
    if found["detected"]:
        primary = found["primary"]
        fault.update(type=KIND_FAULT.get(primary["kind"], "container_change"),
                     target={"plant": settings.plant_name, "component": primary["component"]},
                     parameters={"observed_duration_s": round(found["end"] - found["start"], 2)},
                     injected=True,
                     timing={"active_from": found["start"], "active_until": found["end"]},
                     activation={"status": "confirmed_by_measurement", "evidence": [
                         {"kind": "measured", "detail": f"{', '.join(found['components'])} observed "
                          f"{', '.join(found['kinds'])} in {found['samples_disturbed']} samples",
                          "ref": "samples"}]})
        windows = {"baseline": {"start": samples[0]["t"], "end": found["start"] - 0.001},
                   "fault": {"start": found["start"], "end": found["end"]}}
        post = [s["t"] for s in samples if s["t"] > found["end"] + 10]
        if post:
            windows["post"] = {"start": post[0], "end": post[-1]}
    intervals = [b["t"] - a["t"] for a, b in zip(samples, samples[1:])]
    artifact = {
        "format": ARTIFACT_FORMAT,
        "measurement_method": "chaoslab MetricsAdapter samples v1 + container states",
        "identity": {"source": "chaoslab analysis record", "measurement_method":
                     "chaoslab MetricsAdapter samples v1 + container states", "application": "FactorySemantics MES",
                     "deployment_mode": "docker: lab compose stack (PostgreSQL)", "plant": settings.plant_name,
                     "replay_speed": 1.0, "replay_speed_basis": "docker/compose.lab.yml MES_SIM_SPEED",
                     "workload": None, "label": label,
                     "sampling_interval_s": round(sorted(intervals)[len(intervals) // 2], 3) if intervals else None,
                     "started_at": started},
        "fault": fault,
        "windows": windows,
        "controller": {"overall_result": None,
                       "note": "the fault was performed outside the lab; there is no controller verdict"},
        "samples": samples,
        "detection": found,
        "notes": ["recorded read-only while an external system acted on the Docker stack",
                  "no cleanup or removal record exists for an external fault",
                  "the stack has no floor simulator"],
    }
    directory = baseline_module.analysis_dir(settings) / "recordings"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{baseline_module.new_id('recording', label)}.json"
    path.write_text(json.dumps(artifact, default=str), encoding="utf-8")
    return path
