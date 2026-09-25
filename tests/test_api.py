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
    yield o
    api.set_orchestrator_factory(None)


def _event(method, path, key, body=None, b64=False):
    return {"requestContext": {"http": {"method": method}}, "rawPath": path,
            "headers": {"x-api-key": key, "content-type": "application/pdf"},
            "body": body, "isBase64Encoded": b64}


def test_upload_approve_and_tenant_binding(orch):
    pdf = base64.b64encode(documents.render(C01_DOC)).decode()
    r = api.handler(_event("POST", "/documents", "up-key", pdf, True))
    assert r["statusCode"] == 201
    body = json.loads(r["body"])
    assert body["outcome"] == "PENDING_APPROVAL"
    aid = body["audit_ids"][0]

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
