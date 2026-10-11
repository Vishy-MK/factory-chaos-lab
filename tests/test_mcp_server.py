import asyncio
import sys
import time

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from chaoslab.audit import Lease
from chaoslab.mcp_server import build_server
from chaoslab.store import ResultStore

WRITE_TOOLS = {"run_experiment", "emergency_stop", "plant_start", "plant_stop"}


@pytest.fixture
def server(lab_root):
    return build_server(lab_root)


def call(server, tool_name, **arguments):
    return asyncio.run(server.call_tool(tool_name, arguments)).structured_content


def definition(**overrides):
    base = {
        "name": "proposal", "category": "infrastructure",
        "target": {"plant": "bottling", "component": "api"},
        "fault": {"type": "process_outage", "parameters": {"duration_s": 5}},
    }
    base.update(overrides)
    return base


def store_two_runs(settings):
    store = ResultStore(settings.results_dir)
    ids = []
    for index, success in enumerate((0.0, 0.5), start=1):
        run_id = f"20261010T00000{index}Z-proposal-00000{index}"
        store.create(run_id)
        store.write_result(run_id, {
            "experiment_id": run_id, "overall_result": "recovered",
            "definition": {"name": "proposal", "category": "infrastructure",
                           "steps": [{"target": {"plant": "bottling", "component": "api"},
                                      "fault": {"type": "process_outage", "parameters": {}}}]},
            "observed_metrics": {"fault": {"api_success_ratio": success}},
            "recovery": {"recovered": True, "duration_s": 2.0, "stages": {}},
            "cleanup": {"ok": True}, "scoring": {"applied": False}, "final_health": {},
        })
        ids.append(run_id)
    return ids


def test_only_the_tools_that_change_the_plant_are_marked_destructive(server):
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert len(tools) == 22
    assert {n for n, t in tools.items() if not t.annotations.read_only_hint} == WRITE_TOOLS
    assert all(tools[n].annotations.destructive_hint for n in WRITE_TOOLS)


def test_discovery_tools_return_the_lab_contract(server):
    names = {f["name"] for f in call(server, "list_fault_types")["fault_types"]}
    assert {"process_outage", "sqlite_write_lock", "sim_down"} <= names
    targets = call(server, "list_targets")
    assert sorted(targets["plants"]["bottling"]["components"]) == ["api", "floor", "opc-agent", "opc-sim"]
    assert "resources" in call(server, "get_safety_policy")


def test_validate_explains_a_rejected_proposal(server):
    assert call(server, "validate_experiment", definition=definition())["allowed"] is True
    bad = call(server, "validate_experiment",
               definition=definition(fault={"type": "process_outage", "parameters": {"duration_s": 600}}))
    assert bad["allowed"] is False
    assert bad["stage"] == "policy"
    assert bad["reasons"]


def test_run_refuses_a_policy_violation_without_starting_anything(server):
    response = call(server, "run_experiment", definition=definition(target={"plant": "prod", "component": "api"}))
    assert response["accepted"] is False
    assert response["stage"] == "policy"
    assert call(server, "experiment_status")["active"] is None


def test_run_refuses_while_another_experiment_holds_the_lease(server, settings):
    Lease(settings.lease_file, settings.stop_flag).acquire("20261010T000000Z-held-aaaaaa", "held", time.time() + 100)
    response = call(server, "run_experiment", definition=definition())
    assert response["accepted"] is False
    assert response["stage"] == "lease"


def test_run_needs_exactly_one_of_definition_or_name(server):
    with pytest.raises(ToolError, match="exactly one of definition or name"):
        call(server, "run_experiment")
    with pytest.raises(ToolError, match="exactly one of definition or name"):
        call(server, "run_experiment", definition=definition(), name="proposal")


def test_refusals_reach_the_agent_with_their_reason(server):
    with pytest.raises(ToolError, match="no experiment matches 'nope'"):
        call(server, "get_result", experiment_id="nope")
    with pytest.raises(ToolError, match="no experiment definition named 'nope'"):
        call(server, "run_experiment", name="nope")


def test_result_tools_read_stored_runs(server, settings):
    first, second = store_two_runs(settings)
    runs = call(server, "list_runs")["runs"]
    assert [r["experiment_id"] for r in runs] == [second, first]
    assert call(server, "experiment_status", experiment_id=first)["finished"] is True
    assert call(server, "get_recovery", experiment_id=second)["recovery"]["recovered"] is True
    assert call(server, "get_scoring", experiment_id=second)["scoring"]["applied"] is False
    assert call(server, "get_result", experiment_id=second)["overall_result"] == "recovered"
    assert call(server, "get_metrics", experiment_id=second)["observed_metrics"]
    assert call(server, "get_logs", experiment_id=second)["experiment_id"] == second
    rows = call(server, "compare_runs", first=first, second=second)["rows"]
    assert next(r for r in rows if r["metric"] == "fault_api_success_ratio")["delta"] == 0.5


def test_wait_returns_a_finished_run_at_once_and_times_out_on_an_unknown_one(server, settings, monkeypatch):
    import chaoslab.mcp_server as module

    monkeypatch.setattr(module, "POLL_S", 0.05)
    first, _ = store_two_runs(settings)
    done = call(server, "wait_for_experiment", experiment_id=first)
    assert done["finished"] is True
    assert done["timed_out"] is False
    pending = call(server, "wait_for_experiment", experiment_id="20261010T000009Z-never-000009", timeout_s=1)
    assert pending["timed_out"] is True
    assert pending["state"] == "unknown"


def test_emergency_stop_with_nothing_active_is_safe(server):
    report = call(server, "emergency_stop")
    assert report["ok"] is True
    assert report["active_experiment"] is None


def test_check_resources_reports_the_gate(server):
    report = call(server, "check_resources")
    assert isinstance(report["experiments_allowed"], bool)
    assert report["threshold_available_mb"] > 0


def test_the_cli_serves_the_tools_over_stdio(lab_root):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from chaoslab.settings import PACKAGE_ROOT

    async def list_names():
        params = StdioServerParameters(command=sys.executable, cwd=str(PACKAGE_ROOT),
                                       args=["-m", "chaoslab", "--root", str(lab_root), "mcp"])
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            return {t.name for t in (await session.list_tools()).tools}

    names = asyncio.run(asyncio.wait_for(list_names(), timeout=30))
    assert WRITE_TOOLS <= names
    assert len(names) == 22
