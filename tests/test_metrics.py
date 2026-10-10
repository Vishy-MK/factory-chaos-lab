from chaoslab.adapters.factorysemantics import parse_prometheus
from chaoslab.adapters.metrics import MetricsAdapter, Sampler


def test_prometheus_text_is_parsed():
    text = (
        '# HELP x y\n'
        'mes_plant_info{plant="bottling"} 1\n'
        'mes_work_orders{plant="bottling",status="running"} 3\n'
        'mes_tag_values_max_id{plant="bottling"} 15133\n'
        'mes_coverage_floor{plant="bottling"} 0.8\n'
    )
    samples = parse_prometheus(text)
    assert ("mes_tag_values_max_id", {"plant": "bottling"}, 15133.0) in samples
    work = [s for s in samples if s[0] == "mes_work_orders"][0]
    assert work[1] == {"plant": "bottling", "status": "running"}


def test_sample_carries_values_when_the_api_is_up(stub_plant):
    sample = MetricsAdapter(stub_plant).sample("baseline")
    assert sample["api"]["up"] is True
    assert sample["authenticated_ok"] is True
    assert sample["machines"]["connected"] == 6
    assert sample["tag_values_max_id"] is not None
    assert set(sample["components"]) == {"api", "opc-sim", "opc-agent", "floor"}


def test_unavailable_values_are_none_not_zero_when_the_api_is_down(stub_plant):
    stub_plant.state.api_up = False
    sample = MetricsAdapter(stub_plant).sample("fault")
    assert sample["api"]["up"] is False
    assert sample["authenticated_ok"] is None
    assert sample["tag_values_max_id"] is None
    assert sample["machines"]["connected"] is None
    assert sample["work_orders"]["good_qty"] is None
    assert sample["equipment_states"] is None


def test_summary_of_unavailable_data_is_none(stub_plant):
    adapter = MetricsAdapter(stub_plant)
    stub_plant.state.api_up = False
    summary = adapter.summarise([adapter.sample("fault"), adapter.sample("fault")])
    assert summary["api_success_ratio"] == 0.0
    assert summary["api_latency_ms"] is None
    assert summary["tag_ingest_per_second"] is None
    assert summary["machines_connected_min"] is None
    assert summary["authenticated_success_ratio"] is None


def test_summary_computes_ingest_rate_and_distribution(stub_plant):
    adapter = MetricsAdapter(stub_plant)
    samples = []
    for index in range(5):
        sample = adapter.sample("baseline")
        sample["t"] = 1000.0 + index
        sample["tag_values_max_id"] = 100 + 10 * index
        samples.append(sample)
    summary = adapter.summarise(samples)
    assert summary["tag_ingest_per_second"] == 10.0
    assert summary["api_latency_ms"]["count"] == 5
    assert summary["api_success_ratio"] == 1.0


def test_empty_summary():
    assert MetricsAdapter(None).summarise([]) == {"samples": 0}


def test_sampler_collects_and_stops_cleanly(stub_plant):
    sampler = Sampler(MetricsAdapter(stub_plant), 0.05)
    sampler.start()
    sampler.set_phase("baseline")
    import time

    time.sleep(0.4)
    samples = sampler.stop()
    assert len(samples) >= 3
    assert all(s["phase"] in ("idle", "baseline", "transition") for s in samples)


def test_samples_straddling_a_phase_change_are_labelled_transition(stub_plant):
    import threading
    import time

    original = stub_plant.health
    gate = threading.Event()

    def slow_health(timeout=None):
        gate.wait(1.0)
        return original(timeout)

    stub_plant.health = slow_health
    sampler = Sampler(MetricsAdapter(stub_plant), 0.05)
    sampler.set_phase("baseline")
    sampler.start()
    time.sleep(0.1)
    sampler.set_phase("fault")
    gate.set()
    time.sleep(0.3)
    samples = sampler.stop()
    straddling = [s for s in samples if s["phase"] == "transition"]
    assert straddling
    assert straddling[0]["phase_before"] == "baseline"
    assert straddling[0]["phase_after"] == "fault"
    assert all(s["phase"] != "baseline" or s["t"] < straddling[0]["t"] + 0.01 for s in samples)
