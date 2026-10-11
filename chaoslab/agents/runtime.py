"""The loop every lab agent runs: a model, the lab's MCP tools, and a person between the model and a real fault.

Three rules, enforced here rather than asked of the model:

An agent sees only its own tools. A role lists the tools it may use; the model is offered nothing else, and a call
to anything else is answered with a refusal rather than run.

A real fault waits for a person. `run_experiment` without `dry_run` goes to the approver first. The default
approver asks at the terminal; without one, the answer is no. Dry runs, reads and `emergency_stop` need nobody.

The policy still decides. The tools are the same MCP tools the CLI's contract serves, called in-process, so a
proposal the safety policy rejects is rejected here too, with its reasons.
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from chaoslab import proctitle
from chaoslab.agents.llm import ChatClient

RESULT_LIMIT = 12000

Approver = Callable[[str, dict], bool]
Progress = Callable[[str], None]


@dataclass(frozen=True)
class Role:
    name: str
    system_prompt: str
    tools: tuple[str, ...]
    max_rounds: int = 16


def needs_approval(tool: str, arguments: dict) -> bool:
    return tool == "run_experiment" and not arguments.get("dry_run")


def deny_all(tool: str, arguments: dict) -> bool:
    return False


def approve_all(tool: str, arguments: dict) -> bool:
    return True


def ask_terminal(tool: str, arguments: dict) -> bool:
    """Ask on the controlling terminal, so piped stdin/stdout cannot answer for the person."""
    subject = arguments.get("name") or (arguments.get("definition") or {}).get("name") or "an inline definition"
    try:
        # Separate handles: "r+" on a terminal fails as unseekable, which would read as "no terminal" and refuse.
        with open("/dev/tty") as tty_in, open("/dev/tty", "w") as tty:
            tty.write(f"\nAgent requests fault injection: {tool} {subject}\n")
            if arguments.get("definition"):
                tty.write(json.dumps(arguments["definition"], indent=2) + "\n")
            # Enter alone is no; anything else unrecognised asks again rather than quietly refusing.
            while True:
                tty.write("Proceed? [y/N] ")
                tty.flush()
                answer = tty_in.readline()
                if not answer:
                    return False
                answer = answer.strip().lower()
                if answer in ("y", "yes"):
                    return True
                if answer in ("", "n", "no"):
                    return False
    except OSError:
        return False


class ToolBox:
    """The lab's MCP tools, filtered to one role and called in-process."""

    def __init__(self, server: MCPServer, allowed: tuple[str, ...]):
        self.server = server
        self.allowed = allowed

    async def specs(self) -> list[dict]:
        tools = {t.name: t for t in await self.server.list_tools()}
        missing = [n for n in self.allowed if n not in tools]
        if missing:
            raise ValueError(f"role names tools the server does not have: {missing}")
        return [{"type": "function", "function": {"name": n, "description": tools[n].description or "",
                                                  "parameters": tools[n].input_schema}}
                for n in self.allowed]

    async def call(self, name: str, arguments: dict) -> str:
        try:
            result = await self.server.call_tool(name, arguments)
        except ToolError as exc:
            return f"ERROR: {exc}"
        if result.structured_content is not None:
            text = json.dumps(result.structured_content, default=str)
        else:
            text = "\n".join(getattr(c, "text", "") for c in result.content)
        if len(text) > RESULT_LIMIT:
            text = text[:RESULT_LIMIT] + f"\n[truncated: {len(text) - RESULT_LIMIT} more characters]"
        return text


@dataclass
class AgentRun:
    role: str
    task: str
    final_text: str = ""
    output: dict | None = None
    stopped: str = "answered"
    rounds: int = 0
    transcript: list[dict] = field(default_factory=list)
    #: The conversation so far, so a caller can continue it (validation feedback) instead of starting again.
    messages: list[dict] = field(default_factory=list, repr=False)

    def as_dict(self) -> dict:
        return {"role": self.role, "task": self.task, "stopped": self.stopped, "rounds": self.rounds,
                "output": self.output, "final_text": self.final_text, "transcript": self.transcript}


def extract_json(text: str) -> dict | None:
    """The last JSON object in a reply: the whole text, a fenced block, or the outermost braces."""
    candidates = [text.strip()]
    candidates += [m.strip() for m in re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)][::-1]
    if "{" in text and "}" in text:
        candidates.append(text[text.index("{"): text.rindex("}") + 1])
    for candidate in candidates:
        # Models put raw line breaks inside strings and leave trailing commas; accept both, nothing looser.
        for text in (candidate, re.sub(r",(\s*[}\]])", r"\1", candidate)):
            try:
                value = json.loads(text, strict=False)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(value, dict):
                return value
    return None


