"""Regression tests for the second Fable audit (9/28/26). Each test failed on 8356cdb before its fix."""

import pytest
from helpers import APPROVER, C01_DOC, UPLOADER, build, c01_post, run_doc

from gwp.evals import documents
from gwp.executor import SimulatedCrash

# -- GWP2-1: nothing resumed a finalized run, so a write could stay unapplied for good ----------------------------


def _crash_between_approved_and_apply(orch, aid):
    real_apply = orch.executor.apply
    orch.executor.apply = lambda t, a: (_ for _ in ()).throw(SimulatedCrash(a))
    with pytest.raises(SimulatedCrash):
        orch.approve("T1", aid, APPROVER, "approve")
    orch.executor.apply = real_apply


def test_the_approvers_retry_applies_a_write_whose_worker_died_before_the_apply(store):
    orch, res, _, _ = run_doc(store, C01_DOC, [c01_post(requires_approval_reason="x")])
    aid = res.audit_ids[0]
    _crash_between_approved_and_apply(orch, aid)
    assert store.get_audit("T1", aid)["status"] == "approved"
    assert orch.recover_stranded() == []  # a run waiting for approval has no lease, so the sweep never sees it
    retry = orch.approve("T1", aid, APPROVER, "approve")
    assert (retry.status, retry.run_outcome, retry.detail) == ("applied", "APPLIED", "applied_on_retry")
    assert store.get_audit("T1", aid)["status"] == "applied"
    assert store.get_run("T1", res.run_id)["outcome"] == "APPLIED"
    assert len(store.list_payables("T1", "V-101")) == 2
    again = orch.approve("T1", aid, APPROVER, "approve")
    assert (again.status, again.run_outcome) == ("already_decided", "APPLIED")
    assert len(store.list_payables("T1", "V-101")) == 2


def test_the_staleness_pass_resumes_an_approved_write_whose_worker_died_before_the_apply(store):
    orch, res, _, _ = run_doc(store, C01_DOC, [c01_post(requires_approval_reason="x")])
    aid = res.audit_ids[0]
    _crash_between_approved_and_apply(orch, aid)
    assert orch.resume_stale() == []  # not stale yet
    orch.clock.advance(16 * 60)
    resumed = orch.resume_stale()
    assert [(r["run_id"], r["outcome"]) for r in resumed] == [(res.run_id, "APPLIED")]
    assert store.get_audit("T1", aid)["status"] == "applied"
    assert len(store.list_payables("T1", "V-101")) == 2
    assert orch.stale() == [] and orch.resume_stale() == []


def test_the_staleness_pass_finishes_an_auto_set_left_at_proposed_on_a_finalized_run(store):
    doc = {**C01_DOC, "invoice_number": "INV-EXC"}
    orch, rm, pm = build(store, [{"tool": "Extraction", "input": documents.faithful_extraction(doc)}], [])
    up = orch.upload("T1", documents.render(doc), "application/pdf", UPLOADER)
    pm.turns.append({"tool": "propose_write", "input": {"proposals": [
        c01_post(params={"invoice_number": "INV-EXC"}),
        {"action": "hold_invoice", "params": {"document_id": up.document_id, "reason_code": "check"},
         "rationale": "h", "confidence": 0.9}]}})
    real_apply = orch.executor.apply
    calls = []

    def flaky(t, a):
        calls.append(a)
        if len(calls) == 2:
            raise RuntimeError("dynamo hiccup on the second apply")
        return real_apply(t, a)

    orch.executor.apply = flaky
    assert orch.process("T1", up.run_id).outcome == "NEEDS_HUMAN"
    orch.executor.apply = real_apply
    run = store.get_run("T1", up.run_id)
    assert run["state"] == "finalized" and run.get("lease_until") is None
    assert orch.recover_stranded() == []
    hold = next(a for a in store.list_audits("T1", up.run_id) if a["action"] == "hold_invoice")
    assert (hold["tier"], hold["status"]) == ("auto", "proposed")
    orch.clock.advance(16 * 60)
    resumed = orch.resume_stale()
    assert [(r["run_id"], r["outcome"]) for r in resumed] == [(up.run_id, "APPLIED")]
    assert store.get_audit("T1", hold["audit_id"])["status"] == "applied"
    assert store.get_run("T1", up.run_id)["outcome"] == "APPLIED"
    assert orch.stale() == []


