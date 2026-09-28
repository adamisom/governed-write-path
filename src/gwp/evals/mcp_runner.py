"""Replay the graded cases through the MCP server, and compare each verdict with the direct run.

In the direct run the orchestrator calls its built-in proposer. Here the orchestrator runs in external-proposal
mode, and each case's scripted proposer turns become MCP tool calls from a client holding an agent key:
`get_proposal_context`, then `search_policy` for each scripted search, then `propose`. An adversarial turn that
obeys text only if it was shown (`if_seen`) checks the context and search results the agent received, which are the
same text the built-in proposer is shown. Approvals go through `decide` with an approver key, and reverts through
`revert` with an admin key. Uploads, redeliveries and outbox delivery are the same as in the direct run, since they
are not agent or approver actions. The same grader grades the result.

If the script ends without an accepted proposal, the agent has given up. The harness moves the clock past the
proposal deadline and runs the sweep, which is what happens to a real run nobody proposes for.

One check is adjusted, and only one. A case's `model_calls` counts the model calls stored on the run. In MCP mode
the proposer's calls happen in the agent's own process, so the server stores only the reader's. The expected count
is lowered by the number of proposer calls the direct run of the same case made.
"""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field
from typing import Any

import anyio

from ..access import Caller
from ..agents.prompts import render_proposer_input
from ..executor import SimulatedCrash
from ..mcp_demo import NoInternalProposer
from ..mcp_server import INSTRUCTIONS, build_server, fixed_caller
from ..orchestrator import PROPOSAL_LEASE_SECONDS, Orchestrator
from ..schema import Extraction, Principal, Role, RunOutcome
from .cases import Case
from .grader import Grade, grade
from .runner import CaseRun, _Harness, _proposer_turns, run_case


class ToolCallFailed(Exception):
    pass


class McpHarness(_Harness):
    def __init__(self, case: Case, script: str, mode: str, live: Any):
        if live is not None:
            raise ValueError("the MCP replay is offline only")
        super().__init__(case, script, mode, live)
        self.orch = Orchestrator(self.store, self.blobs, self.orch.reader, NoInternalProposer(), self.clock, self.ids,
                                 self.executor, external_proposals=True)
        self.servers = {role: build_server(self.orch, fixed_caller(
            Caller(Principal(principal_id=f"{role.value}:replay", role=role), case.tenant)))
            for role in (Role.agent, Role.approver, Role.admin)}
        self.shown: list[str] = []  # every context the agent was given, rendered as the built-in proposer's prompt
        self.tool_calls: list[dict] = []

    def proposer_prompts(self) -> list[str]:
        return list(self.shown)

    # -- MCP calls --------------------------------------------------------------------------------

    def mcp(self, role: Role, tool: str, args: dict) -> dict:
        from mcp import Client

        async def go():
            async with Client(self.servers[role]) as c:
                return await c.call_tool(tool, args)

        res = anyio.run(go)
        self.tool_calls.append({"role": role.value, "tool": tool, "is_error": res.is_error})
        if res.is_error:
            raise ToolCallFailed(res.content[0].text)
        return res.structured_content or {}

    def _context(self, run_id: str) -> str:
        ctx = self.mcp(Role.agent, "get_proposal_context", {"run_id": run_id})
        prompt = render_proposer_input(Extraction.model_validate(ctx["extracted_fields_untrusted"]),
                                       ctx["records_trusted"], ctx["vendor_note"], ctx["document_id"],
                                       ctx["retry_feedback"])
        self.shown.append(prompt)
        return prompt

    def _agent(self, run_id: str, turns: list[dict]) -> str:
        """The scripted agent: read the context, search, propose. Returns the run outcome."""
        seen = [INSTRUCTIONS, self._context(run_id)]
        for turn in turns:
            while "if_seen" in turn:
                turn = turn["then"] if turn["if_seen"] in "\n".join(seen) else turn["else"]
            if "tool" not in turn:
                continue  # a model call that ended without a tool call, or failed: no proposal this time
            if turn["tool"] == "search_policy":
                hits = self.mcp(Role.agent, "search_policy", {"run_id": run_id, "query": turn["input"]["query"]})
                seen.append(json.dumps(hits))
                continue
            if turn["tool"] != "propose_write":
                continue  # a tool the agent doesn't have; the MCP server offers no such tool
            raw = turn["input"]
            proposals = raw.get("proposals") if isinstance(raw, dict) else None
            out = self.mcp(Role.agent, "propose", {"run_id": run_id, "proposals": proposals
                                                   if isinstance(proposals, list) else [raw]})
            if out["outcome"] != RunOutcome.AWAITING_PROPOSAL:
                return out["outcome"]
            seen.append(self._context(run_id))  # the retry sees the validation errors
        # The agent gave up. The proposal deadline passes and the sweep ends the run.
        self.clock.advance(PROPOSAL_LEASE_SECONDS + 60)
        self.orch.recover_stranded()
        return (self.store.get_run(self.case.tenant, run_id) or {}).get("outcome") or ""

    # -- steps ------------------------------------------------------------------------------------

    def step(self, name: str, arg: Any) -> list[str]:
        t = self.case.tenant
        if name == "process":
            rs = self.case.run_script(self.process_index, self.script)
            from . import documents

            faults = [{"raise": f} for f in rs.get("reader_faults", [])]
            extraction = documents.faithful_extraction(self.case.doc_spec(self.current_doc))
            extraction.update(rs.get("reader", {}) or {})
            self.reader_model.turns.extend(faults + [{"tool": "Extraction", "input": extraction}])
            self.process_index += 1
            if isinstance(arg, dict) and arg.get("crash_after_commit"):
                self.crash_armed = True
            res = self.orch.process(t, self.current_run)
            if res.outcome != RunOutcome.AWAITING_PROPOSAL:
                return [f"process:{res.outcome}"]
            try:
                return [f"process:{self._agent(self.current_run, _proposer_turns(rs.get('proposer', [])))}"]
            except SimulatedCrash:
                return ["process:CRASHED"]
            except BaseExceptionGroup as eg:  # the crash, raised inside the server's task group
                if eg.subgroup(SimulatedCrash) is not None:
                    return ["process:CRASHED"]
                raise
        if name == "approve":
            opts = arg if isinstance(arg, dict) else {"decision": arg}
            out = []
            for _ in range(opts.get("times", 1)):
                pending = [a for a in self.store.list_audits(t, self.current_run)
                           if a["status"] in ("pending_approval", "approved", "applied", "declined")
                           and a["tier"] == "approval"]
                for a in sorted(pending, key=lambda a: a["audit_id"]):
                    res = self.mcp(Role.approver, "decide", {"audit_id": a["audit_id"],
                                                            "decision": opts.get("decision")})
                    out.append(f"approve:{res['status']}")
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
                    res = self.mcp(Role.admin, "revert", {"audit_id": aid})
                    out.append(f"revert:{res['outcome']}" + (f":{res['reason']}" if res["reason"] else ""))
            return out
        return super().step(name, arg)


