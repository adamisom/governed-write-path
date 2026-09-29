"""Regression tests for the third Fable audit (9/29/26). Each test failed on da469fe before its fix."""

import pytest
from helpers import APPROVER, C01_DOC, UPLOADER, build, c01_post, run_doc

from gwp.evals import documents
from gwp.executor import SimulatedCrash
from gwp.orchestrator import RUN_LEASE_SECONDS

# -- GWP3-1: a decline on a record left at `approved` applied the write, and the retry that applied it said 409 --


def _crash_between_approved_and_apply(orch, aid):
    real_apply = orch.executor.apply
    orch.executor.apply = lambda t, a: (_ for _ in ()).throw(SimulatedCrash(a))
    with pytest.raises(SimulatedCrash):
        orch.approve("T1", aid, APPROVER, "approve")
    orch.executor.apply = real_apply


def test_a_decline_after_an_approval_crash_changes_nothing(store):
    orch, res, _, _ = run_doc(store, C01_DOC, [c01_post(requires_approval_reason="x")])
    aid = res.audit_ids[0]
    _crash_between_approved_and_apply(orch, aid)
    d = orch.approve("T1", aid, APPROVER, "decline")
    assert (d.status, d.detail) == ("already_decided", "approved")
    assert store.get_audit("T1", aid)["status"] == "approved"
    assert len(store.list_payables("T1", "V-101")) == 1


def test_the_retry_that_applies_an_approved_write_says_applied(store):
    orch, res, _, _ = run_doc(store, C01_DOC, [c01_post(requires_approval_reason="x")])
    aid = res.audit_ids[0]
    _crash_between_approved_and_apply(orch, aid)
    d = orch.approve("T1", aid, APPROVER, "approve")
    assert (d.status, d.run_outcome, d.detail) == ("applied", "APPLIED", "applied_on_retry")
    again = orch.approve("T1", aid, APPROVER, "approve")
    assert (again.status, again.detail) == ("already_decided", "applied")
    assert len(store.list_payables("T1", "V-101")) == 2


# -- GWP3-2: a cancelled transaction for the bank change task ended the run with no task --------------------------


def _bank_run(store):
    doc = {**C01_DOC, "invoice_number": "INV-BANK", "vendor_requests": ["bank_details_change"]}
    orch, _, _ = build(store, [{"tool": "Extraction", "input": documents.faithful_extraction(doc)}],
                       [{"tool": "propose_write", "input": {"proposals": [
                           c01_post(params={"invoice_number": "INV-BANK"})]}}])
    return orch, orch.upload("T1", documents.render(doc), "application/pdf", UPLOADER)


def test_a_bank_change_run_whose_transaction_is_cancelled_by_contention_still_posts_and_gets_its_task(store,
                                                                                                    monkeypatch):
    from botocore.exceptions import ClientError

    orch, up = _bank_run(store)
    real, calls = store.client.transact_write_items, []

    def cancel_first_task_transaction(**kw):
        calls.append(kw)
        if len(calls) == 1 and any("Put" in item for item in kw["TransactItems"]):
            raise ClientError({"Error": {"Code": "TransactionCanceledException", "Message": "cancelled"},
                               "CancellationReasons": [{"Code": "TransactionConflict"}, {"Code": "None"}]},
                              "TransactWriteItems")
        return real(**kw)

    monkeypatch.setattr(store.client, "transact_write_items", cancel_first_task_transaction)
    assert orch.process("T1", up.run_id).outcome == "APPLIED"
    assert [t["reason_code"] for t in store.list_human_tasks("T1")] == ["vendor_requested_bank_change"]


# -- GWP3-4: `resume` closed a set as incomplete while a worker that outlived its lease completed it -------------


def test_resume_finishes_a_set_that_a_late_worker_completed_after_resume_read_it(store, monkeypatch):
    doc = {**C01_DOC, "invoice_number": "INV-RACE"}
    orch, rm, pm = build(store, [{"tool": "Extraction", "input": documents.faithful_extraction(doc)}], [])
    up = orch.upload("T1", documents.render(doc), "application/pdf", UPLOADER)
    pm.turns.append({"tool": "propose_write", "input": {"proposals": [
        c01_post(params={"invoice_number": "INV-RACE"}),
        {"action": "hold_invoice", "params": {"document_id": up.document_id, "reason_code": "check"},
         "rationale": "h", "confidence": 0.9}]}})

    class Die(BaseException):
        pass

    real_update = store.update_run
    held = {}

    def stall_at_audited(tenant_id, run_id, fields, *a, **k):
        if fields.get("state") == "audited":
            held.update(fields=fields, expect_state=k.get("expect_state"))
            raise Die()
        return real_update(tenant_id, run_id, fields, *a, **k)

    monkeypatch.setattr(store, "update_run", stall_at_audited)
    with pytest.raises(Die):
        orch.process("T1", up.run_id)
    monkeypatch.setattr(store, "update_run", real_update)
    # The worker outlived its lease. Its move to `audited` lands after `resume` read the run and its records.
    orch.clock.advance(RUN_LEASE_SECONDS + 30)
    real_list, lists = store.list_audits, []

    def list_audits(tenant_id, run_id):
        out = real_list(tenant_id, run_id)
        lists.append(run_id)
        if len(lists) == 1:
            assert real_update(tenant_id, run_id, held["fields"], expect_state=held["expect_state"])
        return out

    monkeypatch.setattr(store, "list_audits", list_audits)
    rows = orch.recover_stranded()
    monkeypatch.undo()
    assert [r["outcome"] for r in rows] == ["APPLIED"]
    assert {a["action"]: a["status"] for a in store.list_audits("T1", up.run_id)} == {
        "post_payable": "applied", "hold_invoice": "applied"}
    assert store.list_human_tasks("T1") == []