def json_error(text: str) -> str:
    """Why a reply is not a JSON object, for feedback to the model."""
    start = text.find("{")
    if start < 0:
        return "the reply contains no JSON object"
    try:
        json.loads(text[start:text.rfind("}") + 1], strict=False)
    except json.JSONDecodeError as exc:
        return f"invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}"
    return "the reply is not a single JSON object"


class Agent:
    def __init__(self, role: Role, llm: ChatClient, toolbox: ToolBox, approver: Approver = deny_all,
                 progress: Progress | None = None):
        self.role = role
        self.llm = llm
        self.toolbox = toolbox
        self.approver = approver
        self.progress = progress or (lambda line: None)

    async def run(self, task: str, resume: AgentRun | None = None, force_answer: bool = False) -> AgentRun:
        """Run a task, or continue `resume` with `task` as the next user message (its rounds count on).

        With force_answer, reaching the round limit asks once more for the final answer instead of giving up.
        """
        specs = await self.toolbox.specs()
        if resume is not None:
            run = resume
            run.output, run.final_text, run.stopped = None, "", "answered"
            messages = run.messages
            messages.append({"role": "user", "content": task})
            first = run.rounds + 1
        else:
            run = AgentRun(role=self.role.name, task=task)
            messages = [{"role": "system", "content": self.role.system_prompt}, {"role": "user", "content": task}]
            run.messages = messages
            first = 1
        for round_number in range(first, first + self.role.max_rounds):
            run.rounds = round_number
            provider = getattr(getattr(self.llm, "config", None), "provider", "the model")
            proctitle.agent(self.role.name, f"round {round_number} | asking {provider}")
            reply = await self.llm.chat(messages, specs)
            messages.append(reply.as_message())
            if reply.content:
                run.transcript.append({"round": round_number, "at": time.time(), "say": reply.content})
            if not reply.tool_calls:
                proctitle.agent(self.role.name, f"done after {round_number} rounds")
                run.final_text = reply.content
                run.output = extract_json(reply.content)
                return run
            for call in reply.tool_calls:
                result = await self._dispatch(call.name, call.arguments, round_number, run)
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": result})
        if force_answer:
            messages.append({"role": "user", "content": "Round limit reached. Reply now with only the final JSON "
                                                        "object in the required schema; do not call any tool."})
            reply = await self.llm.chat(messages, specs)
            messages.append(reply.as_message())
            run.rounds += 1
            run.output = extract_json(reply.content or "")
            if run.output is not None:
                run.final_text = reply.content
                run.stopped = "forced_answer"
                run.transcript.append({"round": run.rounds, "at": time.time(), "say": reply.content})
                return run
        proctitle.agent(self.role.name, f"stopped after {self.role.max_rounds} rounds")
        run.stopped = "max_rounds"
        self.progress(f"[{self.role.name}] stopped after {self.role.max_rounds} rounds without a final answer")
        return run

    async def _dispatch(self, name: str, raw_arguments: str, round_number: int, run: AgentRun) -> str:
        try:
            arguments = json.loads(raw_arguments or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as exc:
            result = f"ERROR: could not parse the arguments as a JSON object: {exc}"
            run.transcript.append({"round": round_number, "tool": name, "arguments": raw_arguments, "result": result})
            return result
        self.progress(f"[{self.role.name}] {name} {json.dumps(arguments, default=str)[:160]}")
        subject = _subject(arguments)
        if name not in self.role.tools:
            result = f"ERROR: {name} is not one of this agent's tools"
        elif needs_approval(name, arguments) and not self._approved(name, arguments, round_number, subject):
            result = "DENIED: the operator did not approve this fault injection. Do not retry it; report and stop."
        else:
            proctitle.agent(self.role.name, f"round {round_number} | {name} {subject}".rstrip())
            started = time.monotonic()
            result = await self.toolbox.call(name, arguments)
            self.progress(f"[{self.role.name}]   -> {result[:160]}  ({time.monotonic() - started:.1f}s)")
        run.transcript.append({"round": round_number, "at": time.time(), "tool": name, "arguments": arguments,
                               "result": result})
        return result

    def _approved(self, name: str, arguments: dict, round_number: int, subject: str) -> bool:
        proctitle.agent(self.role.name, f"round {round_number} | waiting for approval to inject {subject}")
        approved = self.approver(name, arguments)
        proctitle.agent(self.role.name, f"round {round_number} | {'approved' if approved else 'denied'} {subject}")
        return approved


def _subject(arguments: dict) -> str:
    """What a tool call is about, for the process title: an experiment name or id."""
    definition = arguments.get("definition")
    return str(arguments.get("name") or arguments.get("experiment_id")
               or (definition.get("name") if isinstance(definition, dict) else "") or "")


def save_transcript(directory: Path, run: AgentRun) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{run.role}.json"
    path.write_text(json.dumps(run.as_dict(), indent=2, default=str), encoding="utf-8")
    return path


def stderr_progress(line: str) -> None:
    sys.stderr.write(line + "\n")
