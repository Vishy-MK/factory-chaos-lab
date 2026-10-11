import asyncio
import json

import httpx
import pytest

from chaoslab.agents import commands
from chaoslab.agents.llm import ChatClient, LLMConfig, LLMError, Reply, ToolCall, parse_reply
from chaoslab.agents.roles import ANALYST, INJECTOR
from chaoslab.agents.runtime import Agent, Role, ToolBox, approve_all, deny_all, extract_json
from chaoslab.mcp_server import build_server
from chaoslab.store import ResultStore


class ScriptedLLM:
    """Plays back replies in order and records what the agent offered it."""

    def __init__(self, *replies: Reply):
        self.replies = list(replies)
        self.seen_tools: list[list[str]] = []
        self.seen_messages: list[list[dict]] = []

    async def chat(self, messages, tools):
        self.seen_tools.append([t["function"]["name"] for t in tools])
        self.seen_messages.append([dict(m) for m in messages])
        return self.replies.pop(0)


def calls(*pairs) -> Reply:
    return Reply(content="", tool_calls=[ToolCall(id=f"c{i}", name=name, arguments=json.dumps(args))
                                         for i, (name, args) in enumerate(pairs)])


def answer(payload: dict) -> Reply:
    return Reply(content=json.dumps(payload))


def bad_definition():
    return {"name": "proposal", "category": "infrastructure", "target": {"plant": "prod", "component": "api"},
            "fault": {"type": "process_outage", "parameters": {"duration_s": 5}}}


def good_definition():
    return {"name": "proposal", "category": "infrastructure", "target": {"plant": "bottling", "component": "api"},
            "fault": {"type": "process_outage", "parameters": {"duration_s": 5}}}


def store_run(settings, run_id="20261010T000001Z-proposal-000001"):
    store = ResultStore(settings.results_dir)
    store.create(run_id)
    store.write_result(run_id, {
        "experiment_id": run_id, "overall_result": "recovered",
        "definition": {"name": "proposal", "category": "infrastructure",
                       "steps": [{"target": {"plant": "bottling", "component": "api"},
                                  "fault": {"type": "process_outage", "parameters": {}}}]},
        "observed_metrics": {"fault": {"api_success_ratio": 0.0}},
        "recovery": {"recovered": True, "duration_s": 2.0, "stages": {}},
        "cleanup": {"ok": True}, "scoring": {"applied": False}, "final_health": {},
    })
    return run_id


def agent(lab_root, role, llm, approver=deny_all):
    return Agent(role, llm, ToolBox(build_server(lab_root), role.tools), approver)


def submissions(settings):
    directory = settings.state_dir / "submissions"
    return list(directory.iterdir()) if directory.is_dir() else []


# ------------------------------------------------------------------ the loop


def test_each_agent_is_offered_only_its_own_tools(lab_root):
    for role in (INJECTOR, ANALYST):
        llm = ScriptedLLM(answer({}))
        asyncio.run(agent(lab_root, role, llm).run("go"))
        assert llm.seen_tools[0] == list(role.tools)
    assert "run_experiment" not in ANALYST.tools
    assert not {"plant_start", "plant_stop"} & set(INJECTOR.tools)


def test_tool_results_go_back_to_the_model_and_the_final_json_is_parsed(lab_root, settings):
    run_id = store_run(settings)
    llm = ScriptedLLM(calls(("get_recovery", {"experiment_id": run_id})),
                      answer({"experiment_id": run_id, "verdict": "recovered"}))
    run = asyncio.run(agent(lab_root, ANALYST, llm).run("analyse"))
    tool_message = llm.seen_messages[1][-1]
    assert tool_message["role"] == "tool" and tool_message["tool_call_id"] == "c0"
    assert json.loads(tool_message["content"])["recovery"]["recovered"] is True
    assert run.output == {"experiment_id": run_id, "verdict": "recovered"}
    assert run.stopped == "answered"


def test_a_tool_outside_the_role_is_refused_and_not_run(lab_root, settings):
    llm = ScriptedLLM(calls(("run_experiment", {"definition": good_definition()})), answer({}))
    run = asyncio.run(agent(lab_root, ANALYST, llm, approver=approve_all).run("go"))
    assert run.transcript[0]["result"] == "ERROR: run_experiment is not one of this agent's tools"
    assert submissions(settings) == []


def test_a_real_fault_waits_for_the_person_and_a_dry_run_does_not(lab_root, settings):
    asked = []

    def approver(tool, arguments):
        asked.append(arguments)
        return False

    llm = ScriptedLLM(calls(("run_experiment", {"definition": bad_definition(), "dry_run": True})),
                      calls(("run_experiment", {"definition": good_definition()})), answer({}))
    run = asyncio.run(agent(lab_root, INJECTOR, llm, approver=approver).run("go"))
    assert len(asked) == 1 and "dry_run" not in asked[0]
    assert run.transcript[1]["result"].startswith("DENIED")
    assert submissions(settings) == []


