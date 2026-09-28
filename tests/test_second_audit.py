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
    assert (retry.status, retry.run_outcome, retry.detail) == ("already_decided", "APPLIED", "applied")
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