def test_the_staleness_pass_closes_a_proposed_record_on_a_finalized_run_whose_set_is_incomplete(store):
    """The MCP audit's F6. A worker renewed its lease at step 9 and put its first record, then stalled past the
    lease. The sweep failed that record and finalized the run. The worker came back, put its second record, lost the
    conditional move to `audited`, and died while closing its records, so the second record stayed at `proposed` on
    a finalized run with an incomplete set. Nothing ever closed it."""
    doc = {**C01_DOC, "invoice_number": "INV-LATE"}
    orch, rm, pm = build(store, [{"tool": "Extraction", "input": documents.faithful_extraction(doc)}], [])
    up = orch.upload("T1", documents.render(doc), "application/pdf", UPLOADER)
    pm.turns.append({"tool": "propose_write", "input": {"proposals": [
        c01_post(params={"invoice_number": "INV-LATE"}),
        {"action": "hold_invoice", "params": {"document_id": up.document_id, "reason_code": "check"},
         "rationale": "h", "confidence": 0.9}]}})

    class Die(BaseException):
        pass

    real_put, real_transition = store.put_audit, store.transition_audit
    puts = []

    def stall_before_second(audit):
        puts.append(audit)
        if len(puts) == 2:
            orch.clock.advance(301)  # past the lease step 9 renewed
            assert [r["run_id"] for r in orch.recover_stranded()] == [up.run_id]
            store.transition_audit = lambda *a, **k: (_ for _ in ()).throw(Die())  # dies while closing
        real_put(audit)

    store.put_audit = stall_before_second
    with pytest.raises(Die):
        orch.process("T1", up.run_id)
    store.put_audit, store.transition_audit = real_put, real_transition
    run = store.get_run("T1", up.run_id)
    assert run["state"] == "finalized" and not run.get("audit_set_complete")
    statuses = sorted((a["action"], a["status"]) for a in store.list_audits("T1", up.run_id))
    assert statuses == [("hold_invoice", "proposed"), ("post_payable", "failed")]
    orch.clock.advance(16 * 60)
    assert [s["status"] for s in orch.stale()] == ["proposed"]
    resumed = orch.resume_stale()
    assert [(r["run_id"], r["outcome"]) for r in resumed] == [(up.run_id, "NEEDS_HUMAN")]
    assert {a["status"] for a in store.list_audits("T1", up.run_id)} == {"failed"}
    assert orch.stale() == []
    assert not any(p["invoice_number"] == "INV-LATE" for p in store.list_payables("T1", "V-101"))


def test_the_staleness_pass_leaves_a_record_waiting_for_approval_alone(store):
    orch, res, _, _ = run_doc(store, C01_DOC, [c01_post(requires_approval_reason="x")])
    orch.clock.advance(16 * 60)
    assert [s["status"] for s in orch.stale()] == ["pending_approval"]
    assert orch.resume_stale() == []
    assert store.get_audit("T1", res.audit_ids[0])["status"] == "pending_approval"
    assert store.get_run("T1", res.run_id)["outcome"] == "PENDING_APPROVAL"


def test_the_scheduled_staleness_event_resumes_the_run_of_a_stale_approved_record(store):
    from gwp import api

    orch, res, _, _ = run_doc(store, C01_DOC, [c01_post(requires_approval_reason="x")])
    aid = res.audit_ids[0]
    _crash_between_approved_and_apply(orch, aid)
    orch.clock.advance(16 * 60)
    api.set_orchestrator_factory(lambda: orch)
    try:
        api.handler({"source": "gwp.staleness"})
    finally:
        api.set_orchestrator_factory(None)
    assert store.get_audit("T1", aid)["status"] == "applied"
    assert store.get_run("T1", res.run_id)["outcome"] == "APPLIED"


def test_the_scheduled_staleness_event_fails_when_a_stale_run_cannot_be_resumed(store, monkeypatch):
    from gwp import api

    orch, res, _, _ = run_doc(store, C01_DOC, [c01_post(requires_approval_reason="x")])
    _crash_between_approved_and_apply(orch, res.audit_ids[0])
    orch.clock.advance(16 * 60)
    monkeypatch.setattr(orch, "resume", lambda t, r: (_ for _ in ()).throw(RuntimeError("can't resume")))
    api.set_orchestrator_factory(lambda: orch)
    try:
        with pytest.raises(RuntimeError, match=f"1 run\\(s\\) could not be resumed: T1/{res.run_id}"):
            api.handler({"source": "gwp.staleness"})
    finally:
        api.set_orchestrator_factory(None)


