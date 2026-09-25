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


def test_revert_refused_while_a_later_write_depends_on_it(store):
    orch, _, _ = build(store, [], [])
    assert orch.revert("T1", "A-2", APPROVER).outcome == "REVERTED"  # nothing depends on P-2 yet


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
