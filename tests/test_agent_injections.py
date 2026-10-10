"""The Injector and the Recovery Analyst use the database latency and resource limit faults through MCP tools."""

import asyncio
import json

import pytest

from chaoslab.agents.roles import ANALYST, INJECTOR
from chaoslab.agents.runtime import Agent, ToolBox, deny_all
from chaoslab.mcp_server import build_server
from chaoslab.store import ResultStore
from test_agents import ScriptedLLM, answer, calls

DEFINITIONS = {
    "database_latency": {"name": "db-latency-proposal", "category": "infrastructure", "baseline_s": 20,
                         "target": {"plant": "bottling", "resource": "database"},
                         "fault": {"type": "database_latency", "parameters": {"delay_ms": 200, "duration_s": 10}}},
    "resource_limit": {"name": "cpu-limit-proposal", "category": "infrastructure", "baseline_s": 20,
                       "target": {"plant": "bottling", "component": "api"},
                       "fault": {"type": "resource_limit", "parameters": {"cpu_percent": 20, "duration_s": 15}}},
}


def agent(lab_root, role, llm, approver=deny_all):
    return Agent(role, llm, ToolBox(build_server(lab_root), role.tools), approver)


def tool_result(run, index):
    return json.loads(run.transcript[index]["result"])


@pytest.mark.parametrize("fault", sorted(DEFINITIONS))
def test_injector_discovers_validates_and_dry_runs_the_fault(lab_root, fault):
    definition = DEFINITIONS[fault]
    llm = ScriptedLLM(
        calls(("list_fault_types", {}), ("list_targets", {})),
        calls(("validate_experiment", {"definition": definition})),
        calls(("run_experiment", {"definition": definition, "dry_run": True})),
        answer({"experiment_name": definition["name"], "outcome": "preflight_failed"}),
    )
    run = asyncio.run(agent(lab_root, INJECTOR, llm).run(f"Run a {fault} experiment."))
    raw = run.transcript[0]["result"]
    assert "[truncated" not in raw, "the whole catalogue must reach the model"
    listed = {f["name"]: f for f in json.loads(raw)["fault_types"]}
    assert listed[fault]["supported"] is True
    bounds = {p["name"]: (p.get("minimum"), p.get("maximum")) for p in listed[fault]["parameters"]}
    assert bounds["duration_s"] == (1, 60)
    targets = tool_result(run, 1)
    assert targets["fault_targets"][fault]
    validation = tool_result(run, 2)
    assert validation["allowed"] is True, validation
    dry = tool_result(run, 3)
    assert dry["overall_result"] == "dry_run_ok", dry
    # Every tool result went back to the model as a tool message.
    assert sum(1 for m in llm.seen_messages[-1] if m["role"] == "tool") == 4


@pytest.mark.parametrize("fault", sorted(DEFINITIONS))
def test_the_real_fault_waits_for_a_person(lab_root, settings, fault):
    asked = []

    def approver(tool, arguments):
        asked.append(arguments)
        return False

    llm = ScriptedLLM(calls(("run_experiment", {"definition": DEFINITIONS[fault]})), answer({"outcome": "denied"}))
    run = asyncio.run(agent(lab_root, INJECTOR, llm, approver=approver).run("go"))
    assert len(asked) == 1
    assert run.transcript[0]["result"].startswith("DENIED")
    assert not (settings.state_dir / "submissions").exists() or not list((settings.state_dir / "submissions").iterdir())


def test_the_injector_cannot_talk_the_policy_into_a_stronger_fault(lab_root):
    stronger = json.loads(json.dumps(DEFINITIONS["database_latency"]))
    stronger["fault"]["parameters"]["delay_ms"] = 5000
    weaker_cpu = json.loads(json.dumps(DEFINITIONS["resource_limit"]))
    weaker_cpu["fault"]["parameters"]["cpu_percent"] = 1
    llm = ScriptedLLM(calls(("validate_experiment", {"definition": stronger}),
                            ("validate_experiment", {"definition": weaker_cpu})), answer({}))
    run = asyncio.run(agent(lab_root, INJECTOR, llm).run("go"))
    for index in (0, 1):
        assert tool_result(run, index)["allowed"] is False


def test_the_analyst_receives_the_fault_evidence(lab_root, settings):
    store = ResultStore(settings.results_dir)
    run_id = "20261010T000002Z-db-latency-proposal-000002"
    store.create(run_id)
    evidence = {"configured_delay_ms": 200, "measured_added_statement_ms_mean": 266.5,
                "direct_database": {"fault": {"injected_delay_ms_mean": 200.1}}, "downstream": {},
                "restored": {"ok": True, "detail": "back within tolerance"}}
    store.write_result(run_id, {
        "experiment_id": run_id, "overall_result": "recovered", "database_latency": evidence,
        "preparation": [{"step": 1, "name": "prepared", "ok": True, "detail": "already in scope"}],
        "definition": {"name": "db-latency-proposal", "category": "infrastructure", "steps": []},
        "observed_metrics": {"fault": {"api_success_ratio": 1.0}},
        "recovery": {"recovered": True, "duration_s": 0.5, "stages": {
            "database_latency_restored": {"ok": True, "detail": "back within tolerance"}}},
        "cleanup": {"ok": True}, "scoring": {"applied": False}, "final_health": {},
    })
    llm = ScriptedLLM(calls(("get_recovery", {"experiment_id": run_id}), ("get_metrics", {"experiment_id": run_id})),
                      answer({"experiment_id": run_id, "verdict": "recovered"}))
    run = asyncio.run(agent(lab_root, ANALYST, llm).run("analyse"))
    recovery, metrics = tool_result(run, 0), tool_result(run, 1)
    assert recovery["recovery"]["stages"]["database_latency_restored"]["ok"] is True
    assert recovery["preparation"][0]["ok"] is True
    assert metrics["fault_evidence"]["database_latency"]["measured_added_statement_ms_mean"] == 266.5
    assert "fault_evidence" in ANALYST.system_prompt and "resource_limit" in ANALYST.system_prompt


def test_fault_detail_is_available_by_name(lab_root):
    llm = ScriptedLLM(calls(("list_fault_types", {"name": "resource_limit"}),
                            ("list_fault_types", {"name": "rm_rf"})), answer({}))
    run = asyncio.run(agent(lab_root, INJECTOR, llm).run("go"))
    detail = tool_result(run, 0)["fault_types"][0]
    assert detail["name"] == "resource_limit" and detail["recovery_criteria"] and detail["cleanup"]
    assert run.transcript[1]["result"].startswith("ERROR")
