"""The Terraform for the Lambda grants what the code actually calls.

Codex review finding 1 said the role needs `dynamodb:TransactWriteItems`. IAM has no such action: each
operation inside a TransactWriteItems call is authorized by its item action (a Put by `dynamodb:PutItem`, an
Update by `dynamodb:UpdateItem`, a ConditionCheck by `dynamodb:ConditionCheckItem`, a Delete by
`dynamodb:DeleteItem`). This test records every DynamoDB call the runtime makes, maps each one (and each
operation inside a transaction) to that item action on its table or index, and checks the policy in
`infra/terraform/lambda.tf` grants it.
"""

import json
import re
from pathlib import Path

import boto3
import pytest
from helpers import C01_DOC, UPLOADER, build, c01_post

from gwp import api
from gwp.evals.cases import load_cases
from gwp.evals.documents import faithful_extraction, render
from gwp.evals.runner import run_case
from gwp.runtime import sha256_hex
from gwp.store import AUDIT, RECORDS

TF = Path(__file__).resolve().parents[1] / "infra" / "terraform"

# Used only by the eval harness and the demo seeding, never by the Lambda.
HARNESS_ONLY = {"create_table", "batch_write_item", "scan"}
TRANSACT_OP_ACTION = {"Put": "PutItem", "Update": "UpdateItem", "ConditionCheck": "ConditionCheckItem",
                      "Delete": "DeleteItem"}
CALL_ACTION = {"get_item": "GetItem", "query": "Query", "put_item": "PutItem", "update_item": "UpdateItem",
               "delete_item": "DeleteItem", "batch_get_item": "BatchGetItem", "transact_get_items": "GetItem"}


def lambda_policy_grants(tf_text: str) -> set[tuple[str, str, str | None]]:
    """(action, table, index) triples that the Lambda role's DynamoDB statements allow."""
    grants = set()
    body = tf_text.split('resource "aws_iam_role_policy" "lambda"', 1)[1].split('resource "', 1)[0]
    for stmt in re.split(r"\bSid\s*=", body)[1:]:  # one segment per statement
        actions = re.findall(r'"dynamodb:(\w+)"', stmt)
        if not actions:
            continue
        resource = re.search(r"Resource\s*=\s*(\[[^\]]*\]|[^\n]+)", stmt).group(1)
        targets = []
        for table, index in re.findall(r"aws_dynamodb_table\.(\w+)\.arn\}?(?:/index/(\w+))?", resource):
            targets.append((table, index or None))
        for a in actions:
            for table, index in targets:
                grants.add((a, table, index))
    return grants


class Recorder:
    """Wraps a boto3 DynamoDB client and records (action, table name, index) for each call."""

    def __init__(self, client, seen):
        self._client, self._seen = client, seen

    def __getattr__(self, name):
        fn = getattr(self._client, name)
        if not callable(fn) or name.startswith("_") or name in ("meta", "exceptions", "get_paginator"):
            return fn

        def call(**kw):
            if name == "transact_write_items":
                for op in kw["TransactItems"]:
                    (kind, spec), = op.items()
                    self._seen.add((TRANSACT_OP_ACTION[kind], spec["TableName"], None))
            elif name not in HARNESS_ONLY and name in CALL_ACTION:
                self._seen.add((CALL_ACTION[name], kw.get("TableName"), kw.get("IndexName")))
            elif name not in HARNESS_ONLY:
                self._seen.add((f"unmapped:{name}", kw.get("TableName"), kw.get("IndexName")))
            return fn(**kw)

        return call


@pytest.fixture
def recorded(monkeypatch):
    seen: set = set()
    real = boto3.client

    def client(name, *a, **kw):
        c = real(name, *a, **kw)
        return Recorder(c, seen) if name == "dynamodb" else c

    monkeypatch.setattr(boto3, "client", client)
    return seen


