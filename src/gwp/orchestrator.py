"""The orchestrator: ordinary code with an explicit sequence of steps. It decides every next step.

The model never chooses a tier, never chooses the next step, and never calls a
write. Steps 3 (read) and 6 (propose) are the only model calls; everything else
is here or in `policy` and `executor`.

A failure fails closed: a model timeout, a throttle, an invalid output or an
unexpected exception ends the run as NEEDS_HUMAN, never as an auto-apply. An
exception before step 12 leaves no write. An exception after one write of a set
committed leaves that write in place, audited as applied, and the run lists it
in `applied_audit_ids`; `resume` then finishes the rest of the set.

This module imports no agent framework. It talks to the models only through the
`Reader` and `Proposer` interfaces in `gwp.agents`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from typing import get_args

from botocore.exceptions import ClientError
from pydantic import BaseModel, ValidationError

from .agents import ModelCallFailed, Proposer, ProposerInput, Reader
from .blobs import BlobStore
from .executor import Executor, execution_key
from .pdftext import extract_text
from .policy import KeyedContext, contract_fee_for, evaluate, validate_references
from .retrieval import BM25Retriever, Retriever
from .runtime import Clock, Ids, iso_plus, sha256_hex
from .schema import (
    FORBIDDEN_ACTIONS,
    Action,
    SERVICE_PRINCIPAL,
    WRITE_ACTIONS,
    AuditStatus,
    DocumentKind,
    Extraction,
    PostPayable,
    Principal,
    ProposalSet,
    Role,
    RunOutcome,
    Tier,
    VendorRequest,
    params_dict,
)
from .store import DynamoStore
from .world import normalize_ref, tenant_policy

CODE_VERSION = "gwp-0.1.0"
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
# A run holds a lease from upload until it is finalized. The scheduled sweep resumes a run whose lease ran out.
# A worker's lease must outlast the Lambda timeout (240 seconds), so an expired lease means the worker is gone.
RUN_LEASE_SECONDS = 300
# Before a worker claims it, the lease covers the async event's maximum age (3600 seconds) plus one run.
DISPATCH_LEASE_SECONDS = 3600 + RUN_LEASE_SECONDS
# A run parked for an outside agent's proposal waits this long. Then the sweep ends it as "needs human".
PROPOSAL_LEASE_SECONDS = 3600
# An outside agent's searches and proposal sizes are bounded, so it can't grow a run past DynamoDB's 400 KB item limit.
# The built-in proposer is bounded by its model turns.
MAX_SEARCHES_PER_RUN = 6
MAX_PROPOSAL_BYTES = 20_000
# Why an outside agent may hand a parked run to a person: the built-in proposer's failure reasons, and "unclear".
CANNOT_PROPOSE_REASONS = frozenset({"model_timeout", "model_throttled", "model_error", "invalid_proposal", "unclear"})


class NotAllowed(Exception):
    pass


class NotAwaitingProposal(Exception):
    """An agent asked for a run's proposal context, or searched for it, after the run left `awaiting_proposal`."""


class InvalidDecision(Exception):
    """A missing or malformed approval decision. It blocks the write; it never defaults to approve."""


@dataclass
class UploadResult:
    run_id: str
    document_id: str
    duplicate: bool
    outcome: str | None = None


@dataclass
class RunResult:
    run_id: str
    outcome: str
    reason: str | None
    audit_ids: list[str] = field(default_factory=list)
    detail: str | None = None  # validation errors for an agent's invalid proposal, which it may retry once


@dataclass
class DecisionResult:
    status: str  # applied | declined | already_decided | failed | retryable (the retry applies it)
    audit_id: str
    run_outcome: str | None = None
    detail: str | None = None


@dataclass
class RevertResult:
    outcome: str  # REVERTED | REVERT_REFUSED
    reason: str | None = None
    already: bool = False


def _norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s.lower())).strip()


def _schema_field_names(model: type[BaseModel], seen: set | None = None) -> set[str]:
    seen = set() if seen is None else seen
    names: set[str] = set()
    if model in seen:
        return names
    seen.add(model)
    for name, f in model.model_fields.items():
        names.add(name)
        for arg in _type_args(f.annotation):
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                names |= _schema_field_names(arg, seen)
    return names


def _type_args(tp) -> list:
    out, todo = [], [tp]
    while todo:
        t = todo.pop()
        out.append(t)
        todo.extend(get_args(t))
    return out


_PROPOSAL_FIELDS = _schema_field_names(ProposalSet)
_FEEDBACK_WORDS = {
    "missing": "is required", "extra_forbidden": "is not allowed", "union_tag_invalid": "is not an allowed action",
    "union_tag_not_found": "needs an action", "literal_error": "is not an allowed value",
    "string_pattern_mismatch": "does not match the required format", "string_too_long": "is too long",
    "string_too_short": "is too short", "too_long": "has too many items", "too_short": "has too few items",
    "greater_than": "is too small", "greater_than_equal": "is too small", "less_than": "is too large",
    "less_than_equal": "is too large", "_type": "has the wrong type", "_parsing": "has the wrong type",
    "date_from_datetime_parsing": "is not a date", "value_error": "is not valid",
}


def validation_feedback(ve: ValidationError) -> str:
    """Validation errors for a retry, in words code chose. The proposal's own text never comes back.

    A path segment that isn't one of the schema's field names (an extra key the proposer made up) becomes
    "<extra field>", and messages are pydantic's own, which don't echo the input. Feedback is rendered outside the
    untrusted block, and over MCP another agent may read it, so it must not carry the proposer's words.
    """
    known = _PROPOSAL_FIELDS | {a.value for a in Action}
    parts = []
    for err in ve.errors()[:5]:
        loc = ".".join(str(x) if isinstance(x, int) or x in known else "<extra field>" for x in err["loc"])
        # Pydantic's own messages can quote the input (e.g. an unknown action tag), so the words are chosen here from
        # the error type, and an unfamiliar type is named by its code.
        kind = err["type"]
        words = _FEEDBACK_WORDS.get(kind) or next((w for k, w in _FEEDBACK_WORDS.items() if kind.endswith(k)),
                                                   f"is invalid ({kind})")
        parts.append(f"{loc}: {words}"[:200])
    return "; ".join(parts)[:600]


def unknown_ids_feedback(unknown: list[str]) -> str:
    """Feedback for ids that exist nowhere, as kinds and counts, e.g. "unknown ids: 1 po". Like
    `validation_feedback`, it never returns the proposal's own text, and an id is text the proposer wrote."""
    counts: dict[str, int] = {}
    for ref in unknown:
        kind = ref.split(":", 1)[0]
        counts[kind] = counts.get(kind, 0) + 1
    return "unknown ids: " + ", ".join(f"{n} {kind}" for kind, n in sorted(counts.items()))


def too_large(e: ClientError) -> bool:
    """Whether a write failed because the item would pass DynamoDB's 400 KB limit. A single UpdateItem reports it
    as a ValidationException; inside a transaction it is a `ValidationError` cancellation reason."""
    code = e.response["Error"]["Code"]
    reasons = [r.get("Code") for r in e.response.get("CancellationReasons", [])]
    return code == "ValidationException" or (code == "TransactionCanceledException" and "ValidationError" in reasons)


def validate_extraction(ext: Extraction) -> list[str]:
    """Step 4. A failed check is recorded and forces the approval tier; it does not stop the run."""
    flags: list[str] = []
    for ln in ext.lines:
        if ln.amount_cents != ln.qty * ln.unit_price_cents:
            flags.append("arithmetic")
            break
    if ext.total_cents is not None:
        computed = sum(ln.amount_cents for ln in ext.lines) + ext.freight_cents + ext.tax_cents
        if abs(computed) != abs(ext.total_cents):
            flags.append("arithmetic")
    elif ext.document_kind in (DocumentKind.invoice, DocumentKind.credit_memo):
        flags.append("missing_total")
    if ext.invoice_date is not None and not (date(2020, 1, 1) <= ext.invoice_date <= date(2030, 12, 31)):
        flags.append("date_out_of_range")
    return sorted(set(flags))


