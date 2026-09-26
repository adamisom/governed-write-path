"""The deterministic write path: store, policy, executor and orchestrator, with scripted models."""

import pytest
from helpers import APPROVER, C01_DOC, UPLOADER, build, c01_post, run_doc

from gwp.evals import documents
from gwp.executor import Executor, execution_key
from gwp.orchestrator import InvalidDecision, NotAllowed
from gwp.policy import sales_tax, within_tolerance
from gwp.runtime import FakeClock, Ids
from gwp.schema import SERVICE_PRINCIPAL, Principal, ProposalSet, Role
from gwp.store import TransactionConflict, op_put

# -- schema ------------------------------------------------------------------------


def test_forbidden_actions_parse_so_attempts_can_be_counted():
    ps = ProposalSet.model_validate({"proposals": [
        {"action": "schedule_payment", "params": {"payable_id": "P-2", "anything": 1}},
        {"action": "update_vendor_bank_details", "params": {"vendor_id": "V-101", "iban": "x"}}]})
    assert [p.action for p in ps.proposals] == ["schedule_payment", "update_vendor_bank_details"]


@pytest.mark.parametrize("bad", [
    {"proposals": []},
    {"proposals": [c01_post()] * 4},
    {"proposals": [c01_post(extra_field=1)]},
    {"proposals": [c01_post(confidence=1.5)]},
    {"proposals": [{"action": "wire_money", "params": {}}]},
    {"proposals": [c01_post(params={"lines": [{"account": "61000", "amount_cents": 1}]})]},
])
def test_invalid_proposals_are_rejected_by_the_schema(bad):
    with pytest.raises(Exception):
        ProposalSet.model_validate(bad)


# -- arithmetic -----------------------------------------------------------------


def test_tolerance_and_tax_arithmetic():
    assert within_tolerance(19540, 19250, 200)  # 1.5% over
    assert not within_tolerance(68900, 65000, 200)  # 6% over
    assert within_tolerance(19635, 19250, 200)  # exactly 2%
    assert sales_tax(97500, 825) == 8044  # 80.4375 rounds half up to 80.44


# -- happy path and tiers -----------------------------------------------------------


def test_c01_applies_with_audit_record_and_ledger_entry(store):
    orch, res, _, _ = run_doc(store, C01_DOC, [c01_post()])
    assert res.outcome == "APPLIED"
    (audit,) = store.list_audits("T1", res.run_id)
    assert audit["status"] == "applied" and audit["tier"] == "auto"
    assert [h["state"] for h in audit["history"]] == ["proposed", "applied"]
    assert audit["checks"]["price_within_tolerance"] == "pass"
    assert audit["model_calls"] and audit["retrieved"]
    payable = store.get_payable("T1", audit["apply_record"]["payable_id"])
    assert payable["total_cents"] == 84250 and payable["status"] == "open"
    receipts = {r["line_no"]: r["qty_invoiced"] for r in store.get_receipts("T1", "PO-7001")}
    assert receipts == {1: 20, 2: 5}


def test_high_confidence_never_lowers_a_tier_and_low_confidence_raises_it(store):
    _, res, _, _ = run_doc(store, {**C01_DOC, "invoice_number": "INV-7"},
                           [c01_post(confidence=0.3, params={"invoice_number": "INV-7"})])
    assert res.outcome == "PENDING_APPROVAL"
    audit = store.list_audits("T1", res.run_id)[0]
    assert "low_confidence" in audit["tier_rules"]


def test_model_can_raise_the_tier(store):
    _, res, _, _ = run_doc(store, C01_DOC, [c01_post(requires_approval_reason="unsure")])
    assert res.outcome == "PENDING_APPROVAL"


def test_forbidden_action_routes_the_whole_set_to_a_person(store):
    _, res, _, _ = run_doc(store, C01_DOC, [c01_post(), {"action": "schedule_payment", "params": {"payable_id": "x"}}])
    assert res.outcome == "ROUTED_TO_HUMAN" and res.reason == "forbidden_action"
    statuses = sorted((a["action"], a["tier"], a["status"]) for a in store.list_audits("T1", res.run_id))
    assert statuses == [("post_payable", "human", "routed"), ("schedule_payment", "forbidden", "rejected")]
    assert store.list_payables("T1", "V-101")[0]["payable_id"] == "P-2"  # nothing new posted


def test_vendor_query_fields_cannot_carry_an_address(store):
    doc = {**C01_DOC, "total_cents": 85250}
    q = {"action": "send_vendor_query", "params": {"vendor_id": "V-101", "template_id": "total_mismatch", "fields": {
        "invoice_number": "attacker@evil.example", "stated_total_cents": 85250, "computed_total_cents": 84250}}}
    _, res, _, _ = run_doc(store, doc, [q])
    assert res.outcome == "ROUTED_TO_HUMAN" and res.reason == "bad_template_fields"
    assert store.list_outbox("T1") == []


def test_proposal_amounts_must_match_the_extraction(store):
    post = c01_post(params={"total_cents": 94250, "lines": [
        {"source_line": 1, "po_line_no": 1, "account": "6100", "amount_cents": 75000},
        {"source_line": 2, "po_line_no": 2, "account": "6100", "amount_cents": 19250}]})
    _, res, _, _ = run_doc(store, C01_DOC, [post])
    assert res.outcome == "PENDING_APPROVAL"
    assert "proposal_extraction_mismatch" in store.list_audits("T1", res.run_id)[0]["tier_rules"]


# -- approval ------------------------------------------------------------------------


def _pending(store):
    doc = {**C01_DOC, "invoice_number": "INV-9"}
    orch, res, _, _ = run_doc(store, doc, [c01_post(requires_approval_reason="check", params={"invoice_number": "INV-9"})])
    return orch, store.list_audits("T1", res.run_id)[0]["audit_id"]


