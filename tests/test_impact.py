from chaoslab.observability.impact import DEGRADED, DOWN, NORMAL, UNKNOWN, ImpactTracker, target_service


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def alive(**overrides):
    table = {n: {"alive": True, "status": "sleeping"} for n in ("api", "opc-sim", "opc-agent", "floor")}
    table.update(overrides)
    return table


def probe(latency=0.01, tag=1000, connected=6, floor=5.0, up=True):
    return {"plant_up": up, "api_latency_s": latency, "machines_connected": connected, "machines_total": 6,
            "floor_age_s": floor, "tag_id": tag}


def db(statements, ms, active=False, errors=0, locked=0):
    return {"api": {"statements": {"read": statements}, "observed_ms": ms, "errors": errors, "lock_errors": locked,
                    "active": active}}


def warm(tracker, clock, n=20):
    for i in range(n):
        clock.t += 5
        tracker.evaluate(probe(tag=1000 + 75 * (i + 1)), alive(), db(100 * (i + 1), 10.0 * (i + 1)), None, True,
                         experiment_active=False)


def test_a_quiet_plant_is_normal_and_unmeasured_is_unknown():
    clock = Clock()
    tracker = ImpactTracker(clock)
    first = tracker.evaluate({}, alive(), None, None, None, False)
    assert first["Database"]["status"] == UNKNOWN and first["MES API"]["status"] == UNKNOWN
    assert "RAG backend" not in first
    warm(tracker, clock)
    clock.t += 5
    status = tracker.evaluate(probe(tag=1000 + 75 * 21), alive(), db(2100, 210.0), None, True, False)
    assert all(v["status"] == NORMAL for v in status.values()), status


def test_process_faults_are_down_or_frozen():
    clock = Clock()
    tracker = ImpactTracker(clock)
    status = tracker.evaluate(probe(), alive(api={"alive": False}, floor={"alive": True, "status": "stopped"}),
                              None, None, None, True)
    assert status["MES API"]["status"] == DOWN and "not running" in status["MES API"]["reason"]
    assert status["Shop floor"]["status"] == DOWN and "frozen" in status["Shop floor"]["reason"]


def test_slowness_is_judged_against_the_services_own_baseline():
    clock = Clock()
    tracker = ImpactTracker(clock)
    warm(tracker, clock)
    clock.t += 5
    status = tracker.evaluate(probe(latency=1.2, tag=3000), alive(), db(2200, 210.0 + 100 * 200.0, active=True),
                              None, True, experiment_active=True)
    assert status["MES API"]["status"] == DEGRADED and "1200 ms" in status["MES API"]["reason"]
    assert status["Database"]["status"] == DEGRADED and "latency injection active" in status["Database"]["reason"]


def test_ingestion_stall_and_counter_reset():
    clock = Clock()
    tracker = ImpactTracker(clock)
    warm(tracker, clock)
    for _ in range(4):
        clock.t += 5
        status = tracker.evaluate(probe(tag=2500), alive(), None, None, None, True)
    assert status["OPC agent / ingestion"]["status"] == DOWN
    clock.t += 5
    reset = tracker.evaluate(probe(tag=10), alive(), None, None, None, True)
    assert reset["OPC agent / ingestion"]["status"] in (UNKNOWN, NORMAL)


def test_machine_connectivity_and_rag_backend():
    clock = Clock()
    tracker = ImpactTracker(clock)
    partial = tracker.evaluate(probe(connected=2), alive(), None, None, False, True)
    assert partial["Machine connectivity"]["status"] == DEGRADED and partial["RAG backend"]["status"] == DOWN
    none = tracker.evaluate(probe(connected=0), alive(), None, None, True, True)
    assert none["Machine connectivity"]["status"] == DOWN and none["RAG backend"]["status"] == NORMAL


def test_cgroup_throttling_degrades_the_limited_service():
    clock = Clock()
    tracker = ImpactTracker(clock)
    warm(tracker, clock)
    stats = lambda periods, throttled: {"api": {"cpu_nr_periods": periods, "cpu_nr_throttled": throttled,  # noqa
                                                "memory_events": {"high": 0}}}
    clock.t += 5
    tracker.evaluate(probe(tag=3000), alive(), None, stats(100, 0), None, True)
    clock.t += 5
    status = tracker.evaluate(probe(tag=3075), alive(), None, stats(200, 90), None, True)
    assert status["MES API"]["status"] == DEGRADED and "throttled 90%" in status["MES API"]["reason"]


def test_injection_targets_map_to_services():
    assert target_service([{"type": "database_latency"}]) == [("database_latency", "Database")]
    assert target_service([{"type": "process_outage", "component": "opc-sim"}]) == [("process_outage", "OPC UA server")]
    assert target_service([{"type": "resource_limit", "component": "api"}]) == [("resource_limit", "MES API")]
    assert target_service([]) == []
