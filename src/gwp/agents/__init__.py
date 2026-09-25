"""The two model steps, as interfaces the orchestrator depends on.

This module does not import Strands. The Strands implementations live in
`gwp.agents.strands_agents`, and the orchestrator only sees these types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..cost import ModelCall
from ..retrieval import Retriever
from ..schema import Extraction


class ModelCallFailed(Exception):
    """A model step ended without usable output. `kind` is timeout, throttled, invalid_output or error."""

    def __init__(self, kind: str, call: ModelCall, detail: str = ""):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.call = call
        self.detail = detail


@dataclass
class ReaderResult:
    extraction: Extraction
    call: ModelCall


@dataclass
class ProposerInput:
    tenant_id: str
    document_id: str
    extraction: Extraction
    keyed_records: dict[str, Any]
    vendor_note: str
    retry_feedback: str | None = None


@dataclass
class ProposerResult:
    raw: dict | None  # the propose_write arguments exactly as the model sent them, or None
    call: ModelCall
    searches: list[dict] = field(default_factory=list)  # {"query": str, "hits": [{chunk_id, doc_id, version, score}]}
    tool_attempts: list[str] = field(default_factory=list)  # every tool name the model tried, allowed or not


class Reader(Protocol):
    def read(self, document_text: str, document_id: str, attempt: int) -> ReaderResult: ...


class Proposer(Protocol):
    def propose(self, inp: ProposerInput, retriever: Retriever | None, attempt: int) -> ProposerResult: ...