@pytest.mark.parametrize("decision", [None, "", "yes", "APPROVE", "approved"])
def test_missing_or_malformed_decision_blocks_the_write(store, decision):
    orch, aid = _pending(store)
    with pytest.raises(InvalidDecision):
        orch.approve("T1", aid, APPROVER, decision)
    assert store.get_audit("T1", aid)["status"] == "pending_approval"
    assert len(store.list_payables("T1", "V-101")) == 1


@pytest.mark.parametrize("who", [SERVICE_PRINCIPAL, Principal(principal_id="u", role=Role.uploader),
                                 Principal(principal_id="svc:gwp", role=Role.approver)])
def test_only_an_approver_who_is_not_the_service_can_approve(store, who):
    orch, aid = _pending(store)
    with pytest.raises(NotAllowed):
        orch.approve("T1", aid, who, "approve")


def test_approval_view_puts_code_checks_before_model_text(store):
    orch, aid = _pending(store)
    view = orch.approval_view("T1", aid)
    assert list(view)[0] == "1_code_checks" and list(view)[-1] == "4_model_text_not_verified"


def test_approval_is_compare_and_set(store):
    orch, aid = _pending(store)
    assert orch.approve("T1", aid, APPROVER, "decline").status == "declined"
    assert orch.approve("T1", aid, APPROVER, "approve").status == "already_decided"
    assert len(store.list_payables("T1", "V-101")) == 1


def test_executor_refuses_an_approval_tier_write_that_was_not_approved(store):
    orch, aid = _pending(store)
    assert orch.executor.apply("T1", aid).status == "conflict"
    assert len(store.list_payables("T1", "V-101")) == 1


def _hand_built_audit(store, audit_id, tier, status, invoice_number="INV-HAND"):
    """An audit record written straight to the store, as a buggy or future caller might leave one."""
    now = "2026-09-25T12:00:00Z"
    audit = {
        "audit_id": audit_id, "tenant_id": "T1", "run_id": "R-HAND", "proposal_id": f"R-HAND:{audit_id}",
        "idempotency_key": audit_id, "created_at": now, "updated_at": now, "document_id": "D-HAND",
        "document_sha256": "0", "action": "post_payable",
        "params": {"vendor_id": "V-101", "invoice_number": invoice_number, "invoice_date": "2026-08-03",
                   "po_id": "PO-7001", "total_cents": 84250},
        "apply_input": {"lines": [
            {"line_no": 1, "kind": "item", "account": "6100", "amount_cents": 65000, "po_line_no": 1, "qty": 20,
             "source_line": 1},
            {"line_no": 2, "kind": "item", "account": "6100", "amount_cents": 19250, "po_line_no": 2, "qty": 5,
             "source_line": 2}]},
        "tier": tier, "tier_rules": [], "checks": {}, "status": status, "write_ids": [], "history": [],
        "open_flag": "OPEN", "open_since": now,
    }
    store.put_audit(audit)
    return audit


@pytest.mark.parametrize("tier,status", [("human", "proposed"), ("forbidden", "proposed"), ("approval", "proposed"),
                                         ("auto", "approved"), ("human", "approved")])
def test_executor_applies_only_auto_proposed_or_approval_approved(store, tier, status):
    """Audit finding 3: a routed or forbidden record at status proposed used to apply."""
    _hand_built_audit(store, "A-H1", tier, status)
    res = Executor(store, FakeClock(), Ids()).apply("T1", "A-H1")
    assert res.status == "conflict"
    assert [p["payable_id"] for p in store.list_payables("T1", "V-101")] == ["P-2"]
    assert store.get_audit("T1", "A-H1")["status"] == status


def test_the_tier_is_part_of_the_apply_transaction(store):
    """Even a caller that read a stale or forged tier can't apply: the transaction checks the stored tier."""
    stored = _hand_built_audit(store, "A-H2", "human", "proposed")
    real_get_audit = store.get_audit
    store.get_audit = lambda t, a: dict(stored, tier="auto")  # the caller believes the record is auto
    try:
        res = Executor(store, FakeClock(), Ids()).apply("T1", "A-H2")
    finally:
        store.get_audit = real_get_audit
    assert res.status == "conflict"
    assert [p["payable_id"] for p in store.list_payables("T1", "V-101")] == ["P-2"]
    assert store.get_key("T1", "INVKEY#V-101#INVHAND") is None


# -- idempotency ----------------------------------------------------------------------


def test_upload_of_identical_bytes_returns_the_first_run(store):
    orch, _, _ = build(store, [], [])
    pdf = documents.render(C01_DOC)
    a = orch.upload("T1", pdf, "application/pdf", UPLOADER)
    b = orch.upload("T1", pdf, "application/pdf", UPLOADER)
    assert not a.duplicate and b.duplicate and a.run_id == b.run_id
    # The same bytes in another tenant are a different run.
    c = orch.upload("T2", pdf, "application/pdf", UPLOADER)
    assert not c.duplicate and c.run_id != a.run_id


def test_redelivered_execution_with_a_stale_view_is_stopped_by_the_transaction(store):
    """Two workers both think the write is still to do. The execution key lets exactly one commit."""
    orch, res, _, _ = run_doc(store, C01_DOC, [c01_post(requires_approval_reason="x")])
    aid = store.list_audits("T1", res.run_id)[0]["audit_id"]
    orch.approve("T1", aid, APPROVER, "approve")
    stale = dict(store.get_audit("T1", aid), status="approved")
    real_get_audit, real_get_key = store.get_audit, store.get_key
    store.get_audit = lambda t, a: stale  # the second worker read before the first committed
    store.get_key = lambda t, sk: None if sk.startswith("XKEY#") else real_get_key(t, sk)
    try:
        second = Executor(store, FakeClock(), Ids(start=5000)).apply("T1", aid)
    finally:
        store.get_audit, store.get_key = real_get_audit, real_get_key
    assert second.status == "already_applied"
    assert len([p for p in store.list_payables("T1", "V-101") if p["invoice_number"] == "INV-6001"]) == 1
    assert len(store.list_ledger("T1")) == 2  # the seeded E-2 and this write's one entry


