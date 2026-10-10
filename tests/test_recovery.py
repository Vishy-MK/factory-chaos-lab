import time

from chaoslab.chaos.recovery import RecoveryVerifier


class DummyPlant:
    def __init__(self, integrity=(True, "ok")):
        self.integrity = integrity

    def database_integrity(self):
        return self.integrity


def make_sample(t, api=True, auth=True, connected=6, total=6, age=2.0, tag_id=100, alive=True):
    return {
        "t": t, "phase": "recovery",
        "api": {"up": api, "latency_ms": 5.0 if api else None, "status": 200 if api else None, "error": None},
        "authenticated_ok": auth if api else None,
        "machines": {"total": total if api else None, "connected": connected if api else None,
                     "disconnected": 0, "unknown": 0},
        "floor_seconds_since_check": age if api else None,
        "tag_values_max_id": tag_id if api else None,
        "work_orders": {"good_qty": 1, "scrap_qty": 0, "running": 1},
        "equipment_states": None,
        "components": {c: {"alive": alive, "status": "sleeping", "rss_mb": 1, "cpu_percent": 0}
                       for c in ("api", "opc-sim", "opc-agent", "floor")},
        "host": {"available_memory_mb": 4000, "cpu_percent": 5},
    }


def verifier(plant=None):
    return RecoveryVerifier(plant or DummyPlant(), ["api", "opc-sim", "opc-agent", "floor"], stable_samples=2)


def test_a_healthy_sample_passes_every_stage():
    sample = make_sample(100.0, tag_id=130)
    stages = verifier().evaluate_sample(sample, [(90.0, 100)], 100)
    assert stages["service_available"]["ok"]
    assert stages["operation_succeeds"]["ok"]
    assert stages["manufacturing_behaviour"]["ok"]


def test_process_running_is_not_recovery_when_machines_are_disconnected():
    sample = make_sample(100.0, connected=0, tag_id=130)
    stages = verifier().evaluate_sample(sample, [(90.0, 100)], 100)
    assert stages["service_available"]["ok"] is True
    assert stages["manufacturing_behaviour"]["ok"] is False


def test_api_up_is_not_recovery_when_ingestion_is_stalled():
    sample = make_sample(100.0, tag_id=100)
    stages = verifier().evaluate_sample(sample, [(90.0, 100)], 100)
    assert stages["manufacturing_behaviour"]["ok"] is False
    assert "tag id 100 -> 100" in stages["manufacturing_behaviour"]["detail"]


def test_floor_silence_prevents_recovery():
    sample = make_sample(100.0, age=500.0, tag_id=130)
    assert verifier().evaluate_sample(sample, [(90.0, 100)], 100)["manufacturing_behaviour"]["ok"] is False


def test_failed_authenticated_operation_prevents_recovery():
    sample = make_sample(100.0, auth=False, tag_id=130)
    assert verifier().evaluate_sample(sample, [(90.0, 100)], 100)["operation_succeeds"]["ok"] is False


def test_dead_component_prevents_recovery():
    sample = make_sample(100.0, alive=False, tag_id=130)
    assert verifier().evaluate_sample(sample, [(90.0, 100)], 100)["service_available"]["ok"] is False


def test_api_down_marks_everything_unavailable():
    sample = make_sample(100.0, api=False)
    stages = verifier().evaluate_sample(sample, [], 100)
    assert stages["service_available"]["ok"] is False
    assert stages["operation_succeeds"]["ok"] is False
    assert stages["manufacturing_behaviour"]["ok"] is False


def test_wait_reports_recovery_with_a_duration():
    start = time.time()
    samples = [make_sample(start - 10 + i, tag_id=100 + i * 10) for i in range(6)]
    fresh = [make_sample(start + 0.2 + i * 0.2, tag_id=300 + i * 10) for i in range(10)]

    def source():
        return samples + fresh

    report = verifier().wait(source, {"ok": True, "detail": "removed"}, 5.0, 100)
    assert report["recovered"] is True
    assert report["duration_s"] is not None and report["duration_s"] >= 0
    assert report["stages"]["data_consistency"]["ok"] is True


def test_wait_times_out_when_the_system_stays_degraded():
    start = time.time()

    def source():
        return [make_sample(start + 0.1 + i * 0.1, connected=0) for i in range(30)]

    report = verifier().wait(source, {"ok": True, "detail": "removed"}, 1.0, 100)
    assert report["recovered"] is False
    assert report["duration_s"] is None
    assert report["stages"]["manufacturing_behaviour"]["ok"] is False


def test_data_consistency_fails_on_a_corrupt_database():
    start = time.time()

    def source():
        return [make_sample(start + 0.1 + i * 0.2, tag_id=500 + i * 10) for i in range(20)] + \
               [make_sample(start - 10 + i, tag_id=100 + i * 10) for i in range(6)]

    report = verifier(DummyPlant((False, "page 3: corrupt"))).wait(
        source, {"ok": True, "detail": "removed"}, 5.0, 100)
    assert report["recovered"] is False
    assert report["stages"]["data_consistency"]["ok"] is False


def test_unfinished_fault_removal_prevents_recovery():
    start = time.time()

    def source():
        return [make_sample(start - 10 + i, tag_id=100 + i * 10) for i in range(6)] + \
               [make_sample(start + 0.1 + i * 0.2, tag_id=500 + i * 10) for i in range(20)]

    report = verifier().wait(source, {"ok": False, "detail": "still frozen"}, 1.5, 100)
    assert report["recovered"] is False
    assert report["stages"]["fault_removed"]["ok"] is False
