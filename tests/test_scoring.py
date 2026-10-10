import json
import tomllib
from pathlib import Path

import pytest

from chaoslab.adapters.scoring import ScoringAdapter

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def scoring(settings):
    return ScoringAdapter(settings)


def test_plan_is_valid_toml_with_the_requested_events(scoring, tmp_path):
    events = [{"type": "down", "station": "RD", "start": 600, "end": 720}]
    plan = scoring.build_plan(tmp_path, "x", "a description", 1500, 20.0, 42, ["booking", "oee"], events)
    data = tomllib.loads(plan.read_text())
    assert data["duration"] == 1500
    assert data["speed"] == 20.0
    assert data["seed"] == 42
    assert data["measure"] == ["booking", "oee"]
    assert data["scenario"]["bottling"]["events"] == events


def test_baseline_plan_has_an_empty_event_list(scoring, tmp_path):
    plan = scoring.build_plan(tmp_path, "x", "d", 1500, 20.0, 42, ["booking"], [])
    assert tomllib.loads(plan.read_text())["scenario"]["bottling"]["events"] == []


def test_derive_reads_real_harness_output(scoring):
    scores = json.loads((FIXTURES / "scores-lost-connection.json").read_text())
    watched = json.loads((FIXTURES / "watched-lost-connection.json").read_text())
    events = [{"type": "disconnect", "start": 360, "end": 600}, {"type": "disconnect", "start": 1080, "end": 1260}]
    derived = scoring.derive(scores, watched, events, 1500)
    assert derived["available"] is True
    assert derived["product_version"] == "0.2.0"
    assert derived["booking"]["stations_total"] == 6
    assert derived["booking"]["stations_answered"] == 6
    assert derived["booking"]["stations_in_band"] == 0
    assert all(row["inside_band"] is False for row in derived["booking"]["stations"])
    assert derived["downtime"]["breakdowns_scripted"] == 1
    assert derived["downtime"]["breakdowns_detected"] is None
    assert derived["downtime"]["recall"] is None
    assert derived["connection"]["scripted_outages"] == 2
    assert derived["oee"]["stations_answered"] == 6


def test_unavailable_scores_are_none_not_zero(scoring):
    scores = json.loads((FIXTURES / "scores-lost-connection.json").read_text())
    derived = scoring.derive(scores, None, [], 1500)
    assert derived["downtime"]["recall"] is None
    assert derived["state_timeline"]["available"] is False


def test_state_timeline_extracts_states_around_a_window(scoring):
    watched = json.loads((FIXTURES / "watched-lost-connection.json").read_text())
    timeline = scoring.state_timeline(watched, [{"type": "down", "station": "RD", "start": 100, "end": 400}], 1500)
    window = timeline["windows"][0]
    assert window["looks_inside_window"] > 0
    assert window["looks_after_window"] > 0
    assert window["states_at_last_look_after_window"]


def test_missing_plant_is_reported(scoring):
    derived = scoring.derive({"plants": []}, None, [], 1500)
    assert derived["available"] is False