def test_failed_domain_precondition_leaves_no_partial_write(store):
    """If any part of the transaction fails, the audit record is not marked applied and nothing is visible."""
    orch, res, _, _ = run_doc(store, C01_DOC, [c01_post(requires_approval_reason="x")])
    aid = store.list_audits("T1", res.run_id)[0]["audit_id"]
    # Someone else posts the same invoice number first (a race the policy check could not see).
    store.transact_domain([op_put(store.t("records"), {"pk": "TENANT#T1", "sk": "INVKEY#V-101#INV6001",
                                                        "kind": "invoice_key", "payable_id": "P-X"})])
    out = orch.approve("T1", aid, APPROVER, "approve")
    assert out.status == "failed"
    audit = store.get_audit("T1", aid)
    assert audit["status"] == "failed" and audit["error"].startswith("precondition_failed")
    assert len(store.list_payables("T1", "V-101")) == 1
    assert {r["qty_invoiced"] for r in store.get_receipts("T1", "PO-7001")} == {0}


def test_transaction_conflict_reports_one_reason_per_operation(store):
    ops = [op_put(store.t("records"), {"pk": "X", "sk": "1"}, "attribute_not_exists(pk)"),
           op_put(store.t("records"), {"pk": "TENANT#T1", "sk": "TENANT"}, "attribute_not_exists(pk)")]
    with pytest.raises(TransactionConflict) as e:
        store.transact_domain(ops)
    assert e.value.reasons == ["None", "ConditionalCheckFailed"]
    assert store._get("records", "X", "1") is None


# -- revert -----------------------------------------------------------------------


def test_revert_is_a_compensating_entry_and_is_idempotent(store):
    orch, res, _, _ = run_doc(store, C01_DOC, [c01_post()])
    aid = res.audit_ids[0]
    assert orch.revert("T1", aid, APPROVER).outcome == "REVERTED"
    again = orch.revert("T1", aid, APPROVER)
    assert again.outcome == "REVERTED" and again.already
    entries = [e for e in store.list_ledger("T1") if e["payable_id"] != "P-2"]
    assert len(entries) == 2
    net = {}
    for e in entries:
        for ln in e["lines"]:
            net[ln["account"]] = net.get(ln["account"], 0) + ln["debit_cents"] - ln["credit_cents"]
    assert set(net.values()) == {0}
    assert {r["qty_invoiced"] for r in store.get_receipts("T1", "PO-7001")} == {0}
    assert store.list_outbox("T1") == []


def test_revert_of_a_queued_message_cancels_it_but_a_sent_one_is_refused(store):
    doc = {**C01_DOC, "total_cents": 85250}
    q = {"action": "send_vendor_query", "params": {"vendor_id": "V-101", "template_id": "total_mismatch", "fields": {
        "invoice_number": "INV-6001", "stated_total_cents": 85250, "computed_total_cents": 84250}}}
    orch, res, _, _ = run_doc(store, doc, [q])
    aid = res.audit_ids[0]
    orch.approve("T1", aid, APPROVER, "approve")
    assert store.list_outbox("T1")[0]["to"] == "ar@pinestreetpaper.example"
    assert orch.revert("T1", aid, APPROVER).outcome == "REVERTED"
    assert store.list_outbox("T1")[0]["status"] == "cancelled"
    assert orch.executor.deliver_outbox("T1") == []


def test_revert_of_a_payable_with_no_dependent_write_succeeds(store):
    orch, _, _ = build(store, [], [])
    assert orch.revert("T1", "A-2", APPROVER).outcome == "REVERTED"  # nothing depends on P-2 yet


def test_revert_refused_while_a_later_write_depends_on_it(store):
    """Audit finding 11: this test used to assert REVERTED. A credit memo against P-2 now comes first."""
    memo = {"kind": "credit_memo", "vendor_name": "Pine Street Paper Supply", "vendor_tax_id": "84-2210937",
            "invoice_number": "CM-17", "invoice_date": "2026-08-12", "referenced_invoice_numbers": ["INV-5521"],
            "lines": [{"description": "Toner cartridge, black (returned)", "qty": 2, "unit_price_cents": 3850}]}
    credit = {"action": "apply_credit_memo", "params": {"credit_number": "CM-17", "payable_id": "P-2",
                                                        "amount_cents": 7700}, "rationale": "r", "confidence": 0.9}
    orch, res, _, _ = run_doc(store, memo, [credit])
    assert orch.approve("T1", res.audit_ids[0], APPROVER, "approve").status == "applied"
    out = orch.revert("T1", "A-2", APPROVER)
    assert out.outcome == "REVERT_REFUSED" and out.reason == "dependent_write"
    assert store.get_payable("T1", "P-2")["status"] == "open"


# -- recode and hold: apply and revert (audit findings 2 and 10) ------------------------------------

KESTREL_DOC = {"kind": "invoice", "vendor_name": "Kestrel Office Furniture", "vendor_tax_id": "84-3310442",
               "remit_to_bank_last4": "7730", "invoice_number": "KOF-9", "invoice_date": "2026-08-10",
               "po_number": "PO-7009", "lines": [{"description": "Filing cabinet, 4 drawer", "qty": 4,
                                                   "unit_price_cents": 21000}]}
KESTREL_POST = {"action": "post_payable", "params": {
    "vendor_id": "V-102", "invoice_number": "KOF-9", "invoice_date": "2026-08-10", "po_id": "PO-7009",
    "total_cents": 84000, "lines": [{"source_line": 1, "po_line_no": 1, "account": "6150", "amount_cents": 84000}]},
    "rationale": "ok", "confidence": 0.9}


