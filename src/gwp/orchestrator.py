"""The orchestrator: ordinary code with an explicit sequence of steps. It decides every next step.

The model never chooses a tier, never chooses the next step, and never calls a
write. Steps 3 (read) and 6 (propose) are the only model calls; everything else
is here or in `policy` and `executor`.

A failure fails closed: a model timeout, a throttle, an invalid output or an
unexpected exception ends the run as NEEDS_HUMAN with no write, never as an
auto-apply.

This module imports no agent framework. It talks to the models only through the
`Reader` and `Proposer` interfaces in `gwp.agents`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from pydantic import ValidationError

from .agents import ModelCallFailed, Proposer, ProposerInput, Reader
from .blobs import BlobStore
from .executor import Executor, execution_key
from .pdftext import extract_text
from .policy import KeyedContext, contract_fee_for, evaluate, validate_references
from .retrieval import BM25Retriever, Retriever
from .runtime import Clock, Ids, sha256_hex
from .schema import (
    FORBIDDEN_ACTIONS,
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
from .world import tenant_policy

CODE_VERSION = "gwp-0.1.0"
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024


class NotAllowed(Exception):
    pass


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


@dataclass
class DecisionResult:
    status: str  # applied | declined | already_decided | failed
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
                 search_enabled: bool = True):
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
        run = {"tenant_id": tenant_id, "run_id": self.ids.new("R"), "document_id": document_id, "run_key": run_key,
               "state": "received", "started_at": now, "outcome": None,
               "history": [{"state": "received", "at": now}]}
        run, created = self.store.create_run(run, run_key)
        if not created:
            return UploadResult(run["run_id"], run["document_id"], True, RunOutcome.DUPLICATE_UPLOAD)
        self.store.put_document({"tenant_id": tenant_id, "document_id": document_id, "sha256": doc_sha,
                                 "content_type": content_type, "storage_key": storage_key,
                                 "uploaded_by": principal.principal_id, "uploaded_at": now, "size": len(data)})
        return UploadResult(run["run_id"], document_id, False)

    # -- steps 2 to 13: process --------------------------------------------------------

    def process(self, tenant_id: str, run_id: str) -> RunResult:
        run = self.store.get_run(tenant_id, run_id)
        if run is None:
            raise KeyError(run_id)
        if run["state"] != "received":
            return RunResult(run_id, run.get("outcome") or "", run.get("reason"))
        t0 = self.clock.monotonic()
        trace: dict = {"model_calls": [], "retrieved": [], "searches": [], "proposal_attempts": []}
        try:
            return self._process(tenant_id, run, trace, t0)
        except Exception as exc:  # noqa: BLE001 - fail closed on any bug
            return self._finish(tenant_id, run_id, RunOutcome.NEEDS_HUMAN, "internal_error", trace, t0,
                                extra={"error": repr(exc)[:500]})

    def _process(self, tenant_id: str, run: dict, trace: dict, t0: float) -> RunResult:
        run_id = run["run_id"]
        doc = self.store.get_document(tenant_id, run["document_id"])
        assert doc is not None
        policy = tenant_policy(self.store, tenant_id)
        tenant = self.store.get_tenant(tenant_id) or {}

        # Step 2: text extraction.
        text, pages = extract_text(self.blobs.get(doc["storage_key"]), doc["content_type"])
        self.store.update_run(tenant_id, run_id, {"state": "text_extracted", "page_count": pages,
                                                  "char_count": len(text)}, ("text_extracted", self.clock.now()))

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
            try:
                ps = ProposalSet.model_validate(pr.raw)
            except ValidationError as ve:
                feedback = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}"
                                     for err in ve.errors()[:5])
                trace["proposal_attempts"][-1]["error"] = feedback
                continue
            refs = validate_references(ps, tenant_id, self.store)
            if refs.cross_tenant:
                proposal_set, cross_tenant = ps, refs.cross_tenant
                break
            if refs.unknown:
                feedback = f"unknown ids: {', '.join(refs.unknown)}"
                trace["proposal_attempts"][-1]["error"] = feedback
                continue
            proposal_set = ps
            break
        if proposal_set is None:
            return self._finish(tenant_id, run_id, RunOutcome.NEEDS_HUMAN, failure, trace, t0)

        # Step 8: policy check and tier.
        if cross_tenant:
            decision = None
            route, reason = "human", "cross_tenant"
        else:
            decision = evaluate(proposal_set, ctx, self.store)
            route, reason = decision.route, decision.reason

        # Step 9: one audit record per proposal, before anything is visible.
        audit_ids = []
        for i, p in enumerate(proposal_set.proposals):
            pc = decision.proposals[i] if decision else None
            audit = self._audit_record(tenant_id, run_id, doc, extraction, p, pc, policy, tenant, trace, cross_tenant,
                                       route, reason)
            self.store.put_audit(audit)
            audit_ids.append(audit["audit_id"])

        # Step 10: route.
        now = self.clock.now()
        if route == "human":
            for aid, p in zip(audit_ids, proposal_set.proposals):
                status = AuditStatus.rejected if (p.action in FORBIDDEN_ACTIONS or cross_tenant) else AuditStatus.routed
                self.store.transition_audit(tenant_id, aid, ["proposed"], status, now,
                                            {"decided_by": "system", "decided_at": now, "route_reason": reason},
                                            terminal=True)
            self.store.put_human_task({"tenant_id": tenant_id, "task_id": self.ids.new("H"), "run_id": run_id,
                                       "reason_code": reason, "status": "open", "created_at": now})
            return self._finish(tenant_id, run_id, RunOutcome.ROUTED_TO_HUMAN, reason, trace, t0, audit_ids,
                                decision)
        if route == "approval":
            for aid in audit_ids:
                self.store.transition_audit(tenant_id, aid, ["proposed"], AuditStatus.pending_approval, now)
            return self._finish(tenant_id, run_id, RunOutcome.PENDING_APPROVAL, reason, trace, t0, audit_ids,
                                decision)

        # Step 12: execute the auto tier.
        results = [self.executor.apply(tenant_id, aid) for aid in audit_ids]
        if VendorRequest.bank_details_change in extraction.vendor_requests:
            # The written policy sends any bank change request to a person. The agent can't act on it, and the
            # remit-to on this document matched the vendor record, so the payable posts and a person follows up.
            self.store.put_human_task({"tenant_id": tenant_id, "task_id": self.ids.new("H"), "run_id": run_id,
                                       "reason_code": "vendor_requested_bank_change", "status": "open",
                                       "created_at": self.clock.now()})
        if all(r.status in ("applied", "already_applied") for r in results):
            return self._finish(tenant_id, run_id, RunOutcome.APPLIED, None, trace, t0, audit_ids, decision)
        errors = ",".join(r.error or r.status for r in results if r.status not in ("applied", "already_applied"))
        return self._finish(tenant_id, run_id, RunOutcome.NEEDS_HUMAN, "apply_failed", trace, t0, audit_ids,
                            decision, extra={"error": errors})

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
        out["posted_payables"] = [
            {"payable_id": p["payable_id"], "invoice_number": p["invoice_number"],
             "invoice_date": p["invoice_date"], "po_id": p.get("po_id"), "status": p["status"],
             "total_cents": p["total_cents"], "credits_cents": p.get("credits_cents", 0),
             "lines": [{"description": ln.get("description", ""), "qty": ln.get("qty"),
                        "account": ln["account"], "amount_cents": ln["amount_cents"]} for ln in p["lines"]]}
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
                        "description": src.description if src else ln.kind,
                        "qty": src.qty if src else 0, "source_line": ln.source_line})
        return out

    # -- step 13: finalize ----------------------------------------------------------------

    def _finish(self, tenant_id, run_id, outcome, reason, trace, t0, audit_ids=None, decision=None, extra=None):
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
        fields.update(extra or {})
        self.store.update_run(tenant_id, run_id, fields, (f"finalized:{outcome}", fields["ended_at"]))
        return RunResult(run_id, outcome, reason, audit_ids or [])

    def resume(self, tenant_id: str, run_id: str) -> RunResult:
        """Redeliver a run whose worker died after step 9: re-run execution for its audit records and finalize.

        Safe to call any number of times. Execution keys make a repeated apply a no-op.
        """
        run = self.store.get_run(tenant_id, run_id)
        if run is None:
            raise KeyError(run_id)
        if run.get("state") == "finalized":
            return RunResult(run_id, run["outcome"], run.get("reason"), run.get("audit_ids", []))
        audits = self.store.list_audits(tenant_id, run_id)
        if not audits:
            # Died before any audit record existed, so nothing was proposed or written. Start again.
            return RunResult(run_id, RunOutcome.NEEDS_HUMAN, "interrupted")
        for a in audits:
            if a["status"] == "proposed" and a["tier"] == Tier.auto:
                self.executor.apply(tenant_id, a["audit_id"])
        outcome = self._refresh_run_outcome(tenant_id, run_id) or RunOutcome.NEEDS_HUMAN
        calls = audits[0].get("model_calls", [])  # the audit record kept the model calls the dead worker made
        cost = round(sum(c["cost_usd"] for c in calls if c.get("cost_usd") is not None), 8)
        self.store.update_run(tenant_id, run_id, {"state": "finalized", "outcome": outcome, "model_calls": calls,
                                                  "cost_usd": cost, "retrieved": audits[0].get("retrieved", []),
                                                  "audit_ids": [a["audit_id"] for a in audits]},
                              ("finalized_on_resume", self.clock.now()))
        return RunResult(run_id, outcome, None, [a["audit_id"] for a in audits])

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
            return DecisionResult("already_decided", audit_id, detail=current.get("status"))
        status = "declined"
        if decision == "approve":
            res = self.executor.apply(tenant_id, audit_id)
            status = "applied" if res.status in ("applied", "already_applied") else "failed"
        outcome = self._refresh_run_outcome(tenant_id, audit["run_id"])
        return DecisionResult(status, audit_id, outcome)

    def _refresh_run_outcome(self, tenant_id: str, run_id: str) -> str | None:
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
        self.store.update_run(tenant_id, run_id, {"outcome": outcome}, (f"outcome:{outcome}", self.clock.now()))
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

    # -- step 15: staleness --------------------------------------------------------------------

    def stale(self, now_iso: str | None = None, minutes: int = 15) -> list[dict]:
        now = datetime.strptime((now_iso or self.clock.now())[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        cutoff = (now - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S")
        return [{"tenant_id": a["tenant_id"], "audit_id": a["audit_id"], "status": a["status"],
                 "since": a["open_since"]} for a in self.store.list_open_audits(cutoff)]
