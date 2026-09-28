"""AWS Lambda handler behind API Gateway (HTTP API, payload format 2.0), plus the scheduled staleness check.

Routes:
    POST /documents             upload a document (base64 body); 202 with the run id, processing runs async
    GET  /runs/{run_id}         a run's state, outcome, reason, cost and applied writes
    GET  /approvals             pending approvals, each shown code checks first
    POST /approvals/{audit_id}  {"decision": "approve" | "decline", "note": "..."}
    POST /reverts/{audit_id}    revert an applied write, or get the recorded refusal

Processing a document calls two models with budgets of 30 and 60 seconds, each
with one retry, which does not fit API Gateway's HTTP API integration timeout of
about 30 seconds. So `POST /documents` stores the document, creates the run,
hands processing to an asynchronous invocation of this same function (event
`{"source": "gwp.process", ...}`), and returns 202 at once. The client polls
`GET /runs/{run_id}`. A redelivered processing event is safe: `process` claims
the run with a conditional update, so a second delivery returns IN_PROGRESS or
the finished outcome and calls no model.

Authentication in v0 is one API key per role, as the spec says. Each key maps to
a principal, a role and a tenant, so the tenant always comes from the key and
never from the request. Keys are stored hashed in the GWP_API_KEYS environment
variable (JSON: {sha256(key): {"principal_id", "role", "tenant_id"}}). Terraform
sets that variable from a Terraform variable today; moving it to Secrets Manager
is an open item.

Written and tested against moto; never deployed.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any, Callable

from .orchestrator import InvalidDecision, NotAllowed, Orchestrator
from .runtime import sha256_hex
from .schema import Principal, Role

_ORCH: Orchestrator | None = None
_FACTORY: Callable[[], Orchestrator] | None = None
_DISPATCH: Callable[[str, str, Any], None] | None = None
PROCESS_EVENT = "gwp.process"


def set_orchestrator_factory(factory: Callable[[], Orchestrator] | None) -> None:
    """Tests inject an orchestrator with scripted models; production builds one from the environment."""
    global _FACTORY, _ORCH
    _FACTORY, _ORCH = factory, None


def _default_orchestrator() -> Orchestrator:
    from .agents.strands_agents import DEFAULT_MODELS, StrandsProposer, StrandsReader, live_model
    from .blobs import S3Blobs
    from .runtime import Clock, UuidIds
    from .store import DynamoStore

    provider = os.environ.get("GWP_MODEL_PROVIDER", "bedrock")
    region = os.environ.get("AWS_REGION", "us-east-1")
    reader_id = os.environ.get("GWP_READER_MODEL", DEFAULT_MODELS[provider]["reader"])
    proposer_id = os.environ.get("GWP_PROPOSER_MODEL", DEFAULT_MODELS[provider]["proposer"])
    store = DynamoStore(records_table=os.environ["GWP_RECORDS_TABLE"], audit_table=os.environ["GWP_AUDIT_TABLE"],
                        region=region)
    return Orchestrator(
        store, S3Blobs(os.environ["GWP_DOCUMENT_BUCKET"], region=region),
        StrandsReader(live_model(provider, reader_id, region=region), reader_id, budget_s=30),
        StrandsProposer(live_model(provider, proposer_id, region=region), proposer_id, budget_s=60),
        Clock(), UuidIds(),
    )


def set_dispatcher(dispatch: Callable[[str, str, Any], None] | None) -> None:
    """Tests inject a dispatcher; on Lambda the default invokes this function asynchronously."""
    global _DISPATCH
    _DISPATCH = dispatch


def _default_dispatch(tenant_id: str, run_id: str, context: Any) -> None:
    arn = getattr(context, "invoked_function_arn", None)
    if arn is None:
        # Not on Lambda (a local run): process in this call. The response is still 202 with the run id.
        _orchestrator().process(tenant_id, run_id)
        return
    import boto3

    boto3.client("lambda").invoke(FunctionName=arn, InvocationType="Event",
                                  Payload=json.dumps({"source": PROCESS_EVENT, "tenant_id": tenant_id,
                                                      "run_id": run_id}).encode())


def _orchestrator() -> Orchestrator:
    global _ORCH
    if _ORCH is None:
        _ORCH = (_FACTORY or _default_orchestrator)()
    return _ORCH


def _principal(headers: dict) -> tuple[Principal, str] | None:
    key = headers.get("x-api-key") or headers.get("X-Api-Key")
    if not key:
        return None
    table = json.loads(os.environ.get("GWP_API_KEYS", "{}"))
    entry = table.get(sha256_hex(key))
    if not entry:
        return None
    return Principal(principal_id=entry["principal_id"], role=Role(entry["role"])), entry["tenant_id"]


def _resp(status: int, body: Any) -> dict:
    return {"statusCode": status, "headers": {"content-type": "application/json"},
            "body": json.dumps(body, default=str)}


def handler(event: dict, context: Any = None) -> dict:
    if event.get("source") == PROCESS_EVENT:
        # An asynchronous invocation from POST /documents. API Gateway events never carry a top-level "source".
        res = _orchestrator().process(event["tenant_id"], event["run_id"])
        return {"run_id": res.run_id, "outcome": res.outcome}
    if event.get("source") == "gwp.staleness":
        orch = _orchestrator()
        stranded = orch.recover_stranded()  # first, so records it finishes don't show as stale
        # Then the runs of stale proposed or approved records, which no lease covers (DECISIONS entry 47).
        stale_runs = orch.resume_stale()
        stale = orch.stale()
        errors = [r for r in stranded if r.get("error")]
        stale_errors = [r for r in stale_runs if r.get("error")]
        # CloudWatch picks these lines up; an alarm can watch them.
        print(json.dumps({"stranded_runs_resumed": stranded, "stale_runs_resumed": stale_runs,
                          "stale_audit_records": stale, "stranded_errors": len(errors),
                          "stale_run_errors": len(stale_errors)}))
        if errors or stale_errors:
            # Every other run was resumed and the stale list printed; fail the invocation so the Errors metric
            # shows a run the sweep can't finish, instead of a quiet success every 15 minutes.
            raise RuntimeError(f"{len(errors) + len(stale_errors)} run(s) could not be resumed: "
                               + ", ".join(f"{r['tenant_id']}/{r['run_id']}" for r in errors + stale_errors))
        return {"stale": len(stale), "stranded": len(stranded) - len(errors)}

    auth = _principal(event.get("headers") or {})
    if auth is None:
        return _resp(401, {"error": "unauthorized"})
    principal, tenant = auth
    method = event["requestContext"]["http"]["method"]
    parts = [p for p in event.get("rawPath", "/").split("/") if p]
    orch = _orchestrator()
    try:
        if method == "POST" and parts == ["documents"]:
            raw = event.get("body") or ""
            data = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode()
            ctype = (event.get("headers") or {}).get("content-type", "application/pdf")
            up = orch.upload(tenant, data, ctype, principal)
            if up.duplicate:
                return _resp(200, {"run_id": up.run_id, "outcome": up.outcome})
            (_DISPATCH or _default_dispatch)(tenant, up.run_id, context)
            return _resp(202, {"run_id": up.run_id, "document_id": up.document_id, "state": "received"})
        if method == "GET" and len(parts) == 2 and parts[0] == "runs":
            run = orch.store.get_run(tenant, parts[1])
            if run is None:
                return _resp(404, {"error": "not found"})
            return _resp(200, {k: run.get(k) for k in ("run_id", "state", "outcome", "reason", "reasons", "cost_usd",
                                                        "latency_ms", "audit_ids", "applied_audit_ids")})
        if method == "GET" and parts == ["approvals"]:
            if principal.role not in (Role.approver, Role.admin):
                return _resp(403, {"error": "forbidden"})
            pending = [a for a in orch.store.list_audits(tenant) if a["status"] == "pending_approval"]
            return _resp(200, [{"audit_id": a["audit_id"], **orch.approval_view(tenant, a["audit_id"])}
                               for a in pending])
        if method == "POST" and len(parts) == 2 and parts[0] == "approvals":
            body = json.loads(event.get("body") or "{}")
            res = orch.approve(tenant, parts[1], principal, body.get("decision"), body.get("note"))
            # retryable: the decision is recorded and the apply was cancelled by contention; the retry applies it.
            code = {"already_decided": 409, "retryable": 503}.get(res.status, 200)
            return _resp(code, res.__dict__)
        if method == "POST" and len(parts) == 2 and parts[0] == "reverts":
            res = orch.revert(tenant, parts[1], principal)
            return _resp(200 if res.outcome == "REVERTED" else 409, res.__dict__)
    except NotAllowed as e:
        return _resp(403, {"error": str(e)})
    except InvalidDecision as e:
        return _resp(400, {"error": str(e)})
    except KeyError:
        return _resp(404, {"error": "not found"})
    return _resp(404, {"error": "no such route"})