def _letter(n):
    return {"kind": "letter", "vendor_name": "Kestrel Office Furniture", "vendor_tax_id": "84-3310442",
            "letter_date": "2026-08-20", "body": [f"Coding note {n} for invoice KOF-9."]}


def _recode(pid, account):
    return {"action": "recode_line", "params": {"payable_id": pid, "line_no": 1, "account": account},
            "rationale": "recode", "confidence": 0.9}


def _kestrel_with_recodes(store, accounts):
    """Post KOF-9 at 6150, then one letter run per account, each proposing a recode of line 1. One orchestrator,
    so ids don't collide."""
    docs = [KESTREL_DOC] + [_letter(i) for i in range(len(accounts))]
    orch, rm, pm = build(store, [{"tool": "Extraction", "input": documents.faithful_extraction(d)} for d in docs],
                         [{"tool": "propose_write", "input": {"proposals": [KESTREL_POST]}}])
    up = orch.upload("T1", documents.render(KESTREL_DOC), "application/pdf", UPLOADER)
    res = orch.process("T1", up.run_id)
    assert res.outcome == "APPLIED"
    pid = store.get_audit("T1", res.audit_ids[0])["apply_record"]["payable_id"]
    recodes = []
    for i, acct in enumerate(accounts):
        pm.turns.append({"tool": "propose_write", "input": {"proposals": [_recode(pid, acct)]}})
        up = orch.upload("T1", documents.render(_letter(i)), "application/pdf", UPLOADER)
        r = orch.process("T1", up.run_id)
        assert r.outcome == "APPLIED", r
        recodes.append(r.audit_ids[0])
    return orch, pid, res.audit_ids[0], recodes


def _net(store, pid):
    net: dict = {}
    for e in store.list_ledger("T1"):
        if e["payable_id"] == pid:
            for ln in e["lines"]:
                net[ln["account"]] = net.get(ln["account"], 0) + ln["debit_cents"] - ln["credit_cents"]
    return {a: v for a, v in net.items() if v}


def test_recode_applies_and_reverts(store):
    orch, pid, post_aid, (rec,) = _kestrel_with_recodes(store, ["1500"])
    assert store.get_payable("T1", pid)["lines"][0]["account"] == "1500"
    assert store.get_audit("T1", rec)["tier"] == "auto"
    assert _net(store, pid) == {"1500": 84000, "2000": -84000}
    # The payable can't be reverted while the recode depends on it.
    assert orch.revert("T1", post_aid, APPROVER).reason == "dependent_write"
    assert orch.revert("T1", rec, APPROVER).outcome == "REVERTED"
    assert store.get_payable("T1", pid)["lines"][0]["account"] == "6150"
    assert _net(store, pid) == {"6150": 84000, "2000": -84000}
    assert orch.revert("T1", post_aid, APPROVER).outcome == "REVERTED"


def test_reverting_an_earlier_recode_is_refused_while_a_later_one_on_the_line_stands(store):
    """Audit finding 2: reverting recode A after recode B left the line at 1500 while the ledger said 6150."""
    orch, pid, _, (a, b) = _kestrel_with_recodes(store, ["1500", "6150"])
    out = orch.revert("T1", a, APPROVER)
    assert out.outcome == "REVERT_REFUSED" and out.reason == "dependent_write"
    assert store.get_payable("T1", pid)["lines"][0]["account"] == "6150"
    assert orch.revert("T1", b, APPROVER).outcome == "REVERTED"
    assert store.get_payable("T1", pid)["lines"][0]["account"] == "1500"
    assert _net(store, pid) == {"1500": 84000, "2000": -84000}
    assert orch.revert("T1", a, APPROVER).outcome == "REVERTED"
    line = store.get_payable("T1", pid)["lines"][0]
    assert line["account"] == "6150" and "last_recode_write_id" not in line
    assert _net(store, pid) == {"6150": 84000, "2000": -84000}  # the payable and the ledger agree


def test_hold_applies_and_reverts_by_release(store):
    doc = {**C01_DOC, "invoice_number": "INV-HOLD"}
    orch, rm, pm = build(store, [{"tool": "Extraction", "input": documents.faithful_extraction(doc)}], [])
    up = orch.upload("T1", documents.render(doc), "application/pdf", UPLOADER)
    pm.turns.append({"tool": "propose_write", "input": {"proposals": [
        {"action": "hold_invoice", "params": {"document_id": up.document_id, "reason_code": "awaiting_receipt"},
         "rationale": "hold", "confidence": 0.9}]}})
    res = orch.process("T1", up.run_id)
    assert res.outcome == "APPLIED"
    audit = store.get_audit("T1", res.audit_ids[0])
    assert (audit["tier"], audit["status"]) == ("auto", "applied")
    assert store.get_key("T1", f"HOLD#{up.document_id}")["status"] == "on_hold"
    assert orch.revert("T1", res.audit_ids[0], APPROVER).outcome == "REVERTED"
    assert store.get_key("T1", f"HOLD#{up.document_id}")["status"] == "released"
    assert orch.revert("T1", res.audit_ids[0], APPROVER).already


# -- degradation and fail-closed ------------------------------------------------------------


def test_an_unexpected_error_fails_closed(store):
    ext = documents.faithful_extraction(C01_DOC)
    orch, _, _ = build(store, [{"tool": "Extraction", "input": ext}],
                       [{"tool": "propose_write", "input": {"proposals": [c01_post()]}}])
    orch.retriever_factory = lambda t: (_ for _ in ()).throw(RuntimeError("index offline"))
    up = orch.upload("T1", documents.render(C01_DOC), "application/pdf", UPLOADER)
    res = orch.process("T1", up.run_id)
    assert res.outcome == "NEEDS_HUMAN" and res.reason == "internal_error"
    assert len(store.list_payables("T1", "V-101")) == 1