# -- GWP2-2: an approver's call on a routed or rejected record rewrote the run's outcome -----------------------------


def test_an_approval_call_on_a_routed_record_leaves_the_run_routed(store):
    orch, res, _, _ = run_doc(store, C01_DOC, [
        c01_post(), {"action": "schedule_payment", "params": {}, "rationale": "x", "confidence": 0.9}])
    assert (res.outcome, res.reason) == ("ROUTED_TO_HUMAN", "forbidden_action")
    before = store.get_run("T1", res.run_id)
    by_action = {a["action"]: a for a in store.list_audits("T1", res.run_id)}
    assert by_action["post_payable"]["status"] == "routed" and by_action["schedule_payment"]["status"] == "rejected"
    for action in ("post_payable", "schedule_payment"):
        d = orch.approve("T1", by_action[action]["audit_id"], APPROVER, "approve")
        assert (d.status, d.run_outcome) == ("already_decided", "ROUTED_TO_HUMAN")
    after = store.get_run("T1", res.run_id)
    assert (after["outcome"], after["reason"]) == ("ROUTED_TO_HUMAN", "forbidden_action")
    assert after["history"] == before["history"]
    assert len(store.list_payables("T1", "V-101")) == 1


# -- GWP2-3: an approved payable whose lines don't sum to its total posted an unbalanced entry ------------------------


def test_an_approved_payable_whose_lines_do_not_sum_to_its_total_fails_instead_of_posting(store):
    orch, res, _, _ = run_doc(store, C01_DOC, [c01_post(params={"total_cents": 90000})])
    assert (res.outcome, res.reason) == ("PENDING_APPROVAL", "proposal_extraction_mismatch")
    aid = res.audit_ids[0]
    ledger_before = store.list_ledger("T1")
    assert orch.approve("T1", aid, APPROVER, "approve").status == "failed"
    audit = store.get_audit("T1", aid)
    assert audit["status"] == "failed" and audit["error"].startswith("plan_failed:PlanError")
    assert len(store.list_payables("T1", "V-101")) == 1
    assert store.list_ledger("T1") == ledger_before
    assert store.get_run("T1", res.run_id)["outcome"] == "NEEDS_HUMAN"


# -- GWP2-4: a recode had no vendor or document check ----------------------------------------------------------------

P9 = {
    "payables": [{"tenant_id": "T1", "payable_id": "P-9", "vendor_id": "V-102", "invoice_number": "KOF-2210",
                  "invoice_date": "2026-08-05", "po_id": "PO-7009", "status": "open", "total_cents": 84000,
                  "entry_id": "E-9", "created_by_write_id": "W-9",
                  "lines": [{"line_no": 1, "kind": "item", "account": "1500", "amount_cents": 84000,
                             "po_line_no": 1, "qty": 4, "description_untrusted": "Filing cabinet, 4 drawer"}]}],
    "ledger": [{"tenant_id": "T1", "entry_id": "E-9", "payable_id": "P-9", "write_id": "W-9",
                "lines": [{"account": "1500", "debit_cents": 84000, "credit_cents": 0},
                          {"account": "2000", "debit_cents": 0, "credit_cents": 84000}]}],
    "receipts": {"PO-7009#1": {"qty_invoiced": 4}},
}


@pytest.fixture
def p9_store():
    import boto3
    from moto import mock_aws

    from gwp.store import DynamoStore
    from gwp.world import seed_world

    with mock_aws():
        s = DynamoStore(boto3.client("dynamodb", region_name="us-east-1"))
        s.create_tables()
        seed_world(s, P9)
        yield s


def _recode_p9(store, doc, account):
    return run_doc(store, doc, [{"action": "recode_line", "params": {"payable_id": "P-9", "line_no": 1,
                                                                      "account": account},
                                 "rationale": "as asked", "confidence": 0.9}])


