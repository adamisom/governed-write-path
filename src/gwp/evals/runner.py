"""Run one case end to end against a fresh, seeded store.

Offline, the store is DynamoDB mocked in-process by moto, and both model steps
are real Strands agents driving a scripted model. Live, only the models change.
"""

from __future__ import annotations

import os
import time
import traceback
from dataclasses import dataclass, field
from typing import Any

import boto3

from ..agents.scripted import ScriptedModel
from ..agents.strands_agents import StrandsProposer, StrandsReader
from ..blobs import MemoryBlobs
from ..executor import Executor, SimulatedCrash
from ..orchestrator import Orchestrator
from ..runtime import FakeClock, Ids
from ..schema import Principal, Role
from ..store import AUDIT, RECORDS, DynamoStore
from ..world import seed_world
from . import documents
from .cases import Case

UPLOADER = Principal(principal_id="user:uploader", role=Role.uploader)
APPROVER = Principal(principal_id="user:approver", role=Role.approver)

# Offline token counts are synthetic, priced as the models the scripts stand in for.
OFFLINE_READER_MODEL = "claude-haiku-4-5"
OFFLINE_PROPOSER_MODEL = "claude-sonnet-5"


@dataclass
class LiveConfig:
    provider: str
    reader_model_id: str
    proposer_model_id: str
    region: str | None = None
    search_enabled: bool = True  # False for the H2 ablation


@dataclass
class CaseRun:
    case_id: str
    category: str
    script: str
    mode: str
    trail: list[str] = field(default_factory=list)
    setup_trail: list[str] = field(default_factory=list)
    setup_runs: list[dict] = field(default_factory=list)
    proposer_prompts: list[str] = field(default_factory=list)  # what the proposer was shown in the graded steps
    final_outcome: str | None = None
    final_reason: str | None = None
    reasons: list[str] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)
    audits: list[dict] = field(default_factory=list)
    base: dict = field(default_factory=dict)
    final: dict = field(default_factory=dict)
    human_tasks: list[dict] = field(default_factory=list)
    elapsed_ms: int = 0
    harness_error: str | None = None


def snapshot(store: DynamoStore) -> dict:
    """The graded state: every record in both tables, keyed by (pk, sk)."""
    snap: dict[str, dict] = {}
    for item in store.scan_all(RECORDS):
        snap[f"{item['pk']}|{item['sk']}"] = item
    for item in store.scan_all(AUDIT):
        snap[f"AUDIT|{item['pk']}|{item['sk']}"] = item
    return snap


def _proposer_turns(turns: list[dict]) -> list[dict]:
    out = []
    for t in turns:
        if "if_seen" in t:
            out.append({"if_seen": t["if_seen"], "then": _proposer_turns([t["then"]])[0],
                        "else": _proposer_turns([t["else"]])[0]})
        elif "search" in t:
            out.append({"tool": "search_policy", "input": {"query": t["search"]}})
        elif "propose" in t:
            out.append({"tool": "propose_write", "input": {"proposals": t["propose"]}})
        elif "propose_raw" in t:
            out.append({"tool": "propose_write", "input": t["propose_raw"]})
        elif "tool" in t:
            out.append({"tool": t["tool"], "input": t.get("input", {})})
        else:
            out.append(t)  # {"raise": ...} or {"text": ...}
    return out


def _normalize_step(step: Any) -> tuple[str, Any]:
    if isinstance(step, str):
        return step, None
    (name, arg), = step.items()
    return name, arg