def test_proposer_timeout_twice_needs_a_human_and_records_both_attempts(store):
    ext = documents.faithful_extraction(C01_DOC)
    orch, _, _ = build(store, [{"tool": "Extraction", "input": ext}], [{"raise": "timeout"}, {"raise": "timeout"}])
    up = orch.upload("T1", documents.render(C01_DOC), "application/pdf", UPLOADER)
    res = orch.process("T1", up.run_id)
    assert res.outcome == "NEEDS_HUMAN" and res.reason == "model_timeout"
    calls = store.get_run("T1", up.run_id)["model_calls"]
    assert [c["status"] for c in calls] == ["ok", "timeout", "timeout"]
    assert all(c["cost_usd"] > 0 for c in calls)  # failed attempts are costed from an input estimate


def test_unknown_reference_gets_one_retry_then_needs_a_human(store):
    bad = c01_post(params={"po_id": "PO-9999"})
    ext = documents.faithful_extraction(C01_DOC)
    orch, _, _ = build(store, [{"tool": "Extraction", "input": ext}],
                       [{"tool": "propose_write", "input": {"proposals": [bad]}},
                        {"tool": "propose_write", "input": {"proposals": [c01_post()]}}])
    up = orch.upload("T1", documents.render(C01_DOC), "application/pdf", UPLOADER)
    res = orch.process("T1", up.run_id)
    assert res.outcome == "APPLIED"
    attempts = store.get_run("T1", up.run_id)["proposal_attempts"]
    assert "unknown ids: po:PO-9999" in attempts[0]["error"]


# -- staleness -----------------------------------------------------------------------------


def test_staleness_check_lists_old_non_terminal_records(store):
    clock = FakeClock()
    ext = documents.faithful_extraction(C01_DOC)
    orch, _, _ = build(store, [{"tool": "Extraction", "input": ext}],
                       [{"tool": "propose_write", "input": {"proposals": [c01_post(requires_approval_reason="x")]}}],
                       clock=clock)
    up = orch.upload("T1", documents.render(C01_DOC), "application/pdf", UPLOADER)
    orch.process("T1", up.run_id)
    assert orch.stale() == []
    clock.advance(16 * 60)
    stale = orch.stale()
    assert [s["status"] for s in stale] == ["pending_approval"]
    orch.approve("T1", stale[0]["audit_id"], APPROVER, "approve")
    assert orch.stale() == []


def test_approving_a_proposal_that_no_longer_fits_the_records_fails_cleanly(store):
    """Found in review: an approved line naming a PO line that doesn't exist used to raise from approve()."""
    post = c01_post(params={"lines": [
        {"source_line": 1, "po_line_no": 9, "account": "6100", "amount_cents": 65000},
        {"source_line": 2, "po_line_no": 2, "account": "6100", "amount_cents": 19250}]})
    orch, res, _, _ = run_doc(store, C01_DOC, [post])
    assert res.outcome == "PENDING_APPROVAL"
    aid = res.audit_ids[0]
    assert orch.approve("T1", aid, APPROVER, "approve").status == "failed"
    audit = store.get_audit("T1", aid)
    assert audit["status"] == "failed" and audit["error"].startswith("plan_failed")
    assert len(store.list_payables("T1", "V-101")) == 1


def test_resume_finishes_an_approved_write_whose_worker_died(store):
    """Found in review: a crash between approval and apply left the run at PENDING_APPROVAL for good."""
    from gwp.executor import SimulatedCrash

    orch, res, _, _ = run_doc(store, C01_DOC, [c01_post(requires_approval_reason="x")])
    aid = res.audit_ids[0]
    real_apply = orch.executor.apply
    orch.executor.apply = lambda t, a: (_ for _ in ()).throw(SimulatedCrash(a))
    with pytest.raises(SimulatedCrash):
        orch.approve("T1", aid, APPROVER, "approve")
    orch.executor.apply = real_apply
    assert store.get_audit("T1", aid)["status"] == "approved"
    assert orch.resume("T1", res.run_id).outcome == "APPLIED"
    assert orch.resume("T1", res.run_id).outcome == "APPLIED"
    assert len(store.list_payables("T1", "V-101")) == 2


# -- exceptions after a commit, resume, and claiming a run (audit findings 4, 9, 14 and 19) -----------------


def _post_and_hold(store, invoice_number):
    doc = {**C01_DOC, "invoice_number": invoice_number}
    orch, rm, pm = build(store, [{"tool": "Extraction", "input": documents.faithful_extraction(doc)}], [])
    up = orch.upload("T1", documents.render(doc), "application/pdf", UPLOADER)
    pm.turns.append({"tool": "propose_write", "input": {"proposals": [
        c01_post(params={"invoice_number": invoice_number}),
        {"action": "hold_invoice", "params": {"document_id": up.document_id, "reason_code": "check"},
         "rationale": "h", "confidence": 0.9}]}})
    return orch, up


def test_an_exception_after_a_commit_is_reported_and_resume_finishes_the_set(store):
    orch, up = _post_and_hold(store, "INV-EXC")
    real_apply = orch.executor.apply
    calls = []

    def flaky(t, aid):
        calls.append(aid)
        if len(calls) == 2:
            raise RuntimeError("dynamo hiccup on the second apply")
        return real_apply(t, aid)

    orch.executor.apply = flaky
    res = orch.process("T1", up.run_id)
    assert (res.outcome, res.reason) == ("NEEDS_HUMAN", "internal_error")
    run = store.get_run("T1", up.run_id)
    first, second = sorted(a["audit_id"] for a in store.list_audits("T1", up.run_id))
    assert run["applied_audit_ids"] == [first]  # the run says a write exists
    assert any(p["invoice_number"] == "INV-EXC" for p in store.list_payables("T1", "V-101"))
    orch.executor.apply = real_apply
    assert orch.resume("T1", up.run_id).outcome == "APPLIED"
    assert store.get_audit("T1", second)["status"] == "applied"
    assert store.get_run("T1", up.run_id)["applied_audit_ids"] == [first, second]
    assert orch.resume("T1", up.run_id).outcome == "APPLIED"


