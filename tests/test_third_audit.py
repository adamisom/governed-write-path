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


# -- Codex round 2, 9/29: the contention fallback could still leave two tasks with different reasons -------------


def _cancel_run_and_task_transactions(store, monkeypatch, times=None):
    """Cancel every run-and-task transaction (or the first `times` of them) with TransactionConflict."""
    from botocore.exceptions import ClientError

    real, cancelled = store.client.transact_write_items, []

    def maybe_cancel(**kw):
        items = kw["TransactItems"]
        is_task = any(i.get("Put", {}).get("Item", {}).get("sk", {}).get("S", "").startswith("TASK#") for i in items)
        if is_task and (times is None or len(cancelled) < times):
            cancelled.append(kw)
            raise ClientError({"Error": {"Code": "TransactionCanceledException", "Message": "cancelled"},
                               "CancellationReasons": [{"Code": "TransactionConflict"}, {"Code": "None"}]},
                              "TransactWriteItems")
        return real(**kw)

    monkeypatch.setattr(store.client, "transact_write_items", maybe_cancel)
    return cancelled


def _parked(store):
    from test_external_proposals import parked
    return parked(store)


def test_a_cancelled_run_and_task_transaction_is_tried_again_before_any_fallback(store, monkeypatch):
    from test_external_proposals import AGENT

    orch, res, _ = _parked(store)
    orch.submit_proposal("T1", res.run_id, {"proposals": []}, AGENT)
    cancelled = _cancel_run_and_task_transactions(store, monkeypatch, times=2)
    monkeypatch.setattr(orch, "_open_task", lambda *a: pytest.fail("the third try should have gone through"))
    out = orch.submit_proposal("T1", res.run_id, {"proposals": []}, AGENT)
    assert len(cancelled) == 2 and (out.outcome, out.reason) == ("NEEDS_HUMAN", "invalid_proposal")
    assert [t["reason_code"] for t in store.list_human_tasks("T1")] == ["invalid_proposal"]


def test_the_sweep_cannot_finalize_a_run_between_the_fallbacks_task_and_its_update(store, monkeypatch):
    """Codex round 2's sequence: the worker is past the lease its claim set, the transaction stays cancelled, and
    the sweep runs right after the fallback's task put. The fallback reserves the run with a fresh lease first."""
    from test_external_proposals import AGENT

    orch, res, clock = _parked(store)
    orch.submit_proposal("T1", res.run_id, {"proposals": []}, AGENT)
    real_validate = orch._validate_proposal

    def slow(tenant_id, raw, trace):
        clock.advance(RUN_LEASE_SECONDS - 50)
        return real_validate(tenant_id, raw, trace)

    monkeypatch.setattr(orch, "_validate_proposal", slow)
    _cancel_run_and_task_transactions(store, monkeypatch)
    real_open, swept = store.open_human_task, []

    def open_then_sweep(task):
        ok = real_open(task)
        clock.advance(100)  # past the lease the claim set, not the one the reservation set
        swept.append(orch.recover_stranded())
        return ok

    monkeypatch.setattr(store, "open_human_task", open_then_sweep)
    out = orch.submit_proposal("T1", res.run_id, {"proposals": []}, AGENT)
    monkeypatch.undo()
    assert swept == [[]]
    run = store.get_run("T1", res.run_id)
    assert (out.reason, run["state"], run["reason"], run.get("pending_task_reason")) == (
        "invalid_proposal", "finalized", "invalid_proposal", None)
    assert [t["reason_code"] for t in store.list_human_tasks("T1")] == ["invalid_proposal"]


def test_a_worker_that_dies_after_reserving_the_run_leaves_the_sweep_one_task_under_the_same_reason(store,
                                                                                                    monkeypatch):
    from test_external_proposals import AGENT

    class Die(BaseException):
        pass

    orch, res, clock = _parked(store)
    orch.submit_proposal("T1", res.run_id, {"proposals": []}, AGENT)
    _cancel_run_and_task_transactions(store, monkeypatch)
    monkeypatch.setattr(store, "open_human_task", lambda task: (_ for _ in ()).throw(Die()))
    with pytest.raises(Die):
        orch.submit_proposal("T1", res.run_id, {"proposals": []}, AGENT)
    monkeypatch.undo()
    assert store.get_run("T1", res.run_id)["pending_task_reason"] == "invalid_proposal"
    clock.advance(RUN_LEASE_SECONDS + 30)
    assert [r["outcome"] for r in orch.recover_stranded()] == ["NEEDS_HUMAN"]
    assert store.get_run("T1", res.run_id)["reason"] == "invalid_proposal"
    assert [t["reason_code"] for t in store.list_human_tasks("T1")] == ["invalid_proposal"]


def test_a_bank_change_run_whose_transactions_stay_cancelled_posts_with_one_task_while_the_sweep_runs(store,
                                                                                                    monkeypatch):
    orch, up = _bank_run(store)
    _cancel_run_and_task_transactions(store, monkeypatch)
    real_put, real_open = store.put_audit, store.open_human_task

    def slow_put(audit):
        orch.clock.advance(RUN_LEASE_SECONDS - 50)  # the worker nears the end of the lease step 9 renewed
        real_put(audit)

    def open_then_sweep(task):
        ok = real_open(task)
        orch.clock.advance(100)  # past the lease step 9 set, not the one the reservation set
        assert orch.recover_stranded() == []
        return ok

    monkeypatch.setattr(store, "put_audit", slow_put)

    monkeypatch.setattr(store, "open_human_task", open_then_sweep)
    assert orch.process("T1", up.run_id).outcome == "APPLIED"
    monkeypatch.undo()
    assert [t["reason_code"] for t in store.list_human_tasks("T1")] == ["vendor_requested_bank_change"]


def test_resume_changes_nothing_while_its_transaction_stays_cancelled_and_the_next_sweep_finishes(store,
                                                                                                 monkeypatch):
    orch, up = _bank_run(store)

    class Die(BaseException):
        pass

    real_update = store.update_run_with_task
    monkeypatch.setattr(store, "update_run_with_task", lambda *a, **k: (_ for _ in ()).throw(Die()))
    with pytest.raises(Die):
        orch.process("T1", up.run_id)  # dies before the set is marked recorded
    monkeypatch.setattr(store, "update_run_with_task", real_update)
    orch.clock.advance(RUN_LEASE_SECONDS + 30)
    _cancel_run_and_task_transactions(store, monkeypatch)
    monkeypatch.setattr(orch, "_open_task", lambda *a: pytest.fail("resume has no fallback"))
    assert [r["outcome"] for r in orch.recover_stranded()] == ["IN_PROGRESS"]
    assert {a["status"] for a in store.list_audits("T1", up.run_id)} == {"proposed"}
    assert store.list_human_tasks("T1") == []
    monkeypatch.undo()
    assert [r["outcome"] for r in orch.recover_stranded()] == ["NEEDS_HUMAN"]
    assert [t["reason_code"] for t in store.list_human_tasks("T1")] == ["interrupted"]
