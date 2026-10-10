"""One chat client for every OpenAI-compatible provider the agents may use.

Mistral and Groq both serve `/chat/completions` with OpenAI-shaped tool calls, so a provider is a base URL, the
variable its key is read from, and a default model. Mistral is the default. Nothing here knows about tools beyond
passing their schemas through and handing back the calls the model made.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

PROVIDERS = {
    # The free tier gives larger Mistral models a quota of zero requests a minute (checked 2026-10-10);
    # ministral-14b is the strongest one it serves. A paid key can name a larger one in CHAOSLAB_LLM_MODEL.
    "mistral": {"base_url": "https://api.mistral.ai/v1", "key_env": "MISTRAL_API_KEY",
                "model": "ministral-14b-latest"},
    # No default model: Groq's catalogue moves quickly, so the caller names one in CHAOSLAB_LLM_MODEL.
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY", "model": None},
}
RETRY_STATUS = {429, 500, 502, 503, 504}


class LLMError(Exception):
    pass


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    base_url: str
    api_key: str
    model: str
    timeout_s: float = 60.0
    max_retries: int = 4
    temperature: float = 0.2

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> LLMConfig:
        env = os.environ if environ is None else environ
        provider = env.get("CHAOSLAB_LLM_PROVIDER", "mistral").lower()
        if provider not in PROVIDERS:
            raise LLMError(f"unknown provider {provider!r}; choose one of {sorted(PROVIDERS)}")
        preset = PROVIDERS[provider]
        key = env.get(preset["key_env"], "")
        if not key:
            raise LLMError(f"{preset['key_env']} is not set; export it to use the {provider} agents")
        model = env.get("CHAOSLAB_LLM_MODEL") or preset["model"]
        if not model:
            raise LLMError(f"set CHAOSLAB_LLM_MODEL to the {provider} model the agents should use")
        return cls(provider=provider, base_url=env.get("CHAOSLAB_LLM_BASE_URL", preset["base_url"]).rstrip("/"),
                   api_key=key, model=model)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class Reply:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)

    def as_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [{"id": c.id, "type": "function",
                                      "function": {"name": c.name, "arguments": c.arguments}}
                                     for c in self.tool_calls]
        return message


def parse_reply(body: dict) -> Reply:
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"malformed completion: {json.dumps(body)[:300]}") from exc
    calls = []
    for raw in message.get("tool_calls") or []:
        function = raw.get("function") or {}
        arguments = function.get("arguments", "{}")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments)
        calls.append(ToolCall(id=raw.get("id", ""), name=function.get("name", ""), arguments=arguments))
    return Reply(content=message.get("content") or "", tool_calls=calls)


class ChatClient:
    def __init__(self, config: LLMConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._transport = transport

    async def chat(self, messages: list[dict], tools: list[dict]) -> Reply:
        payload: dict[str, Any] = {"model": self.config.model, "messages": messages,
                                   "temperature": self.config.temperature}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        url = f"{self.config.base_url}/chat/completions"
        async with httpx.AsyncClient(timeout=self.config.timeout_s, transport=self._transport) as client:
            for attempt in range(self.config.max_retries + 1):
                try:
                    response = await client.post(url, json=payload, headers=headers)
                except httpx.TransportError as exc:
                    if attempt == self.config.max_retries:
                        raise LLMError(f"{self.config.provider} unreachable: {exc}") from exc
                    await asyncio.sleep(2 ** attempt)
                    continue
                if response.status_code in RETRY_STATUS and attempt < self.config.max_retries:
                    await asyncio.sleep(_retry_after(response) or 2 ** attempt)
                    continue
                if response.status_code != 200:
                    raise LLMError(f"{self.config.provider} returned HTTP {response.status_code}: "
                                   f"{response.text[:300]}")
                return parse_reply(response.json())
        raise LLMError("unreachable")


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return min(float(response.headers["retry-after"]), 60.0)
    except (KeyError, ValueError):
        return None
