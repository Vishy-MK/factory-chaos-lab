"""Detection of externally performed container faults in recordings. SYNTHETIC samples."""

import json

from analysis_fixtures import T0
from chaoslab.analysis import engine, recorder
from chaoslab.analysis.evidence import ARTIFACT_FORMAT


def state(running=True, paused=False, network=True, restarts=0, cpus=1_000_000_000, memory=402653184):
    return {"status": "running" if running else "exited", "running": running, "paused": paused,
            "on_lab_network": network, "health": "healthy", "restart_count": restarts, "nano_cpus": cpus,
            "memory": memory}


def sample(t, api, up=True, tag=1000):
    return {"t": t, "label": "recording", "api_up": up, "api_latency_ms": 8.0 if up else None, "api_error": None,
            "auth_ok": up or None, "machines_total": 2, "machines_connected": 2, "tag_id": tag, "floor_age_s": None,
            "telemetry_age_s": None, "target_reachable": None, "production": None, "database": None,
            "cgroups": None, "host_cpu_percent": 10.0, "host_available_mb": 3000.0,
            "components": {"api": api["running"] and not api["paused"]},
            "container_states": {"api": api, "postgres": state()}}


def recording(kind):
    changed = {"stopped": state(running=False), "paused": state(paused=True), "off_network": state(network=False),
               "restarted": state(restarts=1), "limits_changed": state(cpus=200_000_000)}[kind]
    out, tag = [], 1000
    for i in range(60):
        disturbed = 20 <= i < 30
        tag += 0 if disturbed else 2
        out.append(sample(T0 + i, changed if disturbed else state(restarts=1 if kind == "restarted" and i >= 20
                                                                                else 0),
                          up=not (disturbed and kind in ("stopped", "paused")), tag=tag))
    return out


def test_each_kind_of_container_change_is_detected():
    for kind, fault in (("stopped", "container_stop"), ("paused", "container_pause"),
                        ("off_network", "container_network_disconnect"), ("limits_changed", "container_resource_limit")):
        found = recorder.detect(recording(kind))
        assert found["detected"] and found["primary"] == {"component": "api", "kind": kind}, kind
        assert found["start"] == T0 + 20 and found["end"] == T0 + 29.5, kind
        assert recorder.KIND_FAULT[kind] == fault


def test_a_restart_is_detected_once_and_a_quiet_recording_is_not():
    found = recorder.detect(recording("restarted"))
    assert found["detected"] and found["kinds"] == ["restarted"]
    quiet = [sample(T0 + i, state()) for i in range(30)]
    assert recorder.detect(quiet) == {"detected": False,
                                      "reason": "no container state change was observed during the recording"}


def test_a_recorded_container_stop_is_analysed_end_to_end(settings, tmp_path):
    samples = recording("stopped")
    found = recorder.detect(samples)
    artifact = {"format": ARTIFACT_FORMAT, "measurement_method": "chaoslab MetricsAdapter samples v1 + container states",
                "identity": {"plant": "bottling-docker", "measurement_method": "m", "deployment_mode": "docker"},
                "fault": {"type": "container_stop", "target": {"plant": "bottling-docker", "component": "api"},
                          "injected": True, "timing": {"active_from": found["start"], "active_until": found["end"]},
                          "activation": {"status": "confirmed_by_measurement",
                                         "evidence": [{"kind": "measured", "detail": "api observed stopped"}]}},
                "windows": {"baseline": {"start": T0, "end": found["start"] - 0.001}},
                "samples": samples}
    path = tmp_path / "recording.json"
    path.write_text(json.dumps(artifact))
    analysis = engine.analyse(settings, str(path))
    assert analysis["inputs"]["experiment"]["fault"]["activation"]["status"] == "confirmed_by_measurement"
    assert analysis["comparisons"]["fault"]["metrics"]["api_success_ratio"]["percentage_points"] == -100.0
    assert analysis["recovery"]["recovery_time"]["recovered"] is True
    kinds = {f["kind"] for f in analysis["findings"]}
    assert "availability:api_success_ratio" in kinds
    assert analysis["evidence_check"]["ok"]
