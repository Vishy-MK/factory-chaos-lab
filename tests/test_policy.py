import copy

import pytest

from chaoslab import model
from chaoslab.policy import PolicyValidator

STATIONS = ("LD", "RD", "Washer", "QI", "Refill", "Palletiser")


def infra(fault_type="process_outage", target=None, parameters=None, **extra):
    definition = {
        "name": "t", "category": "infrastructure",
        "target": target or {"plant": "bottling", "component": "api"},
        "fault": {"type": fault_type, "parameters": parameters if parameters is not None else {"duration_s": 5}},
    }
    definition.update(extra)
    return model.from_mapping(definition)


def app(fault_type="sim_down", target=None, parameters=None, application=None):
    return model.from_mapping({
        "name": "a", "category": "application",
        "target": target or {"plant": "bottling", "station": "RD"},
        "fault": {"type": fault_type,
                  "parameters": parameters if parameters is not None else {"start_s": 600, "end_s": 720}},
        "application": application or {"duration_s": 1500, "speed": 20, "seed": 42},
    })


@pytest.fixture
def validator(settings):
    return PolicyValidator(settings, STATIONS)


def test_valid_infrastructure_experiment_is_allowed(validator):
    decision = validator.validate(infra())
    assert decision.allowed, decision.reasons


def test_valid_application_experiment_is_allowed(validator):
    decision = validator.validate(app())
    assert decision.allowed, decision.reasons


def test_duration_over_limit_is_rejected(validator):
    decision = validator.validate(infra(parameters={"duration_s": 61}))
    assert not decision.allowed
    assert any("maximum" in r or "max_fault_duration_s" in r for r in decision.reasons)


def test_duration_at_limit_is_allowed(validator):
    assert validator.validate(infra(parameters={"duration_s": 60})).allowed


def test_zero_or_negative_duration_rejected(validator):
    assert not validator.validate(infra(parameters={"duration_s": 0})).allowed
    assert not validator.validate(infra(parameters={"duration_s": -3})).allowed


def test_non_integer_duration_rejected(validator):
    assert not validator.validate(infra(parameters={"duration_s": "ten"})).allowed
    assert not validator.validate(infra(parameters={"duration_s": True})).allowed


def test_unknown_parameter_rejected(validator):
    decision = validator.validate(infra(parameters={"duration_s": 5, "shell": "rm -rf /"}))
    assert not decision.allowed
    assert any("unknown parameter" in r for r in decision.reasons)


def test_unknown_plant_rejected(validator):
    decision = validator.validate(infra(target={"plant": "production", "component": "api"}))
    assert not decision.allowed
    assert any("allowlist" in r for r in decision.reasons)


def test_unknown_component_rejected(validator):
    decision = validator.validate(infra(target={"plant": "bottling", "component": "docker"}))
    assert not decision.allowed


def test_component_not_allowed_for_fault_rejected(validator):
    decision = validator.validate(infra("cpu_pressure", {"plant": "bottling", "component": "opc-sim"},
                                        {"duration_s": 5, "percent": 50}))
    assert not decision.allowed
    assert any("not an allowlisted target" in r for r in decision.reasons)


@pytest.mark.parametrize("key", ["host", "address", "ip", "url", "port"])
def test_arbitrary_network_targets_rejected(validator, key):
    target = {"plant": "bottling", "component": "api", key: "10.0.0.5"}
    decision = validator.validate(infra(target=target))
    assert not decision.allowed
    assert any("not accepted" in r or "may not specify" in r for r in decision.reasons)


def test_unknown_fault_type_rejected(validator):
    decision = validator.validate(infra("rm_rf"))
    assert not decision.allowed
    assert any("unknown fault type" in r for r in decision.reasons)


@pytest.mark.parametrize("name", ["database_outage", "api_latency", "memory_pressure"])
def test_unsupported_faults_are_refused_with_a_reason(validator, name):
    decision = validator.validate(infra(name))
    assert not decision.allowed
    assert any("not supported" in r for r in decision.reasons)


def test_compound_disabled_by_default(validator):
    experiment = model.from_mapping({
        "name": "c", "category": "infrastructure",
        "steps": [
            {"target": {"plant": "bottling", "component": "api"}, "fault": {"type": "process_outage"}},
            {"target": {"plant": "bottling", "component": "opc-sim"}, "fault": {"type": "process_outage"}},
        ],
    })
    decision = validator.validate(experiment)
    assert not decision.allowed
    assert any("compound" in r for r in decision.reasons)


