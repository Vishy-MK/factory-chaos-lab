import time

import pytest

from chaoslab.settings import ConfigError
from chaoslab.workload.generator import WorkloadGenerator, available_profiles, load_profile


def test_profiles_exist_and_load(settings):
    assert {"idle", "baseline", "demonstration", "burst"} <= set(available_profiles(settings))
    assert load_profile(settings, "baseline")["operations"]


def test_unknown_profile_is_an_error(settings):
    with pytest.raises(ConfigError):
        load_profile(settings, "nope")


def test_rate_is_capped_by_policy(stub_plant, settings):
    generator = WorkloadGenerator(stub_plant, load_profile(settings, "burst"), 5.0)
    assert generator.rate == 5.0


def test_idle_profile_sends_nothing(stub_plant, settings):
    generator = WorkloadGenerator(stub_plant, load_profile(settings, "idle"), 5.0)
    generator.start()
    time.sleep(0.2)
    report = generator.stop()
    assert report["total_requests"] == 0


def test_generator_issues_bounded_requests_and_stops(stub_plant, settings):
    profile = dict(load_profile(settings, "baseline"))
    profile["requests_per_second"] = 20
    generator = WorkloadGenerator(stub_plant, profile, 20.0)
    generator.start()
    time.sleep(1.0)
    report = generator.stop()
    assert 10 <= report["total_requests"] <= 30
    assert report["total_errors"] == 0
    after = report["total_requests"]
    time.sleep(0.3)
    assert generator.report()["total_requests"] == after


def test_generator_records_errors(stub_plant, settings):
    stub_plant.state.api_up = False
    profile = dict(load_profile(settings, "baseline"))
    profile["operations"] = ["health"]
    profile["requests_per_second"] = 20
    generator = WorkloadGenerator(stub_plant, profile, 20.0)
    generator.start()
    time.sleep(0.5)
    report = generator.stop()
    assert report["total_requests"] > 0
    assert report["total_errors"] == report["total_requests"]
    assert report["per_operation"]["health"]["mean_latency_ms"] is None