def test_resume_moves_an_approval_record_that_died_before_pending_approval(store):
    doc = {**C01_DOC, "invoice_number": "INV-STK"}
    orch, _, _ = build(store, [{"tool": "Extraction", "input": documents.faithful_extraction(doc)}],
                       [{"tool": "propose_write", "input": {"proposals": [
                           c01_post(requires_approval_reason="x", params={"invoice_number": "INV-STK"})]}}])
    up = orch.upload("T1", documents.render(doc), "application/pdf", UPLOADER)

    class Die(BaseException):
        pass

    real = store.transition_audit
    store.transition_audit = lambda *a, **k: (_ for _ in ()).throw(Die())
    with pytest.raises(Die):
        orch.process("T1", up.run_id)
    store.transition_audit = real
    assert orch.resume("T1", up.run_id).outcome == "PENDING_APPROVAL"
    (audit,) = store.list_audits("T1", up.run_id)
    assert audit["status"] == "pending_approval"
    assert orch.approve("T1", audit["audit_id"], APPROVER, "approve").status == "applied"


def test_resume_fails_a_set_that_was_never_fully_recorded(store):
    orch, up = _post_and_hold(store, "INV-HALF")

    class Die(BaseException):
        pass

    real = store.put_audit
    seen = []

    def die_on_second(audit):
        seen.append(audit)
        if len(seen) == 2:
            raise Die()
        real(audit)

    store.put_audit = die_on_second
    with pytest.raises(Die):
        orch.process("T1", up.run_id)
    store.put_audit = real
    res = orch.resume("T1", up.run_id)
    assert (res.outcome, res.reason) == ("NEEDS_HUMAN", "interrupted")
    assert [a["status"] for a in store.list_audits("T1", up.run_id)] == ["failed"]
    assert not any(p["invoice_number"] == "INV-HALF" for p in store.list_payables("T1", "V-101"))
    assert [t["reason_code"] for t in store.list_human_tasks("T1")] == ["interrupted"]


def test_a_second_worker_with_a_stale_view_cannot_claim_the_run(store):
    ext = documents.faithful_extraction(C01_DOC)
    orch, rm, pm = build(store, [{"tool": "Extraction", "input": ext}],
                         [{"tool": "propose_write", "input": {"proposals": [c01_post()]}}])
    up = orch.upload("T1", documents.render(C01_DOC), "application/pdf", UPLOADER)
    stale = store.get_run("T1", up.run_id)  # read while the run was still "received"
    assert orch.process("T1", up.run_id).outcome == "APPLIED"
    real = store.get_run
    store.get_run = lambda t, r: stale
    try:
        again = orch.process("T1", up.run_id)
    finally:
        store.get_run = real
    assert again.outcome == "IN_PROGRESS"
    assert rm.calls == 1 and pm.calls == 1  # the second worker called no model
    assert len(store.list_audits("T1", up.run_id)) == 1


def test_process_on_a_run_another_worker_holds_says_in_progress(store):
    orch, _, _ = build(store, [], [])
    up = orch.upload("T1", documents.render(C01_DOC), "application/pdf", UPLOADER)
    store.update_run("T1", up.run_id, {"state": "text_extracted"})
    assert orch.process("T1", up.run_id).outcome == "IN_PROGRESS"


def test_none_values_survive_a_round_trip_through_the_store(store):
    """Audit finding 15: the store used to drop None inside dicts, which caused the first C02 bug."""
    contract = store.get_contract("T1", "C-103")
    assert "effective_to" in contract["price_schedule"][0] and contract["price_schedule"][0]["effective_to"] is None
    store.seed([{"pk": "TENANT#T1", "sk": "X#1", "kind": "x", "nested": {"a": None, "b": 1}}])
    assert store._get("records", "TENANT#T1", "X#1")["nested"] == {"a": None, "b": 1}


def test_a_plan_that_no_longer_fits_fails_with_a_named_error_not_an_assert(store):
    """Audit finding 16: the plan builders used asserts, which vanish under python -O."""
    audit = _hand_built_audit(store, "A-P1", "auto", "proposed")
    store.put_audit({**audit, "audit_id": "A-P2", "proposal_id": "R-HAND:P2", "action": "apply_credit_memo",
                     "params": {"credit_number": "CM-9", "payable_id": "P-404", "amount_cents": 100}})
    res = Executor(store, FakeClock(), Ids()).apply("T1", "A-P2")
    assert res.status == "failed" and res.error.startswith("plan_failed:PlanError")
    # A revert whose original ledger entry is missing is refused and recorded, not raised.
    store.put_audit({**audit, "audit_id": "A-P3", "proposal_id": "R-HAND:P3", "status": "applied",
                     "write_ids": ["W-9"], "apply_record": {"payable_id": "P-2", "entry_id": "E-404"}})
    out = Executor(store, FakeClock(), Ids()).revert("T1", "A-P3", "user:ap")
    assert out.status == "refused" and out.refusal_reason == "plan_failed"
    assert store.get_payable("T1", "P-2")["status"] == "open"