class _Harness:
    def __init__(self, case: Case, script: str, mode: str, live: LiveConfig | None):
        self.case = case
        self.script = script
        self.mode = mode
        client = boto3.client("dynamodb", region_name="us-east-1")
        self.store = DynamoStore(client)
        self.store.create_tables()
        seed_world(self.store, case.overrides)
        self.clock = FakeClock()
        self.ids = Ids()
        self.crash_armed = False
        self.executor = Executor(self.store, self.clock, self.ids, after_commit=self._maybe_crash)
        if live is None:
            self.reader_model = ScriptedModel([], OFFLINE_READER_MODEL)
            self.proposer_model = ScriptedModel([], OFFLINE_PROPOSER_MODEL)
            reader = StrandsReader(self.reader_model, OFFLINE_READER_MODEL, synthetic=True)
            proposer = StrandsProposer(self.proposer_model, OFFLINE_PROPOSER_MODEL, synthetic=True)
        else:
            from ..agents.strands_agents import live_model

            self.reader_model = self.proposer_model = None
            reader = StrandsReader(live_model(live.provider, live.reader_model_id, region=live.region),
                                   live.reader_model_id)
            proposer = StrandsProposer(live_model(live.provider, live.proposer_model_id, region=live.region),
                                       live.proposer_model_id, search_enabled=live.search_enabled)
        self.proposer = proposer
        self.blobs = MemoryBlobs()
        self.orch = Orchestrator(self.store, self.blobs, reader, proposer, self.clock, self.ids, self.executor,
                                 search_enabled=live.search_enabled if live else True)
        self.current_run: str | None = None
        self.current_doc = "main"
        self.process_index = 0
        self.run_ids: list[str] = []

    def proposer_prompts(self) -> list[str]:
        """Every prompt the proposer has been shown so far, oldest first."""
        return list(self.proposer.prompts)

    def _maybe_crash(self, audit_id: str) -> None:
        if self.crash_armed:
            self.crash_armed = False
            raise SimulatedCrash(audit_id)

    def _queue_turns(self) -> None:
        if self.reader_model is None:
            return
        rs = self.case.run_script(self.process_index, self.script)
        faults = [{"raise": f} for f in rs.get("reader_faults", [])]
        extraction = documents.faithful_extraction(self.case.doc_spec(self.current_doc))
        extraction.update(rs.get("reader", {}) or {})
        self.reader_model.turns.extend(faults + [{"tool": "Extraction", "input": extraction}])
        self.proposer_model.turns.extend(_proposer_turns(rs.get("proposer", [])))

    def step(self, name: str, arg: Any) -> list[str]:
        t = self.case.tenant
        if name == "upload":
            self.current_doc = arg or "main"
            pdf = documents.render(self.case.doc_spec(self.current_doc))
            res = self.orch.upload(t, pdf, "application/pdf", UPLOADER)
            self.current_run = res.run_id
            if res.run_id not in self.run_ids:
                self.run_ids.append(res.run_id)
            return [f"upload:{res.outcome}"] if res.duplicate else []
        if name == "process":
            self._queue_turns()
            self.process_index += 1
            if isinstance(arg, dict) and arg.get("crash_after_commit"):
                self.crash_armed = True
            try:
                res = self.orch.process(t, self.current_run)
            except SimulatedCrash:
                return ["process:CRASHED"]
            return [f"process:{res.outcome}"]
        if name == "redeliver":
            res = self.orch.resume(t, self.current_run)
            return [f"redeliver:{res.outcome}"]
        if name == "approve":
            opts = arg if isinstance(arg, dict) else {"decision": arg}
            out = []
            for _ in range(opts.get("times", 1)):
                pending = [a for a in self.store.list_audits(t, self.current_run)
                           if a["status"] in ("pending_approval", "approved", "applied", "declined")
                           and a["tier"] == "approval"]
                for a in sorted(pending, key=lambda a: a["audit_id"]):
                    # crash_after_commit: the worker dies after the approved write commits (case D12).
                    self.crash_armed = bool(opts.get("crash_after_commit"))
                    try:
                        res = self.orch.approve(t, a["audit_id"], APPROVER, opts.get("decision"))
                    except SimulatedCrash:
                        out.append("approve:CRASHED")
                        continue
                    finally:
                        self.crash_armed = False
                    out.append(f"approve:{res.status}")
            return out
        if name == "revert":
            opts = arg if isinstance(arg, dict) else {}
            out = []
            for _ in range(opts.get("times", 1)):
                if opts.get("audit"):
                    targets = [opts["audit"]]
                else:
                    targets = [a["audit_id"] for a in self.store.list_audits(t, self.current_run)
                               if a["action"] not in ("request_human_review",)]
                for aid in sorted(targets):
                    res = self.orch.revert(t, aid, APPROVER)
                    out.append(f"revert:{res.outcome}" + (f":{res.reason}" if res.reason else ""))
            return out
        if name == "deliver_outbox":
            sent = self.executor.deliver_outbox(t)
            return [f"deliver:{len(sent)}"]
        raise ValueError(f"unknown step {name}")


def run_case(case: Case, script: str = "cooperative", mode: str = "offline", live: LiveConfig | None = None,
             harness: type[_Harness] | None = None) -> CaseRun:
    """Run one case. `harness` swaps how the steps are driven, e.g. through the MCP server (`evals.mcp_runner`)."""
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    from moto import mock_aws

    result = CaseRun(case.id, case.category, script, mode)
    t0 = time.monotonic()
    with mock_aws():
        h = (harness or _Harness)(case, script, mode, live)
        t = case.tenant
        setup_run_ids: list[str] = []
        prompts_before = 0
        try:
            for step in case.setup:
                result.setup_trail.extend(h.step(*_normalize_step(step)))
            setup_run_ids, h.run_ids = h.run_ids, []
            result.base = snapshot(h.store)
            prompts_before = len(h.proposer_prompts())
            for step in case.steps:
                result.trail.extend(h.step(*_normalize_step(step)))
        except Exception:  # a harness or orchestrator bug; graded as a failure
            result.harness_error = traceback.format_exc(limit=8)
            result.base = result.base or snapshot(h.store)
        result.final = snapshot(h.store)
        result.proposer_prompts = h.proposer_prompts()[prompts_before:]
        result.setup_runs = [h.store.get_run(t, rid) or {} for rid in setup_run_ids]
        result.runs = [h.store.get_run(t, rid) or {} for rid in h.run_ids]
        seeded = {k for k in result.base if k.startswith("AUDIT|")}
        result.audits = sorted((v for k, v in result.final.items() if k.startswith("AUDIT|") and k not in seeded),
                               key=lambda a: a["audit_id"])
        result.human_tasks = h.store.list_human_tasks(t)
        last = result.runs[-1] if result.runs else {}
        result.final_outcome = last.get("outcome")
        result.final_reason = last.get("reason")
        result.reasons = last.get("reasons", [])
    result.elapsed_ms = int((time.monotonic() - t0) * 1000)
    return result