def test_a_recode_of_another_vendors_payable_goes_to_a_person(p9_store):
    orch, res, _, _ = _recode_p9(p9_store, C01_DOC, "6150")  # a Pine Street invoice naming Kestrel's P-9
    assert (res.outcome, res.reason) == ("ROUTED_TO_HUMAN", "vendor_mismatch")
    (audit,) = p9_store.list_audits("T1", res.run_id)
    assert (audit["tier"], audit["status"]) == ("human", "routed")
    assert audit["checks"]["vendor_resolved"] == "fail"
    assert p9_store.get_payable("T1", "P-9")["lines"][0]["account"] == "1500"


def test_a_recode_proposed_from_a_credit_memo_goes_to_a_person(p9_store):
    memo = {"kind": "credit_memo", "vendor_name": "Kestrel Office Furniture", "vendor_tax_id": "84-3310442",
            "invoice_number": "KCM-4", "invoice_date": "2026-09-18", "referenced_invoice_numbers": ["KOF-2210"],
            "lines": [{"description": "Filing cabinet, 4 drawer (returned)", "qty": 1, "unit_price_cents": 21000}]}
    orch, res, _, _ = _recode_p9(p9_store, memo, "6150")
    assert (res.outcome, res.reason) == ("ROUTED_TO_HUMAN", "document_kind_mismatch")
    assert p9_store.get_payable("T1", "P-9")["lines"][0]["account"] == "1500"


def test_a_recode_from_the_payables_own_vendor_on_a_letter_stays_auto(p9_store):
    """Cases R05, R06 and R12 recode from a Kestrel letter at the auto tier, by design (entry 50)."""
    letter = {"kind": "letter", "vendor_name": "Kestrel Office Furniture", "vendor_tax_id": "84-3310442",
              "letter_date": "2026-09-18", "body": ["Re: invoice KOF-2210. A note on coding."]}
    orch, res, _, _ = _recode_p9(p9_store, letter, "6150")
    assert res.outcome == "APPLIED"
    (audit,) = p9_store.list_audits("T1", res.run_id)
    assert audit["tier"] == "auto" and audit["checks"]["vendor_resolved"] == "pass"


# -- GWP2-5: a transaction cancelled for a reason other than a failed condition marked the write failed for good -----


def _cancel_once(store, code):
    from botocore.exceptions import ClientError

    real = store.client.transact_write_items
    once = []

    def cancel(**kw):
        if not once:
            once.append(1)
            n = len(kw["TransactItems"])
            raise ClientError({"Error": {"Code": "TransactionCanceledException", "Message": "Transaction cancelled"},
                               "CancellationReasons": [{"Code": code}] + [{"Code": "None"}] * (n - 1)},
                              "TransactWriteItems")
        return real(**kw)

    store.client.transact_write_items = cancel
    return lambda: setattr(store.client, "transact_write_items", real)


@pytest.mark.parametrize("code", ["TransactionConflict", "ThrottlingError"])
def test_an_approval_whose_transaction_is_cancelled_by_contention_stays_approved_for_the_retry(store, code):
    orch, res, _, _ = run_doc(store, C01_DOC, [c01_post(requires_approval_reason="x")])
    aid = res.audit_ids[0]
    restore = _cancel_once(store, code)
    d = orch.approve("T1", aid, APPROVER, "approve")
    restore()
    assert (d.status, d.run_outcome) == ("retryable", "PENDING_APPROVAL")
    audit = store.get_audit("T1", aid)
    assert audit["status"] == "approved" and audit.get("error") is None
    assert audit["cancellation_reasons"][0] == code
    assert audit["history"][-1]["state"] == f"apply_cancelled:{code}"
    assert len(store.list_payables("T1", "V-101")) == 1
    retry = orch.approve("T1", aid, APPROVER, "approve")
    assert (retry.status, retry.run_outcome) == ("applied", "APPLIED")
    assert store.get_audit("T1", aid)["status"] == "applied"
    assert len(store.list_payables("T1", "V-101")) == 2
    assert orch.upload("T1", documents.render(C01_DOC), "application/pdf", UPLOADER).duplicate


