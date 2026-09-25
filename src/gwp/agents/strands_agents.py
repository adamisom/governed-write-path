"""The reader and the proposer as Strands agents.

The reader is quarantined: it gets the document text and no tools. Strands
implements structured output as a tool named after the output model, so the
reader's only callable is `Extraction`, and a hook cancels any other tool call.

The proposer never sees document text. It gets the typed extraction and the
records code looked up, and it has two tools:

- `search_policy(query)`, read-only, bound by code to one tenant's retriever,
- `propose_write(proposals)`, which records the arguments and returns. It does
  not write anything and does not validate: validation is the orchestrator's
  job, in plain code, so the rules do not depend on the harness.

A before-tool-call hook enforces the allowlist and allows only one
propose_write per run. An after-tools hook ends the agent's turn as soon as
propose_write has been called, so the model can't keep going.

Each call builds a fresh Agent, with Strands' own retries turned off: the
orchestrator owns the retry policy (one retry with backoff).
"""

from __future__ import annotations

import asyncio
import copy
import math
import time
from typing import Any

from strands import Agent, tool
from strands.hooks import AfterToolsEvent, BeforeToolCallEvent
from strands.types._events import ToolResultEvent
from strands.types.exceptions import ModelThrottledException, StructuredOutputException
from strands.types.tools import AgentTool, ToolGenerator, ToolSpec, ToolUse

from ..cost import make_call
from ..retrieval import Retriever
from ..schema import Extraction, ProposalSet
from . import ModelCallFailed, ProposerInput, ProposerResult, ReaderResult
from .prompts import (
    PROPOSER_PROMPT_VERSION,
    PROPOSER_SYSTEM,
    READER_PROMPT_VERSION,
    READER_SYSTEM,
    render_proposer_input,
    spotlight,
)

PROPOSER_TOOLS = frozenset({"search_policy", "propose_write"})


def inline_refs(schema: dict) -> dict:
    """Inline $ref/$defs and drop pydantic's `discriminator` keyword, for providers that want a plain schema."""
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                return walk(copy.deepcopy(defs[name]))
            return {k: walk(v) for k, v in node.items() if k not in ("$defs", "discriminator", "title")}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def proposal_input_schema() -> dict:
    return inline_refs(ProposalSet.model_json_schema())


class ProposeWriteTool(AgentTool):
    """Records the model's proposal and returns. It never writes and never validates."""

    def __init__(self) -> None:
        super().__init__()
        self.recorded: dict | None = None
        self._spec: ToolSpec = {
            "name": "propose_write",
            "description": ("Propose one to three writes for this document. This only records a proposal. Code "
                            "checks it, sets its approval tier and applies it. Call it exactly once."),
            "inputSchema": {"json": proposal_input_schema()},
        }

    @property
    def tool_name(self) -> str:
        return "propose_write"

    @property
    def tool_spec(self) -> ToolSpec:
        return self._spec

    @property
    def tool_type(self) -> str:
        return "python"

    async def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any) -> ToolGenerator:
        self.recorded = copy.deepcopy(tool_use.get("input") or {})
        yield ToolResultEvent({
            "toolUseId": str(tool_use.get("toolUseId", "")),
            "status": "success",
            "content": [{"text": "Proposal recorded. Code will check it. Your part of this run is done."}],
        })


def make_search_tool(retriever: Retriever, log: list[dict]):
    @tool(name="search_policy")
    def search_policy(query: str) -> str:
        """Search this company's written accounts payable policy. Returns up to three passages with their ids."""
        q = str(query)[:200]
        hits = retriever.search(q, k=3)
        log.append({"query": q, "hits": [{"chunk_id": h.chunk_id, "doc_id": h.doc_id, "version": h.version,
                                          "score": h.score} for h in hits]})
        if not hits:
            return "No matching policy text."
        return "\n\n".join(f"[{h.chunk_id}] {h.text}" for h in hits)

    return search_policy


def _classify(exc: BaseException) -> str:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(exc, ModelThrottledException):
        return "throttled"
    if isinstance(exc, StructuredOutputException):
        return "invalid_output"
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "timeout"
    if "throttl" in name or "ratelimit" in name:
        return "throttled"
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        return _classify(cause)
    return "error"


class _StrandsStep:
    def __init__(self, model: Any, model_id: str, synthetic: bool, budget_s: float):
        self.model = model
        self.model_id = model_id
        self.synthetic = synthetic
        self.budget_s = budget_s

    def _run(self, agent: Agent, prompt: str, **kwargs: Any):
        async def go():
            return await asyncio.wait_for(agent.invoke_async(prompt, **kwargs), timeout=self.budget_s)
        return asyncio.run(go())

    def _estimate(self, *texts: str) -> dict:
        n = math.ceil(sum(len(t) for t in texts) / 4)
        return {"inputTokens": n, "outputTokens": 0, "totalTokens": n}