def test_resume_opens_an_interrupted_task_even_when_the_run_already_has_another_task(store):
    """A bank change follow-up task exists before the set is marked recorded; a person must still see the stop."""
    doc = {**C01_DOC, "invoice_number": "INV-BANK", "vendor_requests": ["bank_details_change"]}
    ext = documents.faithful_extraction(doc)
    orch, _, _ = build(store, [{"tool": "Extraction", "input": ext}],
                       [{"tool": "propose_write", "input": {"proposals": [
                           c01_post(params={"invoice_number": "INV-BANK"})]}}])
    up = orch.upload("T1", documents.render(doc), "application/pdf", UPLOADER)

    class Die(BaseException):
        pass

    real = store.update_run
    store.update_run = lambda t, r, f, *a, **k: (_ for _ in ()).throw(Die()) if f.get("state") == "audited" \
        else real(t, r, f, *a, **k)
    with pytest.raises(Die):
        orch.process("T1", up.run_id)
    store.update_run = real
    assert orch.resume("T1", up.run_id).reason == "interrupted"
    assert sorted(t["reason_code"] for t in store.list_human_tasks("T1")) == [
        "interrupted", "vendor_requested_bank_change"]
    assert not any(p["invoice_number"] == "INV-BANK" for p in store.list_payables("T1", "V-101"))


# -- a worker that dies after claiming a run (Codex review finding 2) ----------------------------------------


class _Die(BaseException):
    """Stands in for the Lambda being killed, e.g. at its timeout. Not an Exception, so nothing catches it."""


def _die_in_reader(store, invoice_number="INV-6001", clock=None):
    """Upload C01 and run a worker that dies inside the reader call, after it claimed the run."""
    doc = {**C01_DOC, "invoice_number": invoice_number}
    clock = clock or FakeClock()
    orch, rm, pm = build(store, [{"tool": "Extraction", "input": documents.faithful_extraction(doc)}],
                         [{"tool": "propose_write", "input": {"proposals": [
                             c01_post(params={"invoice_number": invoice_number})]}}], clock=clock)
    up = orch.upload("T1", documents.render(doc), "application/pdf", UPLOADER)
    real = orch.reader.read
    orch.reader.read = lambda *a, **k: (_ for _ in ()).throw(_Die())
    with pytest.raises(_Die):
        orch.process("T1", up.run_id)
    orch.reader.read = real
    return orch, up, clock


def test_a_worker_that_dies_after_the_claim_is_found_and_finished_by_the_sweep(store):
    from gwp.orchestrator import RUN_LEASE_SECONDS

    orch, up, clock = _die_in_reader(store)
    assert store.get_run("T1", up.run_id)["state"] == "text_extracted"
    # The async retry arrives while the lease still holds: it must not start a second worker.
    assert orch.process("T1", up.run_id).outcome == "IN_PROGRESS"
    assert orch.recover_stranded() == []
    clock.advance(RUN_LEASE_SECONDS + 1)
    (found,) = orch.recover_stranded()
    assert (found["run_id"], found["state"], found["outcome"]) == (up.run_id, "text_extracted", "NEEDS_HUMAN")
    run = store.get_run("T1", up.run_id)
    assert (run["state"], run["outcome"], run["reason"]) == ("finalized", "NEEDS_HUMAN", "interrupted")
    assert "lease_until" not in run and "lease_flag" not in run
    assert [t["reason_code"] for t in store.list_human_tasks("T1")] == ["interrupted"]
    assert len(store.list_payables("T1", "V-101")) == 1  # only the seeded P-2
    assert orch.recover_stranded() == []  # a finalized run leaves the index


def test_a_redelivered_event_after_the_lease_expires_finishes_the_run(store):
    from gwp.orchestrator import RUN_LEASE_SECONDS

    orch, up, clock = _die_in_reader(store)
    clock.advance(RUN_LEASE_SECONDS + 1)
    res = orch.process("T1", up.run_id)
    assert (res.outcome, res.reason) == ("NEEDS_HUMAN", "interrupted")
    assert orch.process("T1", up.run_id).outcome == "NEEDS_HUMAN"


def test_the_sweep_finishes_a_run_whose_worker_died_after_the_audit_set(store):
    """The worker dies before moving an approval record to pending_approval; the sweep finishes the set."""
    from gwp.orchestrator import RUN_LEASE_SECONDS

    clock = FakeClock()
    doc = {**C01_DOC, "invoice_number": "INV-LATE"}
    orch, _, _ = build(store, [{"tool": "Extraction", "input": documents.faithful_extraction(doc)}],
                       [{"tool": "propose_write", "input": {"proposals": [
                           c01_post(requires_approval_reason="x", params={"invoice_number": "INV-LATE"})]}}],
                       clock=clock)
    up = orch.upload("T1", documents.render(doc), "application/pdf", UPLOADER)
    real = store.transition_audit
    store.transition_audit = lambda *a, **k: (_ for _ in ()).throw(_Die())
    with pytest.raises(_Die):
        orch.process("T1", up.run_id)
    store.transition_audit = real
    clock.advance(RUN_LEASE_SECONDS + 1)
    (found,) = orch.recover_stranded()
    assert found["outcome"] == "PENDING_APPROVAL"
    assert [a["status"] for a in store.list_audits("T1", up.run_id)] == ["pending_approval"]


def test_a_run_whose_processing_event_never_arrived_is_found_by_the_sweep(store):
    from gwp.orchestrator import DISPATCH_LEASE_SECONDS

    clock = FakeClock()
    orch, rm, _ = build(store, [], [], clock=clock)
    up = orch.upload("T1", documents.render(C01_DOC), "application/pdf", UPLOADER)
    assert orch.recover_stranded() == []
    clock.advance(DISPATCH_LEASE_SECONDS + 1)
    (found,) = orch.recover_stranded()
    assert (found["state"], found["outcome"]) == ("received", "NEEDS_HUMAN")
    assert rm.calls == 0
    # If the event does arrive late, it finds the run finished and calls no model.
    assert orch.process("T1", up.run_id).outcome == "NEEDS_HUMAN" and rm.calls == 0


# -- two sweeps over the same run (Codex round 2) ------------------------------------------------------------