class Orchestrator:
    def __init__(self, store: DynamoStore, blobs: BlobStore, reader: Reader, proposer: Proposer,
                 clock: Clock | None = None, ids: Ids | None = None, executor: Executor | None = None,
                 retriever_factory: Callable[[str], Retriever] | None = None, backoff_s: float = 2.0,
                 search_enabled: bool = True, external_proposals: bool = False):
        self.store = store
        self.blobs = blobs
        self.reader = reader
        self.proposer = proposer
        self.clock = clock or Clock()
        self.ids = ids or Ids()
        self.executor = executor or Executor(store, self.clock, self.ids)
        self.retriever_factory = retriever_factory or (lambda t: BM25Retriever(t, store.list_chunks(t)))
        self.backoff_s = backoff_s
        self.search_enabled = search_enabled
        # With external proposals, `process` stops after step 5 and an outside agent proposes through
        # `proposal_context`, `search_policy` and `submit_proposal`. The internal proposer is not called.
        self.external_proposals = external_proposals

    # -- step 1: upload ------------------------------------------------------------

    def upload(self, tenant_id: str, data: bytes, content_type: str, principal: Principal) -> UploadResult:
        if principal.role not in (Role.uploader, Role.admin):
            raise NotAllowed("uploading needs the uploader role")
        if len(data) > MAX_DOCUMENT_BYTES:
            raise ValueError("document too large")
        if self.store.get_tenant(tenant_id) is None:
            raise NotAllowed("unknown tenant")
        doc_sha = sha256_hex(data)
        run_key = sha256_hex(tenant_id, doc_sha)
        existing = self.store.find_run_by_key(tenant_id, run_key)
        if existing:
            return UploadResult(existing["run_id"], existing["document_id"], True, RunOutcome.DUPLICATE_UPLOAD)
        document_id = self.ids.new("D")
        storage_key = f"{tenant_id}/{doc_sha}"
        now = self.clock.now()
        self.blobs.put(storage_key, data, content_type)
        # The document record goes first, so a run never points at a missing document. If two uploads of
        # the same bytes race, the loser leaves an unreferenced document record, which is harmless.
        self.store.put_document({"tenant_id": tenant_id, "document_id": document_id, "sha256": doc_sha,
                                 "content_type": content_type, "storage_key": storage_key,
                                 "uploaded_by": principal.principal_id, "uploaded_at": now, "size": len(data)})
        run = {"tenant_id": tenant_id, "run_id": self.ids.new("R"), "document_id": document_id, "run_key": run_key,
               "state": "received", "started_at": now, "outcome": None,
               "history": [{"state": "received", "at": now}],
               "lease_flag": "LEASED", "lease_until": iso_plus(now, DISPATCH_LEASE_SECONDS)}
        run, created = self.store.create_run(run, run_key)
        if not created:
            return UploadResult(run["run_id"], run["document_id"], True, RunOutcome.DUPLICATE_UPLOAD)
        return UploadResult(run["run_id"], document_id, False)

    # -- steps 2 to 13: process --------------------------------------------------------

    def process(self, tenant_id: str, run_id: str) -> RunResult:
        run = self.store.get_run(tenant_id, run_id)
        if run is None:
            raise KeyError(run_id)
        if run["state"] == "finalized":
            return RunResult(run_id, run.get("outcome") or "", run.get("reason"), run.get("audit_ids", []))
        if run["state"] != "received":
            if run.get("lease_until") and run["lease_until"] < self.clock.now():
                # The worker that claimed this run is gone, since a lease outlasts the Lambda timeout.
                return self.resume(tenant_id, run_id)
            if run["state"] == "awaiting_proposal":
                reason = "invalid_proposal" if run.get("retry_feedback") else None
                return RunResult(run_id, RunOutcome.AWAITING_PROPOSAL, reason, detail=run.get("retry_feedback"))
            # Another worker holds this run. Say so instead of returning an empty outcome.
            return RunResult(run_id, RunOutcome.IN_PROGRESS, None)
        t0 = self.clock.monotonic()
        trace: dict = {"model_calls": [], "retrieved": [], "searches": [], "proposal_attempts": []}
        try:
            return self._process(tenant_id, run, trace, t0)
        except Exception as exc:  # noqa: BLE001 - fail closed on any bug
            return self._fail_closed(tenant_id, run_id, trace, t0, exc)

    def _fail_closed(self, tenant_id: str, run_id: str, trace: dict, t0: float, exc: Exception) -> RunResult:
        # Say honestly what exists: an exception after a commit leaves that write applied and audited.
        audits = self.store.list_audits(tenant_id, run_id)
        applied = [a["audit_id"] for a in audits if a["status"] == "applied"]
        current = self.store.get_run(tenant_id, run_id) or {}
        if current.get("state") == "finalized":
            # Someone else, e.g. the sweep, already finished the run. Keep their outcome and add the error.
            self.store.update_run(tenant_id, run_id, {"late_worker_error": repr(exc)[:500]})
            return self._current(current)
        if current.get("route") == "human":
            # Step 10 may have closed the routed records and then failed to open the task. Open it before the
            # run is finalized: finalizing drops the lease, and closed records are not open, so nothing else
            # would. If this fails too, the error leaves the run leased and the sweep finishes it.
            self._open_task(tenant_id, run_id, current.get("route_reason") or "interrupted")
        return self._finish(tenant_id, run_id, RunOutcome.NEEDS_HUMAN, "internal_error", trace, t0,
                            [a["audit_id"] for a in audits],
                            extra={"error": repr(exc)[:500], "applied_audit_ids": applied})

    def _process(self, tenant_id: str, run: dict, trace: dict, t0: float) -> RunResult:
        run_id = run["run_id"]
        doc = self.store.get_document(tenant_id, run["document_id"])
        assert doc is not None
        policy = tenant_policy(self.store, tenant_id)
        tenant = self.store.get_tenant(tenant_id) or {}

        # Step 2: text extraction.
        text, pages = extract_text(self.blobs.get(doc["storage_key"]), doc["content_type"])
        # Claim the run with a conditional update, so two workers that both read "received" can't both go on.
        # The claim starts this worker's lease; the sweep resumes the run if the lease runs out.
        now = self.clock.now()
        claimed = self.store.update_run(tenant_id, run_id, {"state": "text_extracted", "page_count": pages,
                                                            "char_count": len(text),
                                                            "lease_until": iso_plus(now, RUN_LEASE_SECONDS)},
                                        ("text_extracted", now), expect_state="received")
        if not claimed:
            return RunResult(run_id, RunOutcome.IN_PROGRESS, None)

        # Step 3: read, quarantined. One retry with backoff.
        extraction = None
        failure = "model_error"
        for attempt in (1, 2):
            try:
                rr = self.reader.read(text, run["document_id"], attempt)
                trace["model_calls"].append(rr.call.to_dict())
                extraction = rr.extraction
                break
            except ModelCallFailed as e:
                trace["model_calls"].append(e.call.to_dict())
                failure = {"timeout": "model_timeout", "throttled": "model_throttled",
                           "invalid_output": "invalid_extraction"}.get(e.kind, "model_error")
                if attempt == 1:
                    self.clock.sleep(self.backoff_s)
        if extraction is None:
            return self._finish(tenant_id, run_id, RunOutcome.NEEDS_HUMAN, failure, trace, t0)

        # Step 4: validate the extraction.
        flags = validate_extraction(extraction)
        trace["extraction"] = extraction.model_dump(mode="json")
        trace["extraction_flags"] = flags

        # Step 5: retrieve by key.
        ctx = self._keyed_context(tenant_id, policy, run["document_id"], extraction, flags, trace)

        if self.external_proposals:
            # An outside agent proposes, over MCP. Park the run with what it needs; `submit_proposal` goes on.
            return self._park(tenant_id, run_id, trace, t0)

        # Step 6 and 7: propose and validate. One retry for an invalid proposal or a failed call.
        retriever = self.retriever_factory(tenant_id) if self.search_enabled else None
        feedback: str | None = None
        proposal_set: ProposalSet | None = None
        cross_tenant: list[str] = []
        failure = "invalid_proposal"
        for attempt in (1, 2):
            inp = ProposerInput(tenant_id, run["document_id"], extraction, self._render_records(ctx),
                                ctx.vendor_note, feedback)
            try:
                pr = self.proposer.propose(inp, retriever, attempt)
            except ModelCallFailed as e:
                trace["model_calls"].append(e.call.to_dict())
                failure = {"timeout": "model_timeout", "throttled": "model_throttled"}.get(e.kind, "model_error")
                if attempt == 1:
                    self.clock.sleep(self.backoff_s)
                continue
            trace["model_calls"].append(pr.call.to_dict())
            for s in pr.searches:
                trace["searches"].append(s)
                for h in s["hits"]:
                    trace["retrieved"].append({"kind": "policy_chunk", "id": h["chunk_id"], "version": h["version"],
                                               "score": h["score"], "query": s["query"]})
            trace["proposal_attempts"].append({"attempt": attempt, "raw": pr.raw, "tools": pr.tool_attempts})
            failure = "invalid_proposal"
            if pr.raw is None:
                feedback = "no propose_write call"
                continue
            proposal_set, cross_tenant, feedback = self._validate_proposal(tenant_id, pr.raw, trace)
            if proposal_set is not None:
                break
        if proposal_set is None:
            return self._finish(tenant_id, run_id, RunOutcome.NEEDS_HUMAN, failure, trace, t0)
        return self._decide(tenant_id, run_id, doc, extraction, ctx, proposal_set, cross_tenant, policy, tenant,
                            trace, t0, claimed_state="text_extracted")

    def _validate_proposal(self, tenant_id: str, raw: dict, trace: dict
                           ) -> tuple[ProposalSet | None, list[str], str | None]:
        """Step 7: the schema, then every referenced id. Returns (set, cross-tenant ids, feedback for a retry).

        A reference to another tenant's record is not retried: the set goes on, and step 8 routes it to a person.
        """
        try:
            ps = ProposalSet.model_validate(raw)
        except ValidationError as ve:
            feedback = validation_feedback(ve)
            trace["proposal_attempts"][-1]["error"] = feedback
            return None, [], feedback
        refs = validate_references(ps, tenant_id, self.store)
        if refs.cross_tenant:
            return ps, refs.cross_tenant, None
        if refs.unknown:
            feedback = unknown_ids_feedback(refs.unknown)
            trace["proposal_attempts"][-1]["error"] = feedback
            return None, [], feedback
        return ps, [], None

    def _decide(self, tenant_id: str, run_id: str, doc: dict, extraction: Extraction, ctx: KeyedContext,
                proposal_set: ProposalSet, cross_tenant: list[str], policy: dict, tenant: dict, trace: dict,
                t0: float, claimed_state: str) -> RunResult:
        """Steps 8 to 12 for a valid proposal set, the same whether the internal proposer or an agent made it.

        `claimed_state` is the state this worker's claim left the run in. Step 9 starts by renewing the lease on
        that condition and ends with a conditional move to `audited`, so a worker that outlived its lease, which
        the sweep has since finished, stops before anything applies and leaves nothing half done.
        """
        # Step 8: policy check and tier.
        if cross_tenant:
            decision = None
            route, reason = "human", "cross_tenant"
        else:
            decision = evaluate(proposal_set, ctx, self.store)
            route, reason = decision.route, decision.reason

        # Step 9: one audit record per proposal, before anything is visible.
        now = self.clock.now()
        if not self.store.update_run(tenant_id, run_id, {"lease_until": iso_plus(now, RUN_LEASE_SECONDS)},
                                     expect_state=claimed_state):
            return self._current(self.store.get_run(tenant_id, run_id) or {"run_id": run_id})
        audit_ids = []
        for i, p in enumerate(proposal_set.proposals):
            pc = decision.proposals[i] if decision else None
            audit = self._audit_record(tenant_id, run_id, doc, extraction, p, pc, policy, tenant, trace, cross_tenant,
                                       route, reason)
            self.store.put_audit(audit)
            audit_ids.append(audit["audit_id"])
        bank_followup = route == "auto" and VendorRequest.bank_details_change in extraction.vendor_requests
        # The whole set is recorded. From here on, `resume` may finish it; before this, it may not.
        audited = {"state": "audited", "audit_ids": audit_ids, "audit_set_complete": True, "route": route,
                   "route_reason": reason}
        if bank_followup:
            # The written policy sends any bank change request to a person. The agent can't act on it, and the
            # remit-to on this document matched the vendor record, so the payable posts and a person follows up.
            # The task is written with the move to `audited`: opened first, a crash between left the set
            # incomplete, and `resume` opened a second, `interrupted` task (DECISIONS 56).
            moved = self._update_run_with_task(tenant_id, run_id, audited, ("audited", self.clock.now()),
                                               "vendor_requested_bank_change", expect_state=claimed_state)
        else:
            moved = self.store.update_run(tenant_id, run_id, audited, ("audited", self.clock.now()),
                                          expect_state=claimed_state)
        if not moved:
            # The sweep finished this run after the lease renewal above, so it may have seen an incomplete set.
            # Nothing has applied; close these records, and leave the run as the sweep left it.
            for aid in audit_ids:
                self.store.transition_audit(tenant_id, aid, [AuditStatus.proposed], AuditStatus.failed,
                                            self.clock.now(), {"error": "claim_lost"}, terminal=True)
            return self._current(self.store.get_run(tenant_id, run_id) or {"run_id": run_id})

        # Step 10: route.
        now = self.clock.now()
        if route == "human":
            for aid, p in zip(audit_ids, proposal_set.proposals):
                status = AuditStatus.rejected if (p.action in FORBIDDEN_ACTIONS or cross_tenant) else AuditStatus.routed
                self.store.transition_audit(tenant_id, aid, ["proposed"], status, now,
                                            {"decided_by": "system", "decided_at": now, "route_reason": reason},
                                            terminal=True)
            self._open_task(tenant_id, run_id, reason)
            return self._finish(tenant_id, run_id, RunOutcome.ROUTED_TO_HUMAN, reason, trace, t0, audit_ids,
                                decision)
        if route == "approval":
            for aid in audit_ids:
                self.store.transition_audit(tenant_id, aid, ["proposed"], AuditStatus.pending_approval, now)
            return self._finish(tenant_id, run_id, RunOutcome.PENDING_APPROVAL, reason, trace, t0, audit_ids,
                                decision)

        # Step 12: execute the auto tier.
        results = [self.executor.apply(tenant_id, aid) for aid in audit_ids]
        if all(r.status in ("applied", "already_applied") for r in results):
            return self._finish(tenant_id, run_id, RunOutcome.APPLIED, None, trace, t0, audit_ids, decision)
        errors = ",".join(r.error or r.status for r in results if r.status not in ("applied", "already_applied"))
        return self._finish(tenant_id, run_id, RunOutcome.NEEDS_HUMAN, "apply_failed", trace, t0, audit_ids,
                            decision, extra={"error": errors})

    # -- external proposals: an outside agent proposes, e.g. over MCP ----------------------------------

    def _park(self, tenant_id: str, run_id: str, trace: dict, t0: float) -> RunResult:
        """The end of step 5 in external-proposal mode. The run waits, leased, for an agent's proposal.

        The extraction and the step 3 and 5 trace are kept on the run, so `submit_proposal` can go on from here in
        another process. If no proposal arrives before the lease runs out, the sweep ends the run as needing a
        person, with the reason `proposal_timeout`.
        """
        now = self.clock.now()
        fields = {"state": "awaiting_proposal", "outcome": RunOutcome.AWAITING_PROPOSAL,
                  "extraction": trace["extraction"], "extraction_flags": trace["extraction_flags"],
                  "model_calls": trace["model_calls"], "retrieved": trace["retrieved"], "searches": [],
                  "proposal_attempts": [], "lease_until": iso_plus(now, PROPOSAL_LEASE_SECONDS),
                  # The vendor code resolved in step 5, a trusted id, so a view of the run needs no document text.
                  "vendor_id": next((r["id"] for r in trace["retrieved"] if r["kind"] == "vendor"), None),
                  # The server's own time so far. The run's latency adds the time after the proposal arrives, and
                  # leaves out the time the run waited for the agent.
                  "prepare_latency_ms": int((self.clock.monotonic() - t0) * 1000)}
        if not self.store.update_run(tenant_id, run_id, fields, ("awaiting_proposal", now),
                                     expect_state="text_extracted"):
            return self._current(self.store.get_run(tenant_id, run_id) or {"run_id": run_id})
        return RunResult(run_id, RunOutcome.AWAITING_PROPOSAL, None)

    @staticmethod
    def _require_agent(principal: Principal) -> None:
        if principal.role != Role.agent:
            raise NotAllowed("proposing needs the agent role")

    @staticmethod
    def _current(run: dict) -> RunResult:
        outcome = run.get("outcome") if run.get("state") in ("finalized", "awaiting_proposal") else None
        return RunResult(run["run_id"], outcome or RunOutcome.IN_PROGRESS, run.get("reason"), run.get("audit_ids", []))

    def _parked(self, tenant_id: str, run_id: str) -> dict:
        run = self.store.get_run(tenant_id, run_id)
        if run is None:
            raise KeyError(run_id)
        if run["state"] != "awaiting_proposal":
            raise NotAwaitingProposal(f"run {run_id} is not waiting for a proposal (state {run['state']}, "
                                      f"outcome {run.get('outcome')})")
        if run.get("lease_until") and run["lease_until"] < self.clock.now():
            raise NotAwaitingProposal(f"run {run_id} passed its proposal deadline and goes to a person")
        return run

    def awaiting_proposals(self, tenant_id: str, principal: Principal) -> list[dict]:
        """The agent's work queue: this tenant's runs parked for a proposal, oldest first."""
        self._require_agent(principal)
        now = self.clock.now()
        runs = [r for r in self.store.list_runs(tenant_id)
                if r.get("state") == "awaiting_proposal" and not (r.get("lease_until") and r["lease_until"] < now)]
        return [{"run_id": r["run_id"], "document_id": r["document_id"], "started_at": r["started_at"],
                 "proposal_deadline": r.get("lease_until")} for r in sorted(runs, key=lambda r: r["started_at"])]

    def proposal_context(self, tenant_id: str, run_id: str, principal: Principal) -> dict:
        """What the internal proposer would be shown, as data: typed fields and the records code looked up.

        The records are looked up again now, so they are current. No document text is in here, and no free text
        stored from an earlier document (`_render_records`), so an outside agent sees what the internal one sees.
        """
        self._require_agent(principal)
        run = self._parked(tenant_id, run_id)
        extraction = Extraction.model_validate(run["extraction"])
        ctx = self._keyed_context(tenant_id, tenant_policy(self.store, tenant_id), run["document_id"], extraction,
                                  run.get("extraction_flags", []), {"retrieved": []})
        return {"run_id": run_id, "document_id": run["document_id"],
                "attempt": len(run.get("proposal_attempts") or []) + 1, "vendor_note": ctx.vendor_note,
                "extracted_fields_untrusted": extraction.model_dump(mode="json"),
                "records_trusted": self._render_records(ctx), "retry_feedback": run.get("retry_feedback"),
                "search_enabled": self.search_enabled}

    def search_policy(self, tenant_id: str, run_id: str, query: str, principal: Principal) -> list[dict]:
        """The proposer's read-only policy search, bound to the caller's tenant and logged on the run."""
        self._require_agent(principal)
        self._parked(tenant_id, run_id)
        if not self.search_enabled:
            raise NotAllowed("search is disabled for this service")
        q = str(query)[:200]
        hits = self.retriever_factory(tenant_id).search(q, k=3)
        entry = {"query": q, "hits": [{"chunk_id": h.chunk_id, "doc_id": h.doc_id, "version": h.version,
                                       "score": h.score} for h in hits]}
        # Logged before the hits are returned, and only while the run still waits and is under its search limit.
        if not self.store.append_to_run(tenant_id, run_id, {"searches": [entry]}, expect_state="awaiting_proposal",
                                        max_len=("searches", MAX_SEARCHES_PER_RUN)):
            raise NotAwaitingProposal(f"run {run_id} is no longer waiting for a proposal, or has used its "
                                      f"{MAX_SEARCHES_PER_RUN} searches")
        return [{"chunk_id": h.chunk_id, "text": h.text} for h in hits]

    def submit_proposal(self, tenant_id: str, run_id: str, raw: dict, principal: Principal) -> RunResult:
        """Steps 7 to 12 for a proposal set an outside agent sent, through the same code as the internal path.

        The run is claimed with a conditional update from `awaiting_proposal`, so of two calls only one proposes,
        and a repeated call returns the run's current state. An invalid set gets one retry with the errors fed
        back, as the internal proposer does, and a second invalid set ends the run as needing a person.
        """
        self._require_agent(principal)
        run = self.store.get_run(tenant_id, run_id)
        if run is None:
            raise KeyError(run_id)
        if run["state"] != "awaiting_proposal":
            return self._current(run)
        now = self.clock.now()
        if run.get("lease_until") and run["lease_until"] < now:
            return self.resume(tenant_id, run_id)  # the proposal came too late; the run goes to a person
        if not self.store.update_run(tenant_id, run_id, {"state": "proposing", "proposed_by": principal.principal_id,
                                                         "lease_until": iso_plus(now, RUN_LEASE_SECONDS)},
                                     ("proposing", now), expect_state="awaiting_proposal"):
            return self._current(self.store.get_run(tenant_id, run_id) or run)
        # Read the run again now that it's claimed: a search can land between the first read and the claim.
        run = self.store.get_run(tenant_id, run_id) or run
        t0 = self.clock.monotonic() - run.get("prepare_latency_ms", 0) / 1000
        trace: dict = {k: list(run.get(k) or []) for k in ("model_calls", "retrieved", "searches", "proposal_attempts")}
        trace["extraction"], trace["extraction_flags"] = run["extraction"], list(run.get("extraction_flags") or [])
        try:
            return self._submit(tenant_id, run, raw, principal, trace, t0)
        except Exception as exc:  # noqa: BLE001 - fail closed on any bug
            return self._fail_closed(tenant_id, run_id, trace, t0, exc)

    def cannot_propose(self, tenant_id: str, run_id: str, reason: str, principal: Principal) -> RunResult:
        """The agent can't propose, e.g. its own model calls failed: the run goes to a person now.

        The reason comes from a closed list, the same failure reasons the built-in proposer's loop records, and the
        outcome is always NEEDS_HUMAN with a task, so nothing an agent says here can cause a write. The run and its
        task are written in one transaction, as in `resume`. A run that already left `awaiting_proposal` is
        returned as it is.
        """
        self._require_agent(principal)
        if reason not in CANNOT_PROPOSE_REASONS:
            raise ValueError(f"reason must be one of {', '.join(sorted(CANNOT_PROPOSE_REASONS))}")
        run = self.store.get_run(tenant_id, run_id)
        if run is None:
            raise KeyError(run_id)
        if run["state"] != "awaiting_proposal":
            return self._current(run)
        now = self.clock.now()
        if not self.store.finalize_run_with_task(
                tenant_id, run_id, {"state": "finalized", "outcome": RunOutcome.NEEDS_HUMAN, "reason": reason,
                                    "ended_at": now, "reported_by": principal.principal_id},
                (f"finalized:{RunOutcome.NEEDS_HUMAN}", now), self._task(tenant_id, run_id, reason),
                expect_state="awaiting_proposal"):
            return self._current(self.store.get_run(tenant_id, run_id) or run)
        return RunResult(run_id, RunOutcome.NEEDS_HUMAN, reason)

    def _submit(self, tenant_id: str, run: dict, raw: dict, principal: Principal, trace: dict,
                t0: float) -> RunResult:
        run_id = run["run_id"]
        attempt = len(trace["proposal_attempts"]) + 1
        size = len(json.dumps(raw, default=str))
        if size > MAX_PROPOSAL_BYTES:
            # Not stored, so it can't grow the run past DynamoDB's item limit. It still counts as an attempt.
            raw = {"too_large_bytes": size}
        trace["proposal_attempts"].append({"attempt": attempt, "raw": raw, "tools": ["propose"],
                                           "by": principal.principal_id})
        doc = self.store.get_document(tenant_id, run["document_id"])
        assert doc is not None
        policy = tenant_policy(self.store, tenant_id)
        tenant = self.store.get_tenant(tenant_id) or {}
        extraction = Extraction.model_validate(run["extraction"])
        # Step 5 again, so step 8 checks against the records as they are now, not as they were when the run parked.
        keyed: dict = {"retrieved": []}
        ctx = self._keyed_context(tenant_id, policy, run["document_id"], extraction, trace["extraction_flags"], keyed)
        trace["retrieved"] = keyed["retrieved"] + [
            {"kind": "policy_chunk", "id": h["chunk_id"], "version": h["version"], "score": h["score"],
             "query": s["query"]} for s in trace["searches"] for h in s["hits"]]
        if "too_large_bytes" in raw:
            proposal_set, cross_tenant = None, []
            feedback = f"proposal too large: {raw['too_large_bytes']} bytes, the limit is {MAX_PROPOSAL_BYTES}"
            trace["proposal_attempts"][-1]["error"] = feedback
        else:
            proposal_set, cross_tenant, feedback = self._validate_proposal(tenant_id, raw, trace)
        if proposal_set is None:
            if attempt < 2:
                now = self.clock.now()
                if not self.store.update_run(tenant_id, run_id, {"state": "awaiting_proposal",
                                                                 "retry_feedback": feedback,
                                                                 "proposal_attempts": trace["proposal_attempts"],
                                                                 "lease_until": iso_plus(now, PROPOSAL_LEASE_SECONDS)},
                                             ("awaiting_proposal", now), expect_state="proposing"):
                    # The sweep finished the run while this proposal was checked; say what it decided.
                    return self._current(self.store.get_run(tenant_id, run_id) or run)
                return RunResult(run_id, RunOutcome.AWAITING_PROPOSAL, "invalid_proposal", detail=feedback)
            # The run and its task in one transaction, as cannot_propose does. With the task first, a crash between
            # left a leased run with no audit records, which the sweep finalized as `interrupted` with a second task.
            return self._finish(tenant_id, run_id, RunOutcome.NEEDS_HUMAN, "invalid_proposal", trace, t0,
                                expect_state="proposing", task_reason="invalid_proposal")
        return self._decide(tenant_id, run_id, doc, extraction, ctx, proposal_set, cross_tenant, policy, tenant,
                            trace, t0, claimed_state="proposing")

    # -- step 5 helpers ---------------------------------------------------------------

    def _keyed_context(self, tenant_id: str, policy: dict, document_id: str, ext: Extraction, flags: list[str],
                       trace: dict) -> KeyedContext:
        by_name: set[str] = set()
        by_tax: set[str] = set()
        name = _norm_name(ext.vendor_name)
        vendors = self.store.list_vendors(tenant_id)
        for v in vendors:
            names = [_norm_name(v["legal_name"])] + [_norm_name(a) for a in v.get("aliases", [])]
            if name and name in names:
                by_name.add(v["vendor_id"])
            if ext.vendor_tax_id and ext.vendor_tax_id.strip() == v.get("tax_id"):
                by_tax.add(v["vendor_id"])
        found = by_name | by_tax
        vendor = None
        if len(found) == 1:
            vendor = next(v for v in vendors if v["vendor_id"] in found)
            how = "name" if by_name else "tax id"
            note = f"matched {vendor['vendor_id']} by {how}"
        elif not found:
            note = "no vendor in this company's vendor list matches the document"
        else:
            note = "the document matches more than one vendor; not resolved"
        ctx = KeyedContext(tenant_id, policy, document_id, ext, flags, vendor, note)
        log = trace["retrieved"]
        if vendor is None:
            return ctx
        log.append({"kind": "vendor", "id": vendor["vendor_id"], "version": vendor.get("version", 1), "score": None})
        for po in self.store.list_pos(tenant_id, vendor["vendor_id"]):
            if po["status"] != "open":
                continue
            ctx.pos[po["po_id"]] = po
            log.append({"kind": "po", "id": po["po_id"], "version": 1, "score": None})
            for r in self.store.get_receipts(tenant_id, po["po_id"]):
                ctx.receipts[(po["po_id"], r["line_no"])] = r
                log.append({"kind": "receipt", "id": f"{po['po_id']}#{r['line_no']}", "version": r["version"],
                            "score": None})
        for c in self.store.list_contracts(tenant_id, vendor["vendor_id"]):
            ctx.contracts[c["contract_id"]] = c
            log.append({"kind": "contract", "id": c["contract_id"], "version": 1, "score": None})
        for p in self.store.list_payables(tenant_id, vendor["vendor_id"]):
            ctx.payables[p["payable_id"]] = p
            log.append({"kind": "payable", "id": p["payable_id"], "version": p.get("version", 1), "score": None})
        return ctx

    def _render_records(self, ctx: KeyedContext) -> dict:
        pol = ctx.policy
        out: dict = {
            "structured_policy": {
                "auto_limit_cents": pol["auto_limit_cents"], "price_tolerance_percent": pol["price_tolerance_bps"] / 100,
                "sales_tax_percent": pol["sales_tax_bps"] / 100, "freight_account": pol["freight_account"],
                "tax_account": pol["tax_account"],
            },
            "vendor": None,
        }
        v = ctx.vendor
        if v is None:
            return out
        out["vendor"] = {"vendor_id": v["vendor_id"], "legal_name": v["legal_name"], "category": v["category"],
                         "default_account": v["default_account"], "allowed_accounts": v["allowed_accounts"]}
        out["open_purchase_orders"] = [
            {"po_id": po["po_id"], "freight_allowance_cents": po.get("freight_allowance_cents", 0),
             "lines": [{**ln, "qty_received": ctx.receipts[(po["po_id"], ln["line_no"])]["qty_received"],
                        "qty_invoiced": ctx.receipts[(po["po_id"], ln["line_no"])]["qty_invoiced"]}
                       for ln in po["lines"]]}
            for po in ctx.pos.values()]
        inv_date = ctx.extraction.invoice_date
        out["contracts"] = [
            {"contract_id": c["contract_id"], "description": c.get("description", ""),
             "price_schedule": c["price_schedule"],
             "fee_on_invoice_date_cents": contract_fee_for(c, inv_date) if inv_date else None}
            for c in ctx.contracts.values()]
        # Posted payables were made from earlier uploaded documents, so their free text (line descriptions and
        # invoice numbers) is attacker text. None of it is shown here. Item names come from the purchase order,
        # a trusted record, and the invoice number is replaced by code-computed matches against this document.
        ext = ctx.extraction
        this_number = normalize_ref(ext.invoice_number or "")
        referenced = {normalize_ref(r) for r in ext.referenced_invoice_numbers}
        po_items: dict[str, dict[int, str]] = {}
        for p in ctx.payables.values():
            if p.get("po_id") and p["po_id"] not in po_items:
                po = self.store.get_po(ctx.tenant_id, p["po_id"]) or {"lines": []}
                po_items[p["po_id"]] = {pl["line_no"]: pl["item"] for pl in po["lines"]}
        out["posted_payables"] = [
            {"payable_id": p["payable_id"], "invoice_date": p["invoice_date"], "po_id": p.get("po_id"),
             "contract_id": p.get("contract_id"), "status": p["status"],
             "total_cents": p["total_cents"], "credits_cents": p.get("credits_cents", 0),
             "invoice_number_same_as_this_document": bool(this_number)
             and normalize_ref(p["invoice_number"]) == this_number,
             "invoice_number_referenced_by_this_document": normalize_ref(p["invoice_number"]) in referenced,
             "lines": [{"line_no": ln["line_no"], "kind": ln.get("kind", "item"), "po_line_no": ln.get("po_line_no"),
                        "item_from_po": po_items.get(p.get("po_id") or "", {}).get(ln.get("po_line_no") or 0),
                        "qty": ln.get("qty"), "account": ln["account"], "amount_cents": ln["amount_cents"]}
                       for ln in p["lines"]]}
            for p in ctx.payables.values()]
        return out

    # -- step 9 helper ----------------------------------------------------------------

    def _audit_record(self, tenant_id, run_id, doc, extraction, p, pc, policy, tenant, trace, cross_tenant,
                      route, reason):
        audit_id = self.ids.new("A")
        proposal_id = f"{run_id}:{len(trace.get('_proposals', []))}"
        trace.setdefault("_proposals", []).append(proposal_id)
        now = self.clock.now()
        if cross_tenant:
            tier, rules, checks = Tier.forbidden, ["cross_tenant"], {"tenant_match": "fail"}
        else:
            tier, rules, checks = pc.tier, list(pc.rules), pc.checks
            if route == "human" and tier != Tier.forbidden:
                # The run goes to a person, so no write in it can apply at any tier.
                tier = Tier.human
                rules.append(f"run_routed:{reason}")
        audit = {
            # identity
            "audit_id": audit_id, "tenant_id": tenant_id, "run_id": run_id, "proposal_id": proposal_id,
            "idempotency_key": execution_key(tenant_id, proposal_id, p.action), "created_at": now,
            "updated_at": now,
            # context
            "document_id": doc["document_id"], "document_sha256": doc["sha256"], "extraction_id": f"{run_id}#extraction",
            "retrieved": trace["retrieved"], "model_calls": trace["model_calls"],
            "policy_version": tenant.get("policy_version"), "code_version": CODE_VERSION,
            # what was proposed; the rationale is model text, stored as data
            "action": p.action, "params": params_dict(p), "rationale_model_text": p.rationale,
            "requires_approval_reason_model_text": p.requires_approval_reason, "evidence": p.evidence,
            # confidence: the model's own number, and the code checks
            "confidence_model_reported": p.confidence, "checks": checks,
            # authority
            "tier": tier, "tier_rules": rules,
            # outcome
            "status": AuditStatus.proposed, "write_ids": [],
            "history": [{"state": AuditStatus.proposed, "at": now}], "open_flag": "OPEN", "open_since": now,
        }
        if isinstance(p, PostPayable):
            audit["apply_input"] = {"lines": self._payable_lines(p, extraction)}
            audit["target_id"] = f"{p.params.vendor_id}:{p.params.invoice_number}"
        elif pc is not None and pc.target_id:
            audit["target_id"] = pc.target_id
        return audit

    @staticmethod
    def _payable_lines(p: PostPayable, ext: Extraction) -> list[dict]:
        out = []
        for i, ln in enumerate(p.params.lines):
            src = ext.lines[ln.source_line - 1] if ln.source_line and ln.source_line <= len(ext.lines) else None
            out.append({"line_no": i + 1, "kind": ln.kind, "account": ln.account, "amount_cents": ln.amount_cents,
                        "po_line_no": ln.po_line_no if ln.kind == "item" else None,
                        # Stored under a name that says what it is. It is never shown to a model again.
                        "description_untrusted": src.description if src else ln.kind,
                        "qty": src.qty if src else 0, "source_line": ln.source_line})
        return out

    # -- step 13: finalize ----------------------------------------------------------------

    def _finish(self, tenant_id, run_id, outcome, reason, trace, t0, audit_ids=None, decision=None, extra=None,
                expect_state=None, task_reason=None):
        """Finalize the run. With `task_reason`, the run and its task for a person are written in one transaction,
        for a run with no audit records: after a crash between two writes, `resume` would open a second task under
        another reason (DECISIONS M14)."""
        costs = [c["cost_usd"] for c in trace["model_calls"] if c.get("cost_usd") is not None]
        fields = {
            "state": "finalized", "outcome": outcome, "reason": reason, "ended_at": self.clock.now(),
            "latency_ms": int((self.clock.monotonic() - t0) * 1000), "cost_usd": round(sum(costs), 8),
            "model_calls": trace["model_calls"], "retrieved": trace["retrieved"], "searches": trace["searches"],
            "proposal_attempts": trace["proposal_attempts"], "audit_ids": audit_ids or [],
            "reasons": sorted({r for pc in decision.proposals for r in pc.rules}) if decision else [],
        }
        if "extraction" in trace:
            fields["extraction"] = trace["extraction"]
            fields["extraction_flags"] = trace["extraction_flags"]
        fields.setdefault("applied_audit_ids", [])
        fields.update(extra or {})
        if audit_ids and "applied_audit_ids" not in (extra or {}):
            fields["applied_audit_ids"] = [a["audit_id"] for a in self.store.list_audits(tenant_id, run_id)
                                           if a["status"] == "applied"]
        history = (f"finalized:{outcome}", fields["ended_at"])

        def write(f: dict) -> bool:
            if task_reason is None:
                return self.store.update_run(tenant_id, run_id, f, history, expect_state=expect_state)
            return self._update_run_with_task(tenant_id, run_id, f, history, task_reason, expect_state)

        try:
            if not write(fields):
                return self._current(self.store.get_run(tenant_id, run_id) or {"run_id": run_id})
        except ClientError as e:
            if not too_large(e):
                raise
            # The full record would pass DynamoDB's item size limit. Finalize with the outcome only, so the run
            # still ends and leaves the sweep's index; the audit records keep the detail.
            small = {k: fields[k] for k in ("state", "outcome", "reason", "ended_at", "latency_ms", "audit_ids",
                                            "applied_audit_ids", "extraction", "extraction_flags", "error")
                     if k in fields}
            small["size_note"] = "run record too large to store in full"
            if not write(small):
                return self._current(self.store.get_run(tenant_id, run_id) or {"run_id": run_id})
        return RunResult(run_id, outcome, reason, audit_ids or [])

    def resume(self, tenant_id: str, run_id: str, lease_expired_by: str | None = None,
               _again: bool = False) -> RunResult:
        """Redeliver a run whose worker died or raised: finish what its audit records say is left, then finalize.

        Safe to call any number of times. Execution keys make a repeated apply a no-op, and every status change
        is a compare-and-set.

        - If the worker died before the whole proposal set was recorded (step 9), no part of the set may apply:
          the records are marked failed and a person decides.
        - Otherwise each record is finished by its tier: an auto record still at `proposed` is applied, an
          approval record still at `proposed` moves to `pending_approval`, a routed or forbidden record still at
          `proposed` is closed and a person gets a task, and an `approved` record is applied. This holds whether
          or not the run was finalized, e.g. by the fail-closed handler after one write of a set committed.
        - A run routed to a person always gets its task, even if its records were closed before the worker died.
          A task is opened at most once per run and reason, so two sweeps that resume the same run open one.

        The sweep passes `lease_expired_by`, its own time. A run with no records, or an incomplete set, is then
        finalized only if its lease ran out before that time, so a worker that claimed or renewed the run after the
        sweep looked keeps it (entry 59).
        """
        run = self.store.get_run(tenant_id, run_id)
        if run is None:
            raise KeyError(run_id)
        audits = self.store.list_audits(tenant_id, run_id)
        finalized = run.get("state") == "finalized"
        now = self.clock.now()
        if not audits:
            if finalized:
                return RunResult(run_id, run["outcome"], run.get("reason"), [])
            # Died before any audit record existed, so nothing was proposed or written. A person decides. The
            # update is conditional on the state read here, so a worker that claims the run meanwhile wins. The
            # run and its task are written in one transaction: finalizing drops the lease, so a crash between two
            # separate writes left a finalized run with no task and nothing that would ever open it.
            # A run parked for an outside agent that never proposed ends the same way, under its own reason.
            # A worker that reserved the run for its task and died (entry 61) left the reason it was opening.
            reason = run.get("pending_task_reason") or (
                "proposal_timeout" if run["state"] == "awaiting_proposal" else "interrupted")
            if not self.store.finalize_run_with_task(
                    tenant_id, run_id, {"state": "finalized", "outcome": RunOutcome.NEEDS_HUMAN,
                                        "reason": reason}, ("finalized_on_resume", now),
                    self._task(tenant_id, run_id, reason), expect_state=run["state"],
                    lease_before=lease_expired_by):
                return RunResult(run_id, RunOutcome.IN_PROGRESS, None)
            return RunResult(run_id, RunOutcome.NEEDS_HUMAN, reason)
        audit_ids = [a["audit_id"] for a in audits]
        if not run.get("audit_set_complete"):
            # Finalize the run with its task first, on the condition that the set is still incomplete and the run
            # is where it was read. A worker that outlived its lease and marks the set complete first wins, and
            # this reads again and finishes the set by tier (entry 60). The worker's own move to `audited` is
            # conditional on its claim, so once this lands, the worker closes what it wrote as `claim_lost`. A
            # crash before the records below are closed leaves them to the staleness pass (M13).
            try:
                done = self._update_run_with_task(
                    tenant_id, run_id, {"state": "finalized", "outcome": RunOutcome.NEEDS_HUMAN,
                                        "reason": "interrupted", "audit_ids": audit_ids},
                    ("finalized_on_resume", now), "interrupted", run["state"], fallback=False, set_incomplete=True,
                    lease_before=lease_expired_by)
            except ClientError as e:
                if e.response["Error"]["Code"] != "TransactionCanceledException":
                    raise
                # Still contended after the retries. Change nothing; the next sweep comes back (entry 61).
                return RunResult(run_id, RunOutcome.IN_PROGRESS, None)
            if not done:
                if _again:
                    return RunResult(run_id, RunOutcome.IN_PROGRESS, None)
                return self.resume(tenant_id, run_id, lease_expired_by, _again=True)
            for a in audits:
                self.store.transition_audit(tenant_id, a["audit_id"], [AuditStatus.proposed], AuditStatus.failed,
                                            now, {"error": "audit_set_incomplete"}, terminal=True)
            return RunResult(run_id, RunOutcome.NEEDS_HUMAN, "interrupted", audit_ids)

        routed = False
        for a in audits:
            status, tier = a["status"], a["tier"]
            if status == AuditStatus.approved or (status == AuditStatus.proposed and tier == Tier.auto):
                self.executor.apply(tenant_id, a["audit_id"])
            elif status == AuditStatus.proposed and tier == Tier.approval:
                self.store.transition_audit(tenant_id, a["audit_id"], [AuditStatus.proposed],
                                            AuditStatus.pending_approval, now)
            elif status == AuditStatus.proposed:
                to = AuditStatus.rejected if (tier == Tier.forbidden or a["action"] in FORBIDDEN_ACTIONS) \
                    else AuditStatus.routed
                self.store.transition_audit(tenant_id, a["audit_id"], [AuditStatus.proposed], to, now,
                                            {"decided_by": "system", "decided_at": now,
                                             "route_reason": run.get("route_reason")}, terminal=True)
                routed = True
        if routed or run.get("route") == "human":
            # Also when the records were already closed: the worker may have died before it opened the task.
            self._open_task(tenant_id, run_id, run.get("route_reason") or "interrupted")

        if run.get("route") == "human":
            # A run routed to a person stays routed; no write in it applies.
            outcome = RunOutcome.ROUTED_TO_HUMAN
            self.store.update_run(tenant_id, run_id, {"outcome": outcome}, (f"outcome:{outcome}", now))
        else:
            outcome = self._refresh_run_outcome(tenant_id, run_id) or RunOutcome.NEEDS_HUMAN
        if not finalized:
            calls = audits[0].get("model_calls", [])  # the audit record kept the model calls the dead worker made
            cost = round(sum(c["cost_usd"] for c in calls if c.get("cost_usd") is not None), 8)
            self.store.update_run(tenant_id, run_id, {"state": "finalized", "outcome": outcome, "model_calls": calls,
                                                      "cost_usd": cost, "retrieved": audits[0].get("retrieved", []),
                                                      "reason": run.get("route_reason"), "audit_ids": audit_ids},
                                  ("finalized_on_resume", now))
            return RunResult(run_id, outcome, run.get("route_reason"), audit_ids)
        return RunResult(run_id, outcome, run.get("reason"), audit_ids)

    def _update_run_with_task(self, tenant_id: str, run_id: str, fields: dict, history: tuple[str, str],
                              reason: str, expect_state: str | None, fallback: bool = True, **conditions) -> bool:
        """Update the run and open its task in one transaction (M14, entries 44 and 56).

        Contention or throttling cancels a transaction without a failed condition, and it usually clears at once,
        so the transaction is tried up to three times (entry 61). If it is still cancelled and `fallback` is set,
        which only a worker holding the run's claim sets, the worker first reserves the run: a conditional update
        that records the task's reason as `pending_task_reason` and renews the lease, so the sweep leaves the run
        alone. Then it opens the task, and then it updates the run. A crash after the reservation leaves the sweep
        to finish the run under that same reason, so one task. Without `fallback`, the error is raised.
        """
        task = self._task(tenant_id, run_id, reason)
        for attempt in range(3):
            try:
                return self.store.update_run_with_task(tenant_id, run_id, fields, history, task, expect_state,
                                                       **conditions)
            except ClientError as e:
                if e.response["Error"]["Code"] != "TransactionCanceledException" or too_large(e):
                    raise
                if attempt == 2 and not fallback:
                    raise
        reserve = {"pending_task_reason": reason, "lease_until": iso_plus(self.clock.now(), RUN_LEASE_SECONDS)}
        if not self.store.update_run(tenant_id, run_id, reserve, expect_state=expect_state, **conditions):
            return False  # someone else moved the run on first; this worker opens nothing
        self._open_task(tenant_id, run_id, reason)
        return self.store.update_run(tenant_id, run_id, {**fields, "pending_task_reason": None}, history,
                                     expect_state, **conditions)

    def _task(self, tenant_id: str, run_id: str, reason: str) -> dict:
        return {"tenant_id": tenant_id, "task_id": self.ids.new("H"), "run_id": run_id, "reason_code": reason,
                "status": "open", "created_at": self.clock.now()}

    def _open_task(self, tenant_id: str, run_id: str, reason: str) -> None:
        """Open a task for a person, once per run and reason, however many workers or sweeps get here.

        Every caller opens the task before the run is finalized, or in the same transaction, so the run keeps its
        lease until the task exists and the sweep can finish a run whose worker died in between.
        """
        self.store.open_human_task(self._task(tenant_id, run_id, reason))

    # -- step 11: approve -------------------------------------------------------------------

    def approve(self, tenant_id: str, audit_id: str, principal: Principal, decision: str | None,
                note: str | None = None) -> DecisionResult:
        if principal.role not in (Role.approver, Role.admin) or principal.principal_id == SERVICE_PRINCIPAL.principal_id:
            raise NotAllowed("deciding needs the approver role, and the service can never approve")
        if decision not in ("approve", "decline"):
            raise InvalidDecision(f"decision must be 'approve' or 'decline', got {decision!r}")
        audit = self.store.get_audit(tenant_id, audit_id)
        if audit is None:
            raise KeyError(audit_id)
        now = self.clock.now()
        to = AuditStatus.approved if decision == "approve" else AuditStatus.declined
        ok = self.store.transition_audit(
            tenant_id, audit_id, [AuditStatus.pending_approval], to, now,
            {"decided_by": principal.principal_id, "decided_at": now, "decision": decision,
             "decline_note": (note or "")[:500]}, terminal=(to == AuditStatus.declined))
        if not ok:
            current = self.store.get_audit(tenant_id, audit_id) or {}
            if current.get("status") == AuditStatus.approved and decision == "approve":
                # The worker died after the approval and before the apply, or contention cancelled the apply. The
                # run is finalized, so it has no lease; the approver's retry applies what was approved (entry 47)
                # and says so. Any other decision changes nothing: a recorded approval can't be withdrawn, and the
                # staleness pass applies it anyway (entry 57).
                res = self.executor.apply(tenant_id, audit_id)
                outcome = self._refresh_run_outcome(tenant_id, audit["run_id"])
                status = "applied" if res.status in ("applied", "already_applied") else \
                    "retryable" if res.status == "retryable" else "failed"
                return DecisionResult(status, audit_id, outcome, detail="applied_on_retry" if status == "applied"
                                      else None)
            if current.get("status") in ("approved", "applied", "failed", "declined"):  # what approval can produce
                # A retry after a crash between the write and the run update repairs the run's outcome (case D12).
                outcome = self._refresh_run_outcome(tenant_id, audit["run_id"])
            else:
                # A routed or rejected record never went to approval, so this call must not relabel its run (entry 48).
                outcome = (self.store.get_run(tenant_id, audit["run_id"]) or {}).get("outcome")
            return DecisionResult("already_decided", audit_id, outcome, detail=current.get("status"))
        status = "declined"
        if decision == "approve":
            res = self.executor.apply(tenant_id, audit_id)
            status = "applied" if res.status in ("applied", "already_applied") else \
                "retryable" if res.status == "retryable" else "failed"  # retryable: still approved (entry 51)
        outcome = self._refresh_run_outcome(tenant_id, audit["run_id"])
        return DecisionResult(status, audit_id, outcome)

    def _refresh_run_outcome(self, tenant_id: str, run_id: str) -> str | None:
        if self.store.get_run(tenant_id, run_id) is None:
            return None  # e.g. the seeded record A-2, whose run R-SEED has no record; there is nothing to update
        audits = [a for a in self.store.list_audits(tenant_id, run_id) if a["action"] in WRITE_ACTIONS]
        statuses = {a["status"] for a in audits}
        if not audits:
            return None
        if statuses & {"pending_approval", "approved", "proposed", "applying"}:
            outcome = RunOutcome.PENDING_APPROVAL
        elif statuses <= {"applied", "reverted"}:
            outcome = RunOutcome.APPLIED
        elif "declined" in statuses:
            outcome = RunOutcome.DECLINED
        else:
            outcome = RunOutcome.NEEDS_HUMAN
        # A set can end mixed, e.g. one write applied and another declined. The outcome names the part that needs
        # attention, and the run always lists what did apply.
        applied = [a["audit_id"] for a in audits if a["status"] == "applied"]
        self.store.update_run(tenant_id, run_id, {"outcome": outcome, "applied_audit_ids": applied},
                              (f"outcome:{outcome}", self.clock.now()))
        return outcome

    def approval_view(self, tenant_id: str, audit_id: str) -> dict:
        """What an approver sees, in order: code checks, then the document's fields, then the model's text."""
        a = self.store.get_audit(tenant_id, audit_id)
        if a is None:
            raise KeyError(audit_id)
        run = self.store.get_run(tenant_id, a["run_id"]) or {}
        return {
            "1_code_checks": {"tier": a["tier"], "tier_rules": a["tier_rules"], "checks": a["checks"]},
            "2_proposed_write": {"action": a["action"], "params": a["params"]},
            "3_document_fields_untrusted": run.get("extraction"),
            "4_model_text_not_verified": {"rationale": a.get("rationale_model_text"),
                                          "confidence": a.get("confidence_model_reported")},
        }

    # -- step 14: revert ---------------------------------------------------------------------

    def revert(self, tenant_id: str, audit_id: str, principal: Principal) -> RevertResult:
        if principal.role not in (Role.approver, Role.admin):
            raise NotAllowed("reverting needs the approver or admin role")
        res = self.executor.revert(tenant_id, audit_id, principal.principal_id)
        if res.status in ("reverted", "already_reverted"):
            return RevertResult("REVERTED", already=res.status == "already_reverted")
        return RevertResult("REVERT_REFUSED", res.refusal_reason)

    # -- step 15: staleness and stranded runs ---------------------------------------------------

    def recover_stranded(self, now_iso: str | None = None) -> list[dict]:
        """Resume every run whose lease ran out: its worker died, or its processing event never arrived.

        Without this, a worker killed after claiming a run and before writing any audit record left the run in
        flight for good: a redelivered event saw a claimed run and stopped, and the audit staleness check had
        nothing to find.
        """
        now = now_iso or self.clock.now()
        out = []
        for run in self.store.list_expired_leases(now):
            row = {"tenant_id": run["tenant_id"], "run_id": run["run_id"], "state": run["state"],
                   "lease_until": run["lease_until"]}
            # The index is eventually consistent; a worker may have renewed its lease since. Read the run itself.
            fresh = self.store.get_run(run["tenant_id"], run["run_id"]) or {}
            if fresh.get("lease_until") and fresh["lease_until"] >= now:
                continue
            try:
                row["outcome"] = self.resume(run["tenant_id"], run["run_id"], lease_expired_by=now).outcome
            except Exception as exc:  # noqa: BLE001 - one run that can't be resumed must not stop the others
                row["outcome"], row["error"] = None, repr(exc)[:300]
            out.append(row)
        return out

    def stale(self, now_iso: str | None = None, minutes: int = 15) -> list[dict]:
        now = datetime.strptime((now_iso or self.clock.now())[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        cutoff = (now - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S")
        return [{"tenant_id": a["tenant_id"], "audit_id": a["audit_id"], "status": a["status"],
                 "since": a["open_since"]} for a in self.store.list_open_audits(cutoff)]

    def resume_stale(self, now_iso: str | None = None, minutes: int = 15) -> list[dict]:
        """Resume the run of every stale record that is `proposed` or `approved`, once per run (entry 47).

        A finalized run has no lease, so `recover_stranded` never visits it. Without this, three states were left
        for good: an `approved` record whose worker died before the apply, an auto record left at `proposed` when
        the fail-closed handler finalized its run after an earlier write of the set committed, and a `proposed`
        record on a finalized run whose set was never fully recorded, which `resume` closes as failed. `resume` is
        idempotent, and a record waiting for a person (`pending_approval`) is left alone.
        """
        runs: dict[tuple[str, str], None] = {}
        for row in self.stale(now_iso, minutes):
            if row["status"] not in (AuditStatus.proposed, AuditStatus.approved):
                continue
            audit = self.store.get_audit(row["tenant_id"], row["audit_id"]) or {}
            if audit.get("run_id") and audit.get("status") in (AuditStatus.proposed, AuditStatus.approved):
                runs.setdefault((row["tenant_id"], audit["run_id"]), None)
        out = []
        for tenant_id, run_id in runs:
            row = {"tenant_id": tenant_id, "run_id": run_id}
            try:
                row["outcome"] = self.resume(tenant_id, run_id).outcome
            except Exception as exc:  # noqa: BLE001 - one run that can't be resumed must not stop the others
                row["outcome"], row["error"] = None, repr(exc)[:300]
            out.append(row)
        return out
