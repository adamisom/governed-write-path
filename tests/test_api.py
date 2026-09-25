"""The Lambda handler, with moto and scripted models."""

import base64
import json

import pytest
from helpers import C01_DOC, build, c01_post

from gwp import api
from gwp.evals import documents
from gwp.runtime import sha256_hex

KEYS = {
    sha256_hex("up-key"): {"principal_id": "user:up", "role": "uploader", "tenant_id": "T1"},
    sha256_hex("ap-key"): {"principal_id": "user:ap", "role": "approver", "tenant_id": "T1"},
    sha256_hex("t2-ap-key"): {"principal_id": "user:t2", "role": "approver", "tenant_id": "T2"},
}


@pytest.fixture
def orch(store, monkeypatch):
    monkeypatch.setenv("GWP_API_KEYS", json.dumps(KEYS))
    ext = documents.faithful_extraction(C01_DOC)
    o, _, _ = build(store, [{"tool": "Extraction", "input": ext}],
                    [{"tool": "propose_write", "input": {"proposals": [c01_post(requires_approval_reason="x")]}}])
    api.set_orchestrator_factory(lambda: o)
    queued = []
    api.set_dispatcher(lambda tenant, run_id, context: queued.append((tenant, run_id)))
    o.queued = queued
    yield o
    api.set_orchestrator_factory(None)
    api.set_dispatcher(None)


def _event(method, path, key, body=None, b64=False):
    return {"requestContext": {"http": {"method": method}}, "rawPath": path,
            "headers": {"x-api-key": key, "content-type": "application/pdf"},
            "body": body, "isBase64Encoded": b64}


def test_upload_approve_and_tenant_binding(orch):
    pdf = base64.b64encode(documents.render(C01_DOC)).decode()
    r = api.handler(_event("POST", "/documents", "up-key", pdf, True))
    # Audit finding 7: the request returns before any model is called, and processing is dispatched.
    assert r["statusCode"] == 202
    body = json.loads(r["body"])
    assert body["state"] == "received" and orch.queued == [("T1", body["run_id"])]
    run_path = f"/runs/{body['run_id']}"
    assert json.loads(api.handler(_event("GET", run_path, "up-key"))["body"])["state"] == "received"
    done = api.handler({"source": "gwp.process", "tenant_id": "T1", "run_id": body["run_id"]})
    assert done["outcome"] == "PENDING_APPROVAL"
    # A redelivered processing event calls no model and changes nothing.
    assert api.handler({"source": "gwp.process", "tenant_id": "T1", "run_id": body["run_id"]})["outcome"] == \
        "PENDING_APPROVAL"
    run = json.loads(api.handler(_event("GET", run_path, "up-key"))["body"])
    assert run["outcome"] == "PENDING_APPROVAL"
    aid = run["audit_ids"][0]

    assert api.handler(_event("GET", "/approvals", "up-key"))["statusCode"] == 403
    listing = json.loads(api.handler(_event("GET", "/approvals", "ap-key"))["body"])
    assert [x["audit_id"] for x in listing] == [aid]

    # An approver from another tenant can't see or decide T1's records.
    assert json.loads(api.handler(_event("GET", "/approvals", "t2-ap-key"))["body"]) == []
    assert api.handler(_event("POST", f"/approvals/{aid}", "t2-ap-key", json.dumps({"decision": "approve"})))[
        "statusCode"] == 404

    assert api.handler(_event("POST", f"/approvals/{aid}", "ap-key", json.dumps({})))["statusCode"] == 400
    assert api.handler(_event("POST", f"/approvals/{aid}", "up-key", json.dumps({"decision": "approve"})))[
        "statusCode"] == 403
    ok = api.handler(_event("POST", f"/approvals/{aid}", "ap-key", json.dumps({"decision": "approve"})))
    assert ok["statusCode"] == 200 and json.loads(ok["body"])["status"] == "applied"
    again = api.handler(_event("POST", f"/approvals/{aid}", "ap-key", json.dumps({"decision": "approve"})))
    assert again["statusCode"] == 409

    assert api.handler(_event("POST", f"/reverts/{aid}", "ap-key"))["statusCode"] == 200
    dup = api.handler(_event("POST", "/documents", "up-key", pdf, True))
    assert json.loads(dup["body"])["outcome"] == "DUPLICATE_UPLOAD"


def test_unknown_key_is_rejected(orch):
    assert api.handler(_event("GET", "/approvals", "nope"))["statusCode"] == 401


def test_scheduled_staleness_event(orch):
    assert api.handler({"source": "gwp.staleness"}) == {"stale": 0}


def test_default_dispatch_invokes_this_function_asynchronously(monkeypatch):
    import boto3

    calls = []

    class FakeLambda:
        def invoke(self, **kw):
            calls.append(kw)

    monkeypatch.setattr(boto3, "client", lambda name, **kw: FakeLambda() if name == "lambda" else None)

    class Ctx:
        invoked_function_arn = "arn:aws:lambda:us-east-1:111122223333:function:gwp-api"

    api._default_dispatch("T1", "R-1", Ctx())
    (call,) = calls
    assert call["InvocationType"] == "Event" and call["FunctionName"] == Ctx.invoked_function_arn
    assert json.loads(call["Payload"]) == {"source": "gwp.process", "tenant_id": "T1", "run_id": "R-1"}