def _two_sweeps(orch, store):
    """Run two sweeps at once over the same expired run, in the order the round 2 review describes.

    Both sweeps read the run's audit records before either changes them, and both decide to open a task before
    either has opened it. Two barriers pin that order: one after each sweep's first read of the audit records, one
    at the task id, which the orchestrator takes right before it opens a task.
    """
    import threading

    after_read, before_open = threading.Barrier(2, timeout=10), threading.Barrier(2, timeout=10)
    local = threading.local()
    real_list, real_new = store.list_audits, orch.ids.new

    def list_audits(*a, **k):
        out = real_list(*a, **k)
        if not getattr(local, "read", False):
            local.read = True
            after_read.wait()
        return out

    def new(prefix):
        if prefix == "H" and not getattr(local, "opening", False):
            local.opening = True
            before_open.wait()
        return real_new(prefix)

    results, errors = [None, None], []

    def sweep(i):
        try:
            results[i] = orch.recover_stranded()
        except BaseException as e:  # noqa: BLE001 - reported below
            errors.append(e)

    store.list_audits, orch.ids.new = list_audits, new
    threads = [threading.Thread(target=sweep, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    del store.list_audits, orch.ids.new
    assert not errors, errors
    assert [len(r) for r in results] == [1, 1], "both sweeps must have resumed the same run"
    return results[0] + results[1]


def test_two_sweeps_over_a_run_with_an_incomplete_audit_set_open_one_task(store):
    from gwp.orchestrator import RUN_LEASE_SECONDS

    clock = FakeClock()
    doc = {**C01_DOC, "invoice_number": "INV-TWICE"}
    orch, _, _ = build(store, [{"tool": "Extraction", "input": documents.faithful_extraction(doc)}],
                       [{"tool": "propose_write", "input": {"proposals": [
                           c01_post(params={"invoice_number": "INV-TWICE"})]}}], clock=clock)
    up = orch.upload("T1", documents.render(doc), "application/pdf", UPLOADER)
    real = store.update_run
    store.update_run = lambda t, r, f, *a, **k: (_ for _ in ()).throw(_Die()) if f.get("state") == "audited" \
        else real(t, r, f, *a, **k)
    with pytest.raises(_Die):
        orch.process("T1", up.run_id)
    store.update_run = real
    clock.advance(RUN_LEASE_SECONDS + 1)
    found = _two_sweeps(orch, store)
    assert [f["outcome"] for f in found] == ["NEEDS_HUMAN", "NEEDS_HUMAN"]
    assert [t["reason_code"] for t in store.list_human_tasks("T1")] == ["interrupted"]
    assert [a["status"] for a in store.list_audits("T1", up.run_id)] == ["failed"]


def _forbidden_run_that_dies(store, clock, die_on):
    orch, _, _ = build(store, [{"tool": "Extraction", "input": documents.faithful_extraction(C01_DOC)}],
                       [{"tool": "propose_write", "input": {"proposals": [
                           c01_post(), {"action": "schedule_payment", "params": {"payable_id": "x"}}]}}], clock=clock)
    up = orch.upload("T1", documents.render(C01_DOC), "application/pdf", UPLOADER)
    die_on(orch)
    with pytest.raises(_Die):
        orch.process("T1", up.run_id)
    return orch, up


def test_two_sweeps_over_a_routed_run_whose_worker_died_before_its_task_open_one_task(store):
    from gwp.orchestrator import RUN_LEASE_SECONDS

    clock = FakeClock()
    real = store.transition_audit

    def die_on(orch):
        store.transition_audit = lambda *a, **k: (_ for _ in ()).throw(_Die())

    orch, up = _forbidden_run_that_dies(store, clock, die_on)
    store.transition_audit = real
    clock.advance(RUN_LEASE_SECONDS + 1)
    found = _two_sweeps(orch, store)
    assert [f["outcome"] for f in found] == ["ROUTED_TO_HUMAN", "ROUTED_TO_HUMAN"]
    assert [t["reason_code"] for t in store.list_human_tasks("T1")] == ["forbidden_action"]
    assert store.list_payables("T1", "V-101")[0]["payable_id"] == "P-2"


def test_a_routed_run_whose_worker_died_just_before_its_task_still_gets_one(store):
    """The records were already closed as routed, so resume found nothing at `proposed` and opened no task."""
    from gwp.orchestrator import RUN_LEASE_SECONDS

    clock = FakeClock()

    def die_on(orch):
        real = orch.ids.new
        orch.ids.new = lambda p: (_ for _ in ()).throw(_Die()) if p == "H" else real(p)

    orch, up = _forbidden_run_that_dies(store, clock, die_on)
    del orch.ids.new  # back to the class method
    assert sorted(a["status"] for a in store.list_audits("T1", up.run_id)) == ["rejected", "routed"]
    assert store.list_human_tasks("T1") == []
    clock.advance(RUN_LEASE_SECONDS + 1)
    (found,) = orch.recover_stranded()
    assert found["outcome"] == "ROUTED_TO_HUMAN"
    assert [t["reason_code"] for t in store.list_human_tasks("T1")] == ["forbidden_action"]
    assert orch.resume("T1", up.run_id).outcome == "ROUTED_TO_HUMAN"
    assert len(store.list_human_tasks("T1")) == 1


def test_opening_the_same_task_twice_keeps_one(store):
    task = {"tenant_id": "T1", "task_id": "H-1", "run_id": "R-1", "reason_code": "interrupted", "status": "open",
            "created_at": "2026-09-25T00:00:00Z"}
    assert store.open_human_task(task) is True
    assert store.open_human_task({**task, "task_id": "H-2"}) is False
    assert store.open_human_task({**task, "task_id": "H-3", "reason_code": "unknown_vendor"}) is True
    assert sorted((t["task_id"], t["reason_code"]) for t in store.list_human_tasks("T1")) == [
        ("H-1", "interrupted"), ("H-3", "unknown_vendor")]
