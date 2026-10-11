"""The Planner (proposes only) and the Recommender (read only), with deterministic model responses."""

import asyncio
import json
from pathlib import Path

import pytest

from analysis_fixtures import outage_run
from chaoslab import planning
from chaoslab.agents import commands
from chaoslab.agents.restricted import PlannerToolBox, ReadOnlyToolBox
from chaoslab.agents.roles import PLANNER
from chaoslab.analysis import engine, recommender, service, source
from chaoslab.mcp_server import build_server
from test_agents import ScriptedLLM, answer, calls

GOOD = {"name": "retest-api-outage", "category": "infrastructure", "baseline_s": 20,
        "target": {"plant": "bottling", "component": "api"},
        "fault": {"type": "process_outage", "parameters": {"duration_s": 5}}}


@pytest.fixture(autouse=True)
def real_upstream(settings):
    """The temporary lab root has no upstream checkout; read the real one, read only."""
    import copy

    from chaoslab.settings import PACKAGE_ROOT

    lab = copy.deepcopy(settings.lab)
    lab["upstream"]["path"] = str(PACKAGE_ROOT / "factorysemantics-mes")
    object.__setattr__(settings, "lab", lab)
    settings.__dict__.pop("upstream_dir", None)


@pytest.fixture
def analysed(settings):
    outage_run(settings)
    result = service.analyse_failure(settings, "api-outage-synthetic", with_narrative=False)
    return json.loads(Path(result["analysis"]).read_text())


# ------------------------------------------------------------------------------------------------- planner


def test_planner_holds_propose_but_nothing_that_runs(lab_root):
    toolbox = PlannerToolBox(build_server(lab_root), PLANNER.tools)
    permitted = asyncio.run(toolbox._permitted())
    assert "propose_experiment" in permitted
    assert not permitted & {"run_experiment", "emergency_stop", "plant_start", "plant_stop"}
    assert len(asyncio.run(toolbox.specs())) == len(PLANNER.tools)
    for tool in ("run_experiment", "plant_stop"):
        with pytest.raises(PermissionError):
            asyncio.run(PlannerToolBox(build_server(lab_root), PLANNER.tools + (tool,)).specs())
        assert asyncio.run(toolbox.call(tool, {})).startswith("ERROR")


def test_planner_proposals_are_saved_only_when_the_policy_allows(lab_root, settings, analysed):
    finding = analysed["findings"][0]["id"]
    bad = {**GOOD, "name": "too-long", "fault": {"type": "process_outage", "parameters": {"duration_s": 3600}}}
    llm = ScriptedLLM(
        calls(("get_analysis", {"analysis_id": analysed["analysis_id"], "section": "summary"})),
        calls(("propose_experiment", {"definition": bad, "rationale": "retest with a much longer outage window",
                                      "analysis_id": analysed["analysis_id"], "finding_ids": [finding]}),
              ("propose_experiment", {"definition": GOOD, "rationale": "retest the API outage to confirm recovery",
                                      "analysis_id": analysed["analysis_id"], "finding_ids": [finding],
                                      "expected_outcome": "recovered within 10 s"}),
              ("run_experiment", {"definition": GOOD})),
        answer({"analysis_id": analysed["analysis_id"], "proposals": [{"proposal_id": "made-up", "finding_ids": [],
                                                                       "question": "q"}],
                "not_proposed": [], "notes": []}))
    code, payload = commands.plan(lab_root, analysed["analysis_id"], llm=llm)
    assert code == 0
    saved = payload["proposals"]
    assert len(saved) == 1 and saved[0]["name"] == "retest-api-outage" and saved[0]["status"] == "proposed"
    assert payload["output"]["unverified_proposal_ids"] == ["made-up"]
    results = [c for c in json.loads(Path(payload["transcript"]).read_text())["transcript"] if c.get("tool")]
    assert json.loads(results[1]["result"])["saved"] is False
    assert results[3]["result"].startswith("ERROR")                      # run_experiment refused
    assert not (settings.state_dir / "submissions").exists()            # nothing ran


def test_a_proposal_runs_only_after_approval(lab_root, settings, analysed, monkeypatch):
    out = planning.propose(settings, __import__("chaoslab.api", fromlist=["LabApi"]).LabApi(lab_root), GOOD,
                           "retest the API outage to confirm recovery", analysed["analysis_id"],
                           [analysed["findings"][0]["id"]])
    assert out["saved"]
    ran = []

    def fake_inject(root, experiment, approver, task=None, llm=None):
        ran.append((experiment, approver("run_experiment", {}), json.loads(task.split(": ", 1)[1])))
        return 0, {"output": {"experiment_id": "20261011T000000Z-retest-api-outage-aaaaaa",
                              "overall_result": "recovered"}}

    monkeypatch.setattr(commands, "inject", fake_inject)
    assert planning.list_proposals(settings, "proposed")
    code, payload = commands.approve(lab_root, out["proposal_id"])
    assert code == 0 and ran[0][1] is True and ran[0][2] == GOOD
    _, record = planning.load(settings, out["proposal_id"])
    assert record["status"] == "ran" and [h["status"] for h in record["history"]] == ["proposed", "approved", "ran"]
    with pytest.raises(ValueError):
        commands.approve(lab_root, out["proposal_id"])                   # not twice


