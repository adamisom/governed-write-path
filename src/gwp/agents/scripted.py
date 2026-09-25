"""A scripted Strands model for offline runs.

It follows the approach of the SDK's own test double (MockedModelProvider in the
strands-agents test suite): the model is replaced, and the agent loop, hooks,
tools and metrics all run for real. Each call to `stream` replays the next
scripted turn as the stream events a provider would send.

A turn is one of:
    {"tool": name, "input": {...}}     the model calls a tool
    {"text": "..."}                    the model answers in text and ends its turn
    {"raise": "timeout" | "throttle"}  the call fails

Token usage is synthetic: characters divided by 4 for the request and the reply.
It exercises the cost code and is labeled synthetic in every report.
"""

from __future__ import annotations

import json
import math
from typing import Any, AsyncGenerator

from strands.models.model import Model
from strands.types.exceptions import ModelThrottledException


class ScriptExhausted(Exception):
    pass


def _chars(obj: Any) -> int:
    try:
        return len(json.dumps(obj, default=str))
    except (TypeError, ValueError):
        return len(str(obj))


class ScriptedModel(Model):
    def __init__(self, turns: list[dict], model_id: str = "scripted"):
        self.turns = list(turns)
        self.calls = 0
        self.config: dict[str, Any] = {"model_id": model_id}
        self.requests: list[dict] = []  # what the agent sent, for tests that check what a model saw

    def update_config(self, **model_config: Any) -> None:
        self.config.update(model_config)

    def get_config(self) -> dict:
        return self.config

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):  # type: ignore[override]
        raise NotImplementedError("the agent drives structured output through a tool call")
        yield  # pragma: no cover

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs) -> AsyncGenerator[dict, None]:  # type: ignore[override]
        self.requests.append({"messages": messages, "tools": [t["name"] for t in (tool_specs or [])],
                              "system_prompt": system_prompt})
        if self.calls >= len(self.turns):
            raise ScriptExhausted(f"no scripted turn left after {self.calls} calls")
        turn = self.turns[self.calls]
        self.calls += 1
        if "raise" in turn:
            if turn["raise"] == "throttle":
                raise ModelThrottledException("scripted throttle")
            raise TimeoutError("scripted timeout")

        in_tokens = math.ceil((_chars(messages) + _chars(system_prompt or "") + _chars(tool_specs or [])) / 4)
        yield {"messageStart": {"role": "assistant"}}
        if "tool" in turn:
            out_text = json.dumps(turn["input"])
            yield {"contentBlockStart": {"start": {"toolUse": {"name": turn["tool"],
                                                               "toolUseId": f"tooluse_{self.calls}"}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": out_text}}}}
            yield {"contentBlockStop": {}}
            stop = "tool_use"
        else:
            out_text = turn.get("text", "")
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": out_text}}}
            yield {"contentBlockStop": {}}
            stop = "end_turn"
        out_tokens = math.ceil(len(out_text) / 4)
        yield {"messageStop": {"stopReason": stop}}
        yield {"metadata": {"usage": {"inputTokens": in_tokens, "outputTokens": out_tokens,
                                      "totalTokens": in_tokens + out_tokens},
                            "metrics": {"latencyMs": 0}}}