def test_an_approved_fault_still_meets_the_safety_policy(lab_root, settings):
    llm = ScriptedLLM(calls(("run_experiment", {"definition": bad_definition()})), answer({}))
    run = asyncio.run(agent(lab_root, INJECTOR, llm, approver=approve_all).run("go"))
    result = json.loads(run.transcript[0]["result"])
    assert result["accepted"] is False and result["stage"] == "policy"
    assert submissions(settings) == []


def test_refusals_and_bad_arguments_reach_the_model_as_text(lab_root):
    llm = ScriptedLLM(Reply(content="", tool_calls=[
        ToolCall(id="a", name="get_result", arguments=json.dumps({"experiment_id": "nope"})),
        ToolCall(id="b", name="get_result", arguments="{not json"),
    ]), answer({}))
    run = asyncio.run(agent(lab_root, ANALYST, llm).run("go"))
    assert run.transcript[0]["result"] == "ERROR: Error executing tool get_result: no experiment matches 'nope'"
    assert run.transcript[1]["result"].startswith("ERROR: could not parse the arguments")


def test_the_loop_stops_at_its_round_limit(lab_root):
    role = Role(name="looper", system_prompt="x", tools=("list_runs",), max_rounds=3)
    llm = ScriptedLLM(*[calls(("list_runs", {})) for _ in range(3)])
    run = asyncio.run(agent(lab_root, role, llm).run("go"))
    assert run.stopped == "max_rounds" and run.rounds == 3 and run.output is None


@pytest.mark.parametrize("text", [
    '{"verdict": "recovered"}',
    'Here it is:\n```json\n{"verdict": "recovered"}\n```',
    'Done. {"verdict": "recovered"} That is all.',
])
def test_extract_json_finds_the_object(text):
    assert extract_json(text) == {"verdict": "recovered"}


def test_extract_json_is_none_for_prose():
    assert extract_json("I could not finish.") is None


# ------------------------------------------------------------------ the provider client


def config(**overrides):
    base = dict(provider="mistral", base_url="https://api.mistral.ai/v1", api_key="k", model="mistral-large-latest",
                max_retries=2)
    base.update(overrides)
    return LLMConfig(**base)


def test_the_client_speaks_the_openai_shaped_wire_format_and_retries(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"message": "slow down"})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": "abc123xyz", "type": "function",
                            "function": {"name": "list_runs", "arguments": {"limit": 3}}}]}}]})

    client = ChatClient(config(), transport=httpx.MockTransport(handler))
    tools = [{"type": "function", "function": {"name": "list_runs", "description": "", "parameters": {}}}]
    reply = asyncio.run(client.chat([{"role": "user", "content": "hi"}], tools))
    assert len(requests) == 2
    sent = json.loads(requests[-1].content)
    assert requests[-1].url == "https://api.mistral.ai/v1/chat/completions"
    assert requests[-1].headers["authorization"] == "Bearer k"
    assert sent["model"] == "mistral-large-latest" and sent["tools"] == tools and sent["tool_choice"] == "auto"
    assert reply.tool_calls[0].name == "list_runs"
    assert json.loads(reply.tool_calls[0].arguments) == {"limit": 3}
    assert reply.as_message()["tool_calls"][0]["id"] == "abc123xyz"


def test_the_client_reports_a_refused_request():
    client = ChatClient(config(), transport=httpx.MockTransport(lambda r: httpx.Response(401, text="bad key")))
    with pytest.raises(LLMError, match="HTTP 401: bad key"):
        asyncio.run(client.chat([{"role": "user", "content": "hi"}], []))


def test_config_defaults_to_mistral_and_explains_what_is_missing():
    assert LLMConfig.from_env({"MISTRAL_API_KEY": "k"}).model == "ministral-14b-latest"
    with pytest.raises(LLMError, match="MISTRAL_API_KEY is not set"):
        LLMConfig.from_env({})
    with pytest.raises(LLMError, match="set CHAOSLAB_LLM_MODEL"):
        LLMConfig.from_env({"CHAOSLAB_LLM_PROVIDER": "groq", "GROQ_API_KEY": "k"})
    groq = LLMConfig.from_env({"CHAOSLAB_LLM_PROVIDER": "groq", "GROQ_API_KEY": "k", "CHAOSLAB_LLM_MODEL": "m"})
    assert groq.base_url == "https://api.groq.com/openai/v1"


def test_parse_reply_rejects_a_malformed_body():
    with pytest.raises(LLMError, match="malformed completion"):
        parse_reply({"error": "x"})