def test_proposals_must_cite_real_findings(lab_root, settings, analysed):
    from chaoslab.api import LabApi

    out = planning.propose(settings, LabApi(lab_root), GOOD, "a rationale that is long enough",
                           analysed["analysis_id"], ["W99"])
    assert out["saved"] is False and "W99" in out["reasons"][0]


# ---------------------------------------------------------------------------------------------- recommender


def test_recommender_is_read_only(lab_root):
    toolbox = ReadOnlyToolBox(build_server(lab_root), recommender.ROLE.tools)
    assert len(asyncio.run(toolbox.specs())) == len(recommender.ROLE.tools)
    for tool in ("propose_experiment", "run_experiment", "emergency_stop"):
        assert asyncio.run(toolbox.call(tool, {})).startswith("ERROR")
        with pytest.raises(PermissionError):
            asyncio.run(ReadOnlyToolBox(build_server(lab_root), recommender.ROLE.tools + (tool,)).specs())


def test_source_tools_stay_inside_the_upstream_code(settings):
    if not (Path(settings.upstream_dir) / "src" / "fsmes" / "db.py").is_file():
        pytest.skip("upstream checkout not present")
    page = source.read(settings, "factorysemantics-mes/src/fsmes/db.py", 1, 5)
    assert page["path"].endswith("fsmes/db.py") and len(page["lines"]) == 5
    for path in ("../../config/lab.yaml", "/etc/passwd", "factorysemantics-mes/src/fsmes/../../../README.md"):
        with pytest.raises(ValueError):
            source.read(settings, path)
    hits = source.search(settings, r"BEGIN IMMEDIATE", "db.py")
    assert hits["matches"] and hits["matches"][0]["path"].endswith("db.py")


def _valid_recommendation(analysis, settings):
    weakness = next(f for f in analysis["findings"] if f["type"] == "weakness")
    line = next(m for m in source.search(settings, r"retrying in 3s", "integrations/opc/agent.py")["matches"])
    others = [f["id"] for f in analysis["findings"] if f["type"] == "weakness" and f["id"] != weakness["id"]]
    return {"analysis_id": analysis["analysis_id"],
            "recommendations": [{"finding_id": weakness["id"], "title": "Bound client retries",
                                 "remediation": "Retry API calls with bounded backoff in the floor client",
                                 "rationale": "requests failed while the API was down",
                                 "locations": [{"path": line["path"], "line": line["line"],
                                                "excerpt": "retrying in 3s"}],
                                 "tradeoffs": "more load at recovery", "effort": "small",
                                 "acceptance_criteria": ["API success ratio back to 1.0 within 5 s of the API answering"],
                                 "manual_validation": "rerun the same outage and compare recovery time",
                                 "evidence": weakness["evidence"], "confidence": "medium",
                                 "cause_status": "unverified"}],
            "not_recommended": [{"finding_id": f, "reason": "covered by the first fix"} for f in others],
            "missing_evidence": []}


def test_recommendations_must_cite_lines_that_exist(lab_root, settings, analysed):
    if not (Path(settings.upstream_dir) / "src" / "fsmes" / "integrations" / "opc" / "agent.py").is_file():
        pytest.skip("upstream checkout not present")
    evidence = __import__("chaoslab.analysis.evidence", fromlist=["load"]).load(settings, "api-outage-synthetic")
    resolve = lambda ref: engine.resolve_ref(ref, analysed, evidence, settings)  # noqa: E731
    good = _valid_recommendation(analysed, settings)
    assert recommender.validate(good, analysed, resolve, settings) == []
    invented = json.loads(json.dumps(good))
    invented["recommendations"][0]["locations"][0]["line"] = 1
    invented["recommendations"][0]["acceptance_criteria"] = ["it should be faster"]
    invented["not_recommended"] = []
    problems = recommender.validate(invented, analysed, resolve, settings)
    assert any("excerpt not found" in p for p in problems)
    assert any("measurable" in p for p in problems)
    assert len(analysed["findings"]) == 1 or any("without a recommendation" in p for p in problems) or \
        not [f for f in analysed["findings"] if f["type"] == "weakness"][1:]


def test_recommender_retries_then_completes_and_saves(lab_root, settings, analysed):
    if not (Path(settings.upstream_dir) / "src" / "fsmes" / "integrations" / "opc" / "agent.py").is_file():
        pytest.skip("upstream checkout not present")
    good = _valid_recommendation(analysed, settings)
    bad = json.loads(json.dumps(good))
    bad["recommendations"][0]["locations"][0]["excerpt"] = "this code does not exist anywhere"
    llm = ScriptedLLM(
        calls(("get_analysis", {"analysis_id": analysed["analysis_id"], "section": "findings"}),
              ("search_source", {"pattern": "retrying in 3s"})), answer(bad),
        calls(("read_source", {"path": good["recommendations"][0]["locations"][0]["path"],
                               "start_line": good["recommendations"][0]["locations"][0]["line"] - 2,
                               "end_line": good["recommendations"][0]["locations"][0]["line"] + 2})), answer(good))
    result = service.recommend(settings, analysed["analysis_id"], llm=llm)
    assert result["status"] == "complete" and len(result["attempts"]) == 2
    directory = settings.results_dir / "analysis" / analysed["analysis_id"]
    assert json.loads((directory / "recommendations.json").read_text())["status"] == "complete"
    assert "Recommender agent" in (directory / "report.md").read_text()