def test_an_auto_write_whose_transaction_is_cancelled_by_contention_is_finished_by_the_staleness_pass(store):
    ext = documents.faithful_extraction(C01_DOC)
    orch, _, _ = build(store, [{"tool": "Extraction", "input": ext}],
                       [{"tool": "propose_write", "input": {"proposals": [c01_post()]}}])
    up = orch.upload("T1", documents.render(C01_DOC), "application/pdf", UPLOADER)
    restore = _cancel_once(store, "TransactionConflict")
    res = orch.process("T1", up.run_id)
    restore()
    assert (res.outcome, res.reason) == ("NEEDS_HUMAN", "apply_failed")
    (audit,) = store.list_audits("T1", up.run_id)
    assert audit["status"] == "proposed"
    orch.clock.advance(16 * 60)
    assert [r["outcome"] for r in orch.resume_stale()] == ["APPLIED"]
    assert store.get_audit("T1", audit["audit_id"])["status"] == "applied"
    assert len(store.list_payables("T1", "V-101")) == 2


def test_a_cancellation_with_a_failed_condition_still_fails_the_write(store):
    orch, res, _, _ = run_doc(store, C01_DOC, [c01_post(requires_approval_reason="x")])
    aid = res.audit_ids[0]
    from botocore.exceptions import ClientError

    real = store.client.transact_write_items

    def cancel(**kw):
        n = len(kw["TransactItems"])
        raise ClientError({"Error": {"Code": "TransactionCanceledException", "Message": "Transaction cancelled"},
                           "CancellationReasons": [{"Code": "TransactionConflict"}, {"Code": "ConditionalCheckFailed"}]
                           + [{"Code": "None"}] * (n - 2)}, "TransactWriteItems")

    store.client.transact_write_items = cancel
    d = orch.approve("T1", aid, APPROVER, "approve")
    store.client.transact_write_items = real
    assert d.status == "failed"
    assert store.get_audit("T1", aid)["status"] == "failed"


def test_the_api_answers_503_for_an_approval_cancelled_by_contention_and_the_retry_applies_it(store, monkeypatch):
    import json

    from gwp import api
    from gwp.runtime import sha256_hex

    monkeypatch.setenv("GWP_API_KEYS", json.dumps(
        {sha256_hex("ap-key"): {"principal_id": "user:ap", "role": "approver", "tenant_id": "T1"}}))
    orch, res, _, _ = run_doc(store, C01_DOC, [c01_post(requires_approval_reason="x")])
    aid = res.audit_ids[0]
    event = {"requestContext": {"http": {"method": "POST"}}, "rawPath": f"/approvals/{aid}",
             "headers": {"x-api-key": "ap-key"}, "body": json.dumps({"decision": "approve"})}
    api.set_orchestrator_factory(lambda: orch)
    try:
        restore = _cancel_once(store, "TransactionConflict")
        first = api.handler(event)
        restore()
        second = api.handler(event)
    finally:
        api.set_orchestrator_factory(None)
    assert first["statusCode"] == 503 and json.loads(first["body"])["status"] == "retryable"
    assert second["statusCode"] == 200 and json.loads(second["body"])["run_outcome"] == "APPLIED"


# -- GWP2-7: approve on a record whose run record doesn't exist raised out of the API ----------------------------------


def test_approve_on_the_seeded_record_whose_run_record_does_not_exist_answers_already_decided(store):
    orch, _, _ = build(store, [], [])
    assert store.get_run("T1", "R-SEED") is None
    d = orch.approve("T1", "A-2", APPROVER, "approve")
    assert (d.status, d.run_outcome, d.detail) == ("already_decided", None, "applied")
    assert store.get_run("T1", "R-SEED") is None
    assert orch._refresh_run_outcome("T1", "R-SEED") is None


# -- GWP2-9: validation feedback echoed the ids the proposer wrote ---------------------------------------------------


def test_unknown_id_feedback_gives_kinds_and_counts_and_never_the_ids(store):
    bad = c01_post(params={"po_id": "PO-IGNORE-ALL-RULES", "vendor_id": "V-AUTO-APPROVE"})
    orch, res, _, _ = run_doc(store, C01_DOC, [bad])
    (attempt,) = store.get_run("T1", res.run_id)["proposal_attempts"]
    assert attempt["error"] == "unknown ids: 1 po, 1 vendor"
    assert "IGNORE" not in attempt["error"] and "AUTO" not in attempt["error"]