# ------------------------------------------------------------------ the commands


def test_analyze_writes_its_verdict_beside_the_lab_record(lab_root, settings):
    run_id = store_run(settings)
    verdict = {"experiment_id": run_id, "verdict": "recovered", "agrees_with_lab": True}
    llm = ScriptedLLM(calls(("get_recovery", {"experiment_id": run_id})), answer(verdict))
    code, payload = commands.analyze(lab_root, "proposal-000001", llm=llm)
    assert code == commands.EXIT_OK
    assert json.loads(open(payload["analysis_file"]).read()) == verdict
    assert payload["analysis_file"].startswith(str(settings.results_dir / run_id))
    assert json.loads(open(payload["transcript"]).read())["role"] == "analyst"
    assert "result.json" in {p.name for p in (settings.results_dir / run_id).iterdir()}


def test_inject_maps_a_denied_run_to_the_rejected_exit_code(lab_root, settings):
    llm = ScriptedLLM(calls(("run_experiment", {"definition": good_definition()})),
                      answer({"experiment_name": "proposal", "outcome": "denied", "experiment_id": None}))
    code, payload = commands.inject(lab_root, "proposal", deny_all, llm=llm)
    assert code == commands.EXIT_REJECTED
    assert payload["output"]["outcome"] == "denied"
    assert submissions(settings) == []


def test_analyze_does_not_repeat_a_baseline_it_never_compared(lab_root, settings):
    run_id = store_run(settings)
    baseline = store_run(settings, "20261010T000000Z-baseline-observe-000000")
    claimed = {"experiment_id": run_id, "verdict": "recovered", "baseline_compared": baseline}
    _, payload = commands.analyze(lab_root, run_id, llm=ScriptedLLM(answer(dict(claimed))))
    assert payload["output"]["baseline_compared"] is None
    assert payload["output"]["baseline_compared_unverified"] == baseline
    _, payload = commands.analyze(lab_root, run_id, llm=ScriptedLLM(
        calls(("compare_runs", {"first": baseline, "second": run_id})), answer(dict(claimed))))
    assert payload["output"]["baseline_compared"] == baseline


ASK_CHILD = """
import sys
sys.path.insert(0, {root!r})
from chaoslab.agents.runtime import ask_terminal
print("RESULT", ask_terminal("run_experiment", {{"name": "api-process-outage-10s"}}), flush=True)
"""


@pytest.mark.parametrize("typed, expected", [
    (["y\n"], True), (["\n"], False), (["no\n"], False), (["sure\n", "yes\n"], True),
])
def test_the_approval_prompt_works_on_a_real_terminal(typed, expected):
    """A person at a terminal is asked, and their answer decides: the prompt must not refuse on its own."""
    import os
    import pty
    import select
    import sys
    import time
    from pathlib import Path

    root = str(Path(__file__).resolve().parents[1])
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(sys.executable, [sys.executable, "-c", ASK_CHILD.format(root=root)])
    output = b""

    def read_for(seconds):
        nonlocal output
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if select.select([fd], [], [], 0.05)[0]:
                try:
                    output += os.read(fd, 4096)
                except OSError:
                    return

    try:
        for answer in typed:
            deadline = time.monotonic() + 10
            prompts = output.count(b"[y/N]")
            while output.count(b"[y/N]") == prompts and time.monotonic() < deadline:
                read_for(0.1)
            os.write(fd, answer.encode())
        deadline = time.monotonic() + 10
        while b"RESULT" not in output and time.monotonic() < deadline:
            read_for(0.1)
    finally:
        os.waitpid(pid, 0)
    assert b"Agent requests fault injection: run_experiment api-process-outage-10s" in output
    assert f"RESULT {expected}".encode() in output


def test_inject_reports_emergency_stop_from_the_tool_calls_not_the_model(lab_root):
    claims_it = answer({"experiment_name": "proposal", "outcome": "error", "emergency_stop_called": True})
    _, payload = commands.inject(lab_root, "proposal", deny_all, llm=ScriptedLLM(claims_it))
    assert payload["output"]["emergency_stop_called"] is False
    _, payload = commands.inject(lab_root, "proposal", deny_all, llm=ScriptedLLM(
        calls(("emergency_stop", {})), answer({"experiment_name": "proposal", "outcome": "error"})))
    assert payload["output"]["emergency_stop_called"] is True


def test_extract_json_tolerates_raw_newlines_and_trailing_commas():
    from chaoslab.agents.runtime import json_error

    assert extract_json('{"a": "line one\nline two", "b": [1, 2,],}') == {"a": "line one\nline two", "b": [1, 2]}
    assert extract_json("no json here") is None
    assert "line" in json_error('{"a": [1, 2}') and json_error("plain prose") == "the reply contains no JSON object"
