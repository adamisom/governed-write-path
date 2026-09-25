"""The policy check and the authority tier. Plain code; the model never picks a tier.

`validate_references` is step 7 (every id a proposal names must exist in this
tenant). `evaluate` is step 8: it runs the checks that the audit record lists,
assigns each proposal a tier, and decides where the run goes.

Tiers can only be raised by the model (with `requires_approval_reason`) and by
low confidence, never lowered. Any forbidden action sends the whole run to a
person, because a forbidden attempt is a sign the model may be compromised.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .schema import (
    FORBIDDEN_ACTIONS,
    WRITE_ACTIONS,
    Action,
    ApplyCreditMemo,
    DocumentKind,
    Extraction,
    HoldInvoice,
    PostPayable,
    ProposalSet,
    RecodeLine,
    RequestHumanReview,
    SendVendorQuery,
    Tier,
)
from .store import DynamoStore
from .world import TEMPLATES, normalize_ref

PASS, FAIL, NA = "pass", "fail", "n/a"

# Run-level reasons for sending a run to a person, most important first.
HUMAN_REASON_ORDER = [
    "cross_tenant",
    "unknown_vendor",
    "duplicate_invoice",
    "duplicate_credit",
    "forbidden_action",
    "multiple_payables",
]


@dataclass
class KeyedContext:
    """What code looked up by key before the proposer ran (step 5)."""

    tenant_id: str
    policy: dict
    document_id: str
    extraction: Extraction
    extraction_flags: list[str]
    vendor: dict | None
    vendor_note: str
    pos: dict[str, dict] = field(default_factory=dict)
    receipts: dict[tuple[str, int], dict] = field(default_factory=dict)
    contracts: dict[str, dict] = field(default_factory=dict)
    payables: dict[str, dict] = field(default_factory=dict)


@dataclass
class ProposalCheck:
    index: int
    action: str
    tier: str
    rules: list[str]
    checks: dict[str, str]
    human_reasons: list[str] = field(default_factory=list)
    target_id: str | None = None


@dataclass
class PolicyDecision:
    route: str  # auto | approval | human
    reason: str | None
    proposals: list[ProposalCheck]


@dataclass
class ReferenceProblems:
    cross_tenant: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.cross_tenant and not self.unknown


# ---------------------------------------------------------------------------
# Step 7: references
# ---------------------------------------------------------------------------


def _referenced_ids(p: Any) -> list[tuple[str, str]]:
    a = p.action
    prm = p.params
    if a == Action.post_payable:
        out = [("vendor", prm.vendor_id)]
        if prm.po_id:
            out.append(("po", prm.po_id))
        if prm.contract_id:
            out.append(("contract", prm.contract_id))
        return out
    if a in (Action.recode_line, Action.apply_credit_memo):
        return [("payable", prm.payable_id)]
    if a == Action.send_vendor_query:
        return [("vendor", prm.vendor_id)]
    if a == Action.hold_invoice:
        return [("document", prm.document_id)]
    return []


def validate_references(ps: ProposalSet, tenant_id: str, store: DynamoStore) -> ReferenceProblems:
    probs = ReferenceProblems()
    getters = {
        "vendor": store.get_vendor,
        "po": store.get_po,
        "contract": store.get_contract,
        "payable": store.get_payable,
        "document": store.get_document,
    }
    for p in ps.proposals:
        if p.action in FORBIDDEN_ACTIONS:
            continue  # rejected by name in step 8; its parameters are never looked up
        for kind, rid in _referenced_ids(p):
            if getters[kind](tenant_id, rid) is not None:
                continue
            owner = store.owner_of(rid)
            if owner is not None and owner != tenant_id:
                probs.cross_tenant.append(rid)
            else:
                probs.unknown.append(f"{kind}:{rid}")
    return probs


# ---------------------------------------------------------------------------
# Step 8: checks and tiers
# ---------------------------------------------------------------------------


def contract_fee_for(contract: dict, on: date) -> int | None:
    iso = on.isoformat()
    for row in contract["price_schedule"]:
        if row["effective_from"] <= iso and (row.get("effective_to") is None or iso <= row["effective_to"]):
            return row["fee_cents"]
    return None


def within_tolerance(actual: int, expected: int, bps: int) -> bool:
    return abs(actual - expected) * 10_000 <= bps * abs(expected)


def sales_tax(goods_cents: int, bps: int) -> int:
    return (goods_cents * bps + 5_000) // 10_000


def _check_post_payable(p: PostPayable, ctx: KeyedContext, store: DynamoStore) -> ProposalCheck:
    prm = p.params
    pol = ctx.policy
    ext = ctx.extraction
    checks: dict[str, str] = {}
    rules: list[str] = []
    human: list[str] = []

    # vendor
    if ctx.vendor is None:
        checks["vendor_resolved"] = FAIL
        human.append("unknown_vendor")
    elif prm.vendor_id != ctx.vendor["vendor_id"]:
        checks["vendor_resolved"] = FAIL
        human.append("vendor_mismatch")
    else:
        checks["vendor_resolved"] = PASS
    vendor = store.get_vendor(ctx.tenant_id, prm.vendor_id) or {}

    if ext.document_kind != DocumentKind.invoice:
        human.append("document_kind_mismatch")

    # duplicate
    dup = store.find_invoice(ctx.tenant_id, prm.vendor_id, normalize_ref(prm.invoice_number))
    checks["not_duplicate"] = FAIL if dup else PASS
    if dup:
        human.append("duplicate_invoice")

    # the proposal must agree with what the reader extracted
    same = (
        ext.invoice_number is not None
        and normalize_ref(prm.invoice_number) == normalize_ref(ext.invoice_number)
        and prm.invoice_date == ext.invoice_date
        and prm.total_cents == ext.total_cents
        and sum(ln.amount_cents for ln in prm.lines) == prm.total_cents
    )
    items = [ln for ln in prm.lines if ln.kind == "item"]
    sources = [ln.source_line for ln in items]
    if sorted(s for s in sources if s is not None) != list(range(1, len(ext.lines) + 1)) or None in sources:
        same = False
    for ln in items:
        if ln.source_line and ln.source_line <= len(ext.lines):
            if ext.lines[ln.source_line - 1].amount_cents != ln.amount_cents:
                same = False
    if sum(ln.amount_cents for ln in prm.lines if ln.kind == "freight") != ext.freight_cents:
        same = False
    if sum(ln.amount_cents for ln in prm.lines if ln.kind == "tax") != ext.tax_cents:
        same = False
    checks["matches_extraction"] = PASS if same else FAIL
    if not same:
        rules.append("proposal_extraction_mismatch")

    checks["extraction_arithmetic"] = FAIL if "arithmetic" in ctx.extraction_flags else PASS
    if "arithmetic" in ctx.extraction_flags:
        rules.append("extraction_arithmetic")
    for flag in ctx.extraction_flags:
        if flag != "arithmetic":
            rules.append(f"extraction_{flag}")
    if ext.conflicts:
        rules.append("reader_conflicts")
    if ext.currency != pol["currency"]:
        rules.append("currency_not_supported")

    # bank details
    if ext.remit_to_bank_last4 and vendor and ext.remit_to_bank_last4 != vendor.get("bank_last4"):
        checks["bank_details_match"] = FAIL
        rules.append("bank_mismatch")
    else:
        checks["bank_details_match"] = PASS if ext.remit_to_bank_last4 else NA

    # accounts
    allowed = set(vendor.get("allowed_accounts", []))
    acct_ok = True
    for ln in prm.lines:
        if ln.kind == "item" and ln.account not in allowed:
            acct_ok = False
        if ln.kind == "freight" and ln.account != pol["freight_account"]:
            acct_ok = False
        if ln.kind == "tax" and ln.account != pol["tax_account"]:
            acct_ok = False
    checks["account_allowed"] = PASS if acct_ok else FAIL
    if not acct_ok:
        rules.append("account_not_allowed")

    # purchase order or contract
    goods = sum(ln.amount_cents for ln in items)
    if prm.po_id:
        po = store.get_po(ctx.tenant_id, prm.po_id)
        po_ok = po is not None and po["vendor_id"] == prm.vendor_id and po["status"] == "open"
        price_ok = qty_ok = po_ok
        if po_ok:
            po_lines = {pl["line_no"]: pl for pl in po["lines"]}
            wanted: dict[int, int] = defaultdict(int)
            for ln in items:
                pl = po_lines.get(ln.po_line_no or -1)
                src = ext.lines[ln.source_line - 1] if ln.source_line and ln.source_line <= len(ext.lines) else None
                if pl is None or src is None:
                    po_ok = price_ok = qty_ok = False
                    continue
                wanted[pl["line_no"]] += src.qty
                if not within_tolerance(ln.amount_cents, src.qty * pl["unit_price_cents"], pol["price_tolerance_bps"]):
                    price_ok = False
            receipts = {r["line_no"]: r for r in store.get_receipts(ctx.tenant_id, prm.po_id)}
            for line_no, qty in wanted.items():
                r = receipts.get(line_no)
                if r is None or qty > r["qty_received"] - r["qty_invoiced"]:
                    qty_ok = False
            if ext.freight_cents > po.get("freight_allowance_cents", 0):
                rules.append("freight_over_allowance")
        checks["po_match"] = PASS if po_ok else FAIL
        checks["price_within_tolerance"] = PASS if price_ok else FAIL
        checks["qty_within_receipt"] = PASS if qty_ok else FAIL
        if not po_ok:
            rules.append("po_mismatch")
        if po_ok and not price_ok:
            rules.append("price_variance")
        if po_ok and not qty_ok:
            rules.append("qty_exceeds_receipt")
    elif prm.contract_id:
        contract = store.get_contract(ctx.tenant_id, prm.contract_id)
        checks["po_match"] = NA
        checks["qty_within_receipt"] = NA
        if contract is None or contract["vendor_id"] != prm.vendor_id:
            checks["contract_match"] = FAIL
            checks["price_within_tolerance"] = FAIL
            rules.append("contract_mismatch")
        else:
            checks["contract_match"] = PASS
            fee = contract_fee_for(contract, prm.invoice_date)
            ok = fee is not None and within_tolerance(goods, fee, pol["price_tolerance_bps"])
            checks["price_within_tolerance"] = PASS if ok else FAIL
            if not ok:
                rules.append("price_variance")
    else:
        checks["po_match"] = FAIL
        rules.append("no_po_or_contract")

    # tax
    if ext.tax_cents:
        ok = ext.tax_cents == sales_tax(goods, pol["sales_tax_bps"])
        checks["tax_correct"] = PASS if ok else FAIL
        if not ok:
            rules.append("tax_mismatch")
    else:
        checks["tax_correct"] = NA

    checks["within_auto_limit"] = PASS if prm.total_cents <= pol["auto_limit_cents"] else FAIL
    if prm.total_cents > pol["auto_limit_cents"]:
        rules.append("over_auto_limit")

    return ProposalCheck(0, p.action, Tier.approval if rules else Tier.auto, rules, checks, human)


def _check_credit_memo(p: ApplyCreditMemo, ctx: KeyedContext, store: DynamoStore) -> ProposalCheck:
    prm = p.params
    ext = ctx.extraction
    checks: dict[str, str] = {}
    rules = ["credit_memo_needs_approval"]
    human: list[str] = []
    payable = store.get_payable(ctx.tenant_id, prm.payable_id) or {}
    if ctx.vendor is None:
        human.append("unknown_vendor")
        checks["vendor_resolved"] = FAIL
    elif payable.get("vendor_id") != ctx.vendor["vendor_id"]:
        human.append("vendor_mismatch")
        checks["vendor_resolved"] = FAIL
    else:
        checks["vendor_resolved"] = PASS
    if ext.document_kind != DocumentKind.credit_memo:
        human.append("document_kind_mismatch")
    vendor_id = payable.get("vendor_id", "")
    if store.find_credit(ctx.tenant_id, vendor_id, normalize_ref(prm.credit_number)):
        human.append("duplicate_credit")
        checks["not_duplicate"] = FAIL
    else:
        checks["not_duplicate"] = PASS
    same = (
        ext.invoice_number is not None
        and normalize_ref(prm.credit_number) == normalize_ref(ext.invoice_number)
        and ext.total_cents is not None
        and prm.amount_cents == abs(ext.total_cents)
    )
    checks["matches_extraction"] = PASS if same else FAIL
    if not same:
        rules.append("proposal_extraction_mismatch")
    if ext.referenced_invoice_numbers:
        refs = {normalize_ref(r) for r in ext.referenced_invoice_numbers}
        ok = normalize_ref(payable.get("invoice_number", "")) in refs
        checks["reference_match"] = PASS if ok else FAIL
        if not ok:
            rules.append("reference_mismatch")
    if payable.get("status") != "open":
        rules.append("payable_not_open")
    balance = payable.get("total_cents", 0) - payable.get("credits_cents", 0)
    checks["within_balance"] = PASS if prm.amount_cents <= balance else FAIL
    if prm.amount_cents > balance:
        rules.append("credit_exceeds_balance")
    return ProposalCheck(0, p.action, Tier.approval, rules, checks, human, prm.payable_id)


def _check_recode(p: RecodeLine, ctx: KeyedContext, store: DynamoStore) -> ProposalCheck:
    prm = p.params
    payable = store.get_payable(ctx.tenant_id, prm.payable_id) or {}
    vendor = store.get_vendor(ctx.tenant_id, payable.get("vendor_id", "")) or {}
    rules: list[str] = []
    checks = {"account_allowed": PASS if prm.account in vendor.get("allowed_accounts", []) else FAIL}
    if checks["account_allowed"] == FAIL:
        rules.append("account_not_allowed")
    lines = payable.get("lines", [])
    if not any(ln["line_no"] == prm.line_no and ln.get("kind", "item") == "item" for ln in lines):
        rules.append("no_such_line")
    if payable.get("status") != "open":
        rules.append("payable_not_open")
    return ProposalCheck(0, p.action, Tier.approval if rules else Tier.auto, rules, checks, [], prm.payable_id)


def _check_vendor_query(p: SendVendorQuery, ctx: KeyedContext) -> ProposalCheck:
    prm = p.params
    human: list[str] = []
    checks: dict[str, str] = {}
    if ctx.vendor is None or prm.vendor_id != ctx.vendor["vendor_id"]:
        human.append("unknown_vendor" if ctx.vendor is None else "vendor_mismatch")
        checks["vendor_resolved"] = FAIL
    else:
        checks["vendor_resolved"] = PASS
    template = TEMPLATES.get(prm.template_id)
    ok = template is not None and set(prm.fields) == set(template)
    if ok:
        for k, typ in template.items():
            v = prm.fields[k]
            if typ is int and not isinstance(v, int):
                ok = False
            if typ is str and (not isinstance(v, str) or "@" in v or "://" in v):
                ok = False
    checks["template_fields"] = PASS if ok else FAIL
    if not ok:
        human.append("bad_template_fields")
    return ProposalCheck(0, p.action, Tier.approval, ["vendor_message_needs_approval"], checks, human, prm.vendor_id)


def _check_hold(p: HoldInvoice, ctx: KeyedContext) -> ProposalCheck:
    human = [] if p.params.document_id == ctx.document_id else ["hold_wrong_document"]
    return ProposalCheck(0, p.action, Tier.auto, [], {"document_match": PASS if not human else FAIL}, human,
                         p.params.document_id)


def evaluate(ps: ProposalSet, ctx: KeyedContext, store: DynamoStore) -> PolicyDecision:
    results: list[ProposalCheck] = []
    model_reason: str | None = None
    for i, p in enumerate(ps.proposals):
        if p.action in FORBIDDEN_ACTIONS:
            pc = ProposalCheck(i, p.action, Tier.forbidden, ["forbidden_action"], {}, ["forbidden_action"])
        elif isinstance(p, RequestHumanReview):
            pc = ProposalCheck(i, p.action, Tier.human, ["model_requested_review"], {}, [])
            model_reason = model_reason or p.params.reason_code
        elif isinstance(p, PostPayable):
            pc = _check_post_payable(p, ctx, store)
        elif isinstance(p, ApplyCreditMemo):
            pc = _check_credit_memo(p, ctx, store)
        elif isinstance(p, RecodeLine):
            pc = _check_recode(p, ctx, store)
        elif isinstance(p, SendVendorQuery):
            pc = _check_vendor_query(p, ctx)
        elif isinstance(p, HoldInvoice):
            pc = _check_hold(p, ctx)
        else:  # pragma: no cover - the enum is closed
            raise AssertionError(p.action)
        pc.index = i
        if pc.target_id is None and isinstance(p, PostPayable):
            pc.target_id = f"{p.params.vendor_id}:{p.params.invoice_number}"
        # Confidence and the model's own request can raise a write's tier, never lower it.
        if p.action in WRITE_ACTIONS:
            if p.confidence < ctx.policy["low_confidence"]:
                pc.rules.append("low_confidence")
            if p.requires_approval_reason:
                pc.rules.append("model_requested_approval")
            if pc.tier == Tier.auto and pc.rules:
                pc.tier = Tier.approval
        results.append(pc)

    # Run-level routing to a person.
    human_reasons: list[str] = []
    for pc in results:
        human_reasons.extend(pc.human_reasons)
    if sum(1 for p in ps.proposals if p.action == Action.post_payable) > 1:
        human_reasons.append("multiple_payables")
    if ctx.vendor is None and ctx.extraction.document_kind in (DocumentKind.invoice, DocumentKind.credit_memo):
        human_reasons.append("unknown_vendor")
    if human_reasons or model_reason:
        ordered = [r for r in HUMAN_REASON_ORDER if r in human_reasons]
        reason = ordered[0] if ordered else (human_reasons[0] if human_reasons else model_reason)
        return PolicyDecision("human", reason, results)

    writes = [pc for pc in results if pc.action in WRITE_ACTIONS]
    if any(pc.tier == Tier.approval for pc in writes):
        for pc in writes:
            if pc.tier == Tier.auto:
                pc.tier = Tier.approval
                pc.rules.append("set_level_tier")
        first = next(pc for pc in writes if pc.rules)
        return PolicyDecision("approval", first.rules[0], results)
    return PolicyDecision("auto", None, results)