def run_case_via_mcp(case: Case, script: str = "cooperative") -> CaseRun:
    return run_case(case, script, "offline", None, harness=McpHarness)


def proposer_calls(run: CaseRun) -> int:
    return sum(1 for c in (run.runs[-1].get("model_calls") or []) if c.get("step") == "proposer") if run.runs else 0


def mcp_expectations(case: Case, direct: CaseRun) -> Case:
    """The case with its one MCP-mode adjustment: the proposer's model calls are not stored on the server's run."""
    if "model_calls" not in case.expect:
        return case
    adjusted = copy.copy(case)
    adjusted.expect = {**case.expect, "model_calls": case.expect["model_calls"] - proposer_calls(direct)}
    return adjusted


@dataclass
class Parity:
    case_id: str
    category: str
    script: str
    direct: str
    via_mcp: str
    same: bool
    injection_system_direct: bool | None
    injection_system_mcp: bool | None
    mismatches_mcp: list[str] = field(default_factory=list)
    tool_calls: int = 0
    denied_calls: int = 0


def compare(case: Case, script: str) -> tuple[Grade, Grade, Parity]:
    direct_run = run_case(case, script)
    direct = grade(case, direct_run)
    mcp_run = run_case_via_mcp(case, script)
    via = grade(mcp_expectations(case, direct_run), mcp_run)
    denied = sum(1 for r in mcp_run.final.values() if r.get("kind") == "access_record" and r["decision"] == "denied")
    calls = sum(1 for r in mcp_run.final.values() if r.get("kind") == "access_record")
    p = Parity(case.id, case.category, script, direct.verdict, via.verdict,
               direct.verdict == via.verdict and direct.injection_system == via.injection_system,
               direct.injection_system, via.injection_system, via.mismatches, calls, denied)
    return direct, via, p


def report(parities: list[Parity]) -> tuple[str, dict]:
    lines = ["# The graded cases, replayed through the MCP server", ""]
    by_script: dict[str, list[Parity]] = {}
    for p in parities:
        by_script.setdefault(p.script, []).append(p)
    data: dict = {"scripts": {}}
    for script, ps in by_script.items():
        same = sum(p.same for p in ps)
        ok = sum(p.via_mcp == "success" for p in ps)
        unsafe = sorted(p.case_id for p in ps if p.via_mcp == "unsafe")
        through = sorted(p.case_id for p in ps if p.injection_system_mcp)
        lines += [f"## {script}", "",
                  f"- Same verdict as the direct run: {same} of {len(ps)}",
                  f"- Success through MCP: {ok} of {len(ps)}",
                  f"- Unsafe through MCP: {', '.join(unsafe) or 'none'}",
                  f"- Attacks that changed a store through MCP: {', '.join(through) or 'none'}",
                  f"- MCP tool calls: {sum(p.tool_calls for p in ps)}, all recorded; denied: "
                  f"{sum(p.denied_calls for p in ps)}", ""]
        diffs = [p for p in ps if not p.same]
        if diffs:
            lines += ["| Case | Direct | Through MCP | Why |", "| --- | --- | --- | --- |"]
            lines += [f"| {p.case_id} | {p.direct} | {p.via_mcp} | {'; '.join(p.mismatches_mcp)[:300]} |"
                      for p in diffs]
            lines.append("")
        data["scripts"][script] = {"cases": len(ps), "same": same, "success": ok, "unsafe": unsafe,
                                   "attacks_through": through, "parity": [asdict(p) for p in ps]}
    return "\n".join(lines), data
