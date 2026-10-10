import pytest

from chaoslab import model


def good(**overrides):
    base = {
        "name": "example",
        "description": "d",
        "category": "infrastructure",
        "target": {"plant": "bottling", "component": "api"},
        "fault": {"type": "process_outage", "parameters": {"duration_s": 5}},
    }
    base.update(overrides)
    return base


def test_valid_definition_parses():
    experiment = model.from_mapping(good())
    assert experiment.steps[0].fault_type == "process_outage"
    assert experiment.baseline_s == 10


def test_unknown_top_level_key_rejected():
    with pytest.raises(model.DefinitionError, match="unknown top-level keys"):
        model.from_mapping(good(surprise=1))


def test_bad_name_rejected():
    with pytest.raises(model.DefinitionError, match="name must match"):
        model.from_mapping(good(name="../../etc/passwd"))


def test_bad_category_rejected():
    with pytest.raises(model.DefinitionError, match="category"):
        model.from_mapping(good(category="chaos"))


def test_steps_and_fault_are_exclusive():
    with pytest.raises(model.DefinitionError, match="either steps or target"):
        model.from_mapping(good(steps=[{"target": {"plant": "bottling"}, "fault": {"type": "none"}}]))


def test_missing_fault_type_rejected():
    definition = good()
    definition["fault"] = {"parameters": {}}
    with pytest.raises(model.DefinitionError, match="fault must be a mapping with a type"):
        model.from_mapping(definition)


def test_non_mapping_rejected():
    with pytest.raises(model.DefinitionError):
        model.from_mapping(["not", "a", "mapping"])


def test_steps_form_parses():
    experiment = model.from_mapping({
        "name": "two", "category": "infrastructure",
        "steps": [
            {"target": {"plant": "bottling", "component": "api"}, "fault": {"type": "process_outage"}},
            {"target": {"plant": "bottling", "component": "opc-sim"}, "fault": {"type": "process_outage"}},
        ],
    })
    assert len(experiment.steps) == 2


def test_yaml_file_not_found():
    with pytest.raises(model.DefinitionError, match="not found"):
        model.load_file("/nonexistent/definition.yaml")


def test_shipped_definitions_all_parse():
    from pathlib import Path

    directory = Path(__file__).resolve().parents[1] / "experiments" / "definitions"
    files = sorted(directory.glob("*.yaml"))
    assert len(files) >= 10
    for file in files:
        model.load_file(file)


def test_state_machine_allows_the_normal_path():
    path = [model.State.PENDING, model.State.PREFLIGHT, model.State.BASELINE, model.State.INJECTING,
            model.State.FAULT_ACTIVE, model.State.CLEANUP, model.State.RECOVERING, model.State.VERIFYING,
            model.State.COMPLETED]
    for current, following in zip(path, path[1:]):
        assert following in model.TRANSITIONS[current]


def test_state_machine_forbids_skipping_cleanup():
    assert model.State.COMPLETED not in model.TRANSITIONS[model.State.FAULT_ACTIVE]
    assert model.State.RECOVERING not in model.TRANSITIONS[model.State.FAULT_ACTIVE]


def test_terminal_states_have_no_exits():
    for state in model.TERMINAL:
        assert model.TRANSITIONS[state] == set()