class StrandsReader(_StrandsStep):
    """Quarantined reader: document text in, a validated Extraction out, no tools."""

    def __init__(self, model: Any, model_id: str, synthetic: bool = False, budget_s: float = 30.0):
        super().__init__(model, model_id, synthetic, budget_s)

    def read(self, document_text: str, document_id: str, attempt: int) -> ReaderResult:
        prompt, _ = spotlight(document_text, document_id)
        attempted: list[str] = []

        def allow_only_output(event: BeforeToolCallEvent) -> None:
            name = event.tool_use["name"]
            attempted.append(name)
            if name != "Extraction":
                event.cancel_tool = "The reader has no tools."

        agent = Agent(model=self.model, tools=[], system_prompt=READER_SYSTEM, callback_handler=None,
                      retry_strategy=None, hooks=[allow_only_output])
        t0 = time.monotonic()
        try:
            result = self._run(agent, prompt, structured_output_model=Extraction)
        except BaseException as exc:  # noqa: BLE001 - every failure ends as a recorded, typed failure
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            kind = _classify(exc)
            usage = dict(agent.event_loop_metrics.accumulated_usage)
            synthetic = self.synthetic
            if not usage.get("inputTokens"):
                usage, synthetic = self._estimate(READER_SYSTEM, prompt), True
            call = make_call("reader", attempt, self.model_id, READER_PROMPT_VERSION, usage,
                             int((time.monotonic() - t0) * 1000), kind, synthetic)
            raise ModelCallFailed(kind, call, repr(exc)) from exc
        usage = dict(result.metrics.accumulated_usage)
        call = make_call("reader", attempt, self.model_id, READER_PROMPT_VERSION, usage,
                         int((time.monotonic() - t0) * 1000), "ok", self.synthetic, result.metrics.cycle_count)
        ext = result.structured_output
        if not isinstance(ext, Extraction):
            call.status = "invalid_output"
            raise ModelCallFailed("invalid_output", call, "no structured output")
        return ReaderResult(ext, call)


class StrandsProposer(_StrandsStep):
    """Proposer: typed fields and trusted records in, a raw proposal out. Never sees document text."""

    def __init__(self, model: Any, model_id: str, synthetic: bool = False, budget_s: float = 60.0,
                 search_enabled: bool = True):
        super().__init__(model, model_id, synthetic, budget_s)
        self.search_enabled = search_enabled

    def propose(self, inp: ProposerInput, retriever: Retriever | None, attempt: int) -> ProposerResult:
        propose_tool = ProposeWriteTool()
        searches: list[dict] = []
        attempted: list[str] = []
        tools: list[Any] = [propose_tool]
        if self.search_enabled and retriever is not None:
            tools.insert(0, make_search_tool(retriever, searches))

        def allowlist(event: BeforeToolCallEvent) -> None:
            name = event.tool_use["name"]
            attempted.append(name)
            if name not in PROPOSER_TOOLS:
                event.cancel_tool = f"Tool {name} is not available."
            elif name == "search_policy" and not self.search_enabled:
                event.cancel_tool = "Search is disabled for this run."
            elif name == "propose_write" and propose_tool.recorded is not None:
                event.cancel_tool = "propose_write was already called once."

        def stop_after_proposal(event: AfterToolsEvent) -> None:
            if propose_tool.recorded is not None:
                event.end_turn = "Proposal recorded."

        prompt = render_proposer_input(inp.extraction, inp.keyed_records, inp.vendor_note, inp.document_id,
                                       inp.retry_feedback)
        agent = Agent(model=self.model, tools=tools, system_prompt=PROPOSER_SYSTEM, callback_handler=None,
                      retry_strategy=None, hooks=[allowlist, stop_after_proposal])
        t0 = time.monotonic()
        try:
            result = self._run(agent, prompt)
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            kind = _classify(exc)
            usage = dict(agent.event_loop_metrics.accumulated_usage)
            synthetic = self.synthetic
            if not usage.get("inputTokens"):
                usage, synthetic = self._estimate(PROPOSER_SYSTEM, prompt), True
            call = make_call("proposer", attempt, self.model_id, PROPOSER_PROMPT_VERSION, usage,
                             int((time.monotonic() - t0) * 1000), kind, synthetic)
            if propose_tool.recorded is not None and kind == "error":
                # The proposal was recorded before the failure; keep it, code will validate it.
                return ProposerResult(propose_tool.recorded, call, searches, attempted)
            raise ModelCallFailed(kind, call, repr(exc)) from exc
        usage = dict(result.metrics.accumulated_usage)
        call = make_call("proposer", attempt, self.model_id, PROPOSER_PROMPT_VERSION, usage,
                         int((time.monotonic() - t0) * 1000), "ok", self.synthetic, result.metrics.cycle_count)
        return ProposerResult(propose_tool.recorded, call, searches, attempted)


# ---------------------------------------------------------------------------
# Live models. Written, never run here: there are no credentials on this machine.
# ---------------------------------------------------------------------------

DEFAULT_MODELS = {
    # Research (2026-09-25): Haiku 4.5 as the reader and Sonnet 5 as the proposer, about $0.04 a run.
    "anthropic": {"reader": "claude-haiku-4-5", "proposer": "claude-sonnet-5"},
    # Bedrock needs a global or geo inference profile id. Check the ids on the Bedrock model cards before use.
    "bedrock": {"reader": "global.anthropic.claude-haiku-4-5", "proposer": "global.anthropic.claude-sonnet-5"},
}


def live_model(provider: str, model_id: str, max_tokens: int = 4096, region: str | None = None) -> Any:
    """Build a Strands model for a live run. No sampling parameters: Sonnet 5 rejects temperature."""
    if provider == "anthropic":
        from strands.models.anthropic import AnthropicModel  # needs the `live` extra

        return AnthropicModel(model_id=model_id, max_tokens=max_tokens)
    if provider == "bedrock":
        from strands.models.bedrock import BedrockModel

        return BedrockModel(model_id=model_id, max_tokens=max_tokens, region_name=region or "us-east-1")
    raise ValueError(f"unknown provider {provider}")