def _api_flow(monkeypatch):
    """Upload, async process, list approvals, approve, revert and the staleness sweep, through the handler."""
    from moto import mock_aws

    from gwp.store import DynamoStore
    from gwp.world import seed_world

    keys = {sha256_hex("up"): {"principal_id": "user:up", "role": "uploader", "tenant_id": "T1"},
            sha256_hex("ap"): {"principal_id": "user:ap", "role": "approver", "tenant_id": "T1"}}
    monkeypatch.setenv("GWP_API_KEYS", json.dumps(keys))
    with mock_aws():
        store = DynamoStore(boto3.client("dynamodb", region_name="us-east-1"))
        store.create_tables()
        seed_world(store)
        o, _, _ = build(store, [{"tool": "Extraction", "input": faithful_extraction(C01_DOC)}],
                        [{"tool": "propose_write", "input": {"proposals": [c01_post(requires_approval_reason="x")]}}])
        api.set_orchestrator_factory(lambda: o)
        api.set_dispatcher(lambda tenant, run_id, context: None)
        try:
            def ev(method, path, key, body=None):
                return {"requestContext": {"http": {"method": method}}, "rawPath": path, "body": body,
                        "headers": {"x-api-key": key, "content-type": "application/pdf"}, "isBase64Encoded": False}

            import base64
            up = api.handler({**ev("POST", "/documents", "up", base64.b64encode(render(C01_DOC)).decode()),
                              "isBase64Encoded": True})
            run_id = json.loads(up["body"])["run_id"]
            api.handler({"source": "gwp.process", "tenant_id": "T1", "run_id": run_id})
            api.handler(ev("GET", f"/runs/{run_id}", "up"))
            (item,) = json.loads(api.handler(ev("GET", "/approvals", "ap"))["body"])
            api.handler(ev("POST", f"/approvals/{item['audit_id']}", "ap", json.dumps({"decision": "approve"})))
            api.handler(ev("POST", f"/reverts/{item['audit_id']}", "ap"))
            # A run whose processing event never arrived: the sweep finalizes it and opens its task in one
            # transaction (Codex round 3).
            stranded = o.upload("T1", render({**C01_DOC, "invoice_number": "INV-IAM"}), "application/pdf", UPLOADER)
            store.update_run("T1", stranded.run_id, {"lease_until": "2000-01-01T00:00:00Z"})
            api.handler({"source": "gwp.staleness"})
            assert [t["run_id"] for t in store.list_human_tasks("T1")] == [stranded.run_id]
            o.resume("T1", run_id)
            return store.tables
        finally:
            api.set_orchestrator_factory(None)
            api.set_dispatcher(None)


def _needed(seen: set, tables: dict) -> set[tuple[str, str, str | None]]:
    by_name = {v: k for k, v in tables.items()}
    return {(a, by_name.get(t, t), i) for a, t, i in seen}


def test_policy_parser_reads_the_statements():
    grants = lambda_policy_grants((TF / "lambda.tf").read_text())
    assert ("PutItem", "records", None) in grants
    assert ("Query", "audit", "open_by_age") in grants
    assert not any(a == "DeleteItem" for a, _, _ in grants)


@pytest.mark.eval
def test_lambda_policy_grants_every_dynamodb_action_the_code_uses(recorded, monkeypatch):
    tables = _api_flow(monkeypatch)
    # Every write action, every revert kind, a redelivery, a crash and resume, and routed runs.
    for case in load_cases():
        if case.id[0] in "CARDT":
            run_case(case, "cooperative")
    needed = _needed(recorded, tables)
    assert not [n for n in needed if n[0].startswith("unmapped:")], needed
    # Sanity: the flows above did exercise the transactional writes on both tables.
    assert ("UpdateItem", AUDIT, None) in needed and ("PutItem", RECORDS, None) in needed
    grants = lambda_policy_grants((TF / "lambda.tf").read_text())
    missing = sorted((n for n in needed if n not in grants), key=str)
    assert missing == [], f"lambda.tf does not grant {missing}"


def test_run_leases_outlast_the_lambda_timeout_and_the_async_event_age():
    """Codex review finding 2: the sweep may resume a run only once its worker can no longer be running."""
    from gwp.orchestrator import DISPATCH_LEASE_SECONDS, RUN_LEASE_SECONDS

    tf = (TF / "lambda.tf").read_text()
    timeout = int(re.search(r"^\s*timeout\s*=\s*(\d+)", tf, re.M).group(1))
    event_age = int(re.search(r"maximum_event_age_in_seconds\s*=\s*(\d+)", tf).group(1))
    assert timeout < RUN_LEASE_SECONDS
    assert event_age + RUN_LEASE_SECONDS <= DISPATCH_LEASE_SECONDS


def test_the_records_table_has_the_leased_runs_index_the_store_queries():
    from gwp.store import LEASE_INDEX

    records = (TF / "storage.tf").read_text().split('resource "aws_dynamodb_table" "audit"')[0]
    assert f'name            = "{LEASE_INDEX}"' in records
    assert 'hash_key        = "lease_flag"' in records and 'range_key       = "lease_until"' in records