def test_compound_allowed_when_enabled(settings):
    policy = copy.deepcopy(settings.policy)
    policy["compound"]["enabled"] = True
    object.__setattr__(settings, "policy", policy)
    experiment = model.from_mapping({
        "name": "c", "category": "infrastructure",
        "steps": [
            {"target": {"plant": "bottling", "component": "api"}, "fault": {"type": "process_outage"}},
            {"target": {"plant": "bottling", "component": "opc-sim"}, "fault": {"type": "process_outage"}},
        ],
    })
    assert PolicyValidator(settings, STATIONS).validate(experiment).allowed


def test_too_many_steps_rejected_even_when_compound_enabled(settings):
    policy = copy.deepcopy(settings.policy)
    policy["compound"]["enabled"] = True
    object.__setattr__(settings, "policy", policy)
    step = {"target": {"plant": "bottling", "component": "api"}, "fault": {"type": "process_outage"}}
    experiment = model.from_mapping({"name": "c", "category": "infrastructure", "steps": [step] * 4})
    decision = PolicyValidator(settings, STATIONS).validate(experiment)
    assert not decision.allowed
    assert any("max_steps_per_experiment" in r for r in decision.reasons)


def test_cpu_pressure_percent_bounded(validator):
    decision = validator.validate(infra("cpu_pressure", None, {"duration_s": 5, "percent": 95}))
    assert not decision.allowed


def test_cpu_pressure_duration_bounded_by_policy(validator):
    decision = validator.validate(infra("cpu_pressure", None, {"duration_s": 31, "percent": 50}))
    assert not decision.allowed


def test_high_volume_workload_rejected(validator):
    decision = validator.validate(infra(workload="burst"))
    assert not decision.allowed
    assert any("high-volume" in r or "requests_per_second" in r for r in decision.reasons)


def test_unknown_workload_rejected(validator):
    assert not validator.validate(infra(workload="nonexistent")).allowed


def test_baseline_bounds(validator):
    assert not validator.validate(infra(baseline_s=1)).allowed
    assert not validator.validate(infra(baseline_s=600)).allowed


def test_recovery_timeout_cannot_exceed_policy(validator):
    decision = validator.validate(infra(recovery={"timeout_s": 3600}))
    assert not decision.allowed


def test_station_must_exist(validator):
    decision = validator.validate(app(target={"plant": "bottling", "station": "Reactor"}))
    assert not decision.allowed
    assert any("not part of the line" in r for r in decision.reasons)


def test_application_window_wall_time_bounded(validator):
    decision = validator.validate(app(parameters={"start_s": 0, "end_s": 3000},
                                      application={"duration_s": 3600, "speed": 20}))
    assert not decision.allowed
    assert any("max_scripted_fault_wall_s" in r or "estimated wall time" in r for r in decision.reasons)


def test_application_window_must_be_ordered(validator):
    decision = validator.validate(app(parameters={"start_s": 700, "end_s": 600}))
    assert not decision.allowed


def test_application_speed_bounded(validator):
    decision = validator.validate(app(application={"duration_s": 1500, "speed": 500}))
    assert not decision.allowed


def test_application_duration_required(validator):
    decision = validator.validate(app(application={"speed": 20}))
    assert not decision.allowed


def test_infrastructure_fault_in_application_experiment_rejected(validator):
    experiment = model.from_mapping({
        "name": "x", "category": "application",
        "target": {"plant": "bottling", "component": "api"},
        "fault": {"type": "process_outage", "parameters": {"duration_s": 5}},
        "application": {"duration_s": 1500},
    })
    decision = validator.validate(experiment)
    assert not decision.allowed
    assert any("cannot run in a application" in r for r in decision.reasons)


def test_simulator_fault_in_infrastructure_experiment_rejected(validator):
    decision = validator.validate(infra("sim_down", {"plant": "bottling", "station": "RD"},
                                        {"start_s": 1, "end_s": 5}))
    assert not decision.allowed


def test_missing_target_key_rejected(validator):
    decision = validator.validate(infra(target={"plant": "bottling"}))
    assert not decision.allowed
    assert any("missing" in r for r in decision.reasons)


def test_database_target_must_be_the_database_resource(validator):
    decision = validator.validate(infra("sqlite_write_lock", {"plant": "bottling", "resource": "/etc/passwd"},
                                        {"duration_s": 5}))
    assert not decision.allowed
