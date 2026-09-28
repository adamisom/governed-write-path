"""A demo of the MCP server that runs offline: a seeded store, a scripted reader and invoices waiting for a proposal.

DynamoDB is mocked in-process by moto, and the reader is a real Strands agent driving a scripted model that returns
each invoice's fields as printed, as in the offline evals. Nothing calls a network or a model provider.

`gwp mcp serve --demo` serves it, and `gwp mcp walkthrough` drives one invoice through propose, approve and revert
over streamable HTTP with a key for each role, including calls each role may not make.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from .agents import ProposerInput, ProposerResult
from .blobs import MemoryBlobs
from .orchestrator import Orchestrator
from .retrieval import Retriever
from .runtime import Clock, Ids, sha256_hex
from .schema import Principal, Role

DEMO_CASES = ("C01", "A01")  # an invoice code applies by itself, and one over the auto limit that needs approval
UPLOADER = Principal(principal_id="user:uploader", role=Role.uploader)

# Demo keys only. Each maps to a principal, a role and a tenant, in the same table format as the HTTP API.
DEMO_KEYS = {
    "demo-agent-key": ("agent:demo", "agent", "T1"),
    "demo-approver-key": ("user:approver", "approver", "T1"),
    "demo-admin-key": ("user:admin", "admin", "T1"),
    "demo-t2-agent-key": ("agent:t2", "agent", "T2"),
}


def demo_keys_json() -> str:
    return json.dumps({sha256_hex(k): {"principal_id": p, "role": r, "tenant_id": t}
                       for k, (p, r, t) in DEMO_KEYS.items()})


class NoInternalProposer:
    """In external-proposal mode the orchestrator never calls its own proposer."""

    def propose(self, inp: ProposerInput, retriever: Retriever | None, attempt: int) -> ProposerResult:
        raise RuntimeError("external-proposal mode: an agent proposes over MCP")


@dataclass
class Demo:
    orch: Orchestrator
    runs: dict[str, str] = field(default_factory=dict)  # case id -> parked run id
    proposals: dict[str, list[dict]] = field(default_factory=dict)  # case id -> the case's correct proposal set
    searches: dict[str, list[str]] = field(default_factory=dict)  # case id -> the policy searches the case makes


def build_demo(case_ids: tuple[str, ...] = DEMO_CASES, clock: Clock | None = None, ids: Ids | None = None) -> Demo:
    """Seed a store, upload each case's invoice and read it, so each run waits for a proposal.

    Call it inside moto's `mock_aws()`.
    """
    import boto3

    from .agents.scripted import ScriptedModel
    from .agents.strands_agents import StrandsReader
    from .evals import documents
    from .evals.cases import load_cases
    from .store import DynamoStore
    from .world import seed_world

    for k, v in (("AWS_ACCESS_KEY_ID", "testing"), ("AWS_SECRET_ACCESS_KEY", "testing"),
                 ("AWS_DEFAULT_REGION", "us-east-1")):
        os.environ.setdefault(k, v)
    store = DynamoStore(boto3.client("dynamodb", region_name="us-east-1"))
    store.create_tables()
    seed_world(store)
    reader_model = ScriptedModel([], "claude-haiku-4-5")
    orch = Orchestrator(store, MemoryBlobs(), StrandsReader(reader_model, "claude-haiku-4-5", synthetic=True),
                        NoInternalProposer(), clock or Clock(), ids or Ids(), external_proposals=True)
    cases = {c.id: c for c in load_cases()}
    demo = Demo(orch)
    for cid in case_ids:
        case = cases[cid]
        doc = case.doc_spec()
        reader_model.turns.append({"tool": "Extraction", "input": documents.faithful_extraction(doc)})
        up = orch.upload(case.tenant, documents.render(doc), "application/pdf", UPLOADER)
        orch.process(case.tenant, up.run_id)
        demo.runs[cid] = up.run_id
        turns = case.run_script(0, "cooperative").get("proposer", [])
        demo.proposals[cid] = next(t["propose"] for t in turns if "propose" in t)
        demo.searches[cid] = [t["search"] for t in turns if "search" in t]
    return demo


def _text(result: Any) -> str:
    if result.is_error:
        return "ERROR " + result.content[0].text
    return json.dumps(result.structured_content, default=str)


async def walkthrough(out=print) -> dict:
    """Serve the demo over streamable HTTP in this process and drive it with one key per role. Returns a summary."""
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.server.auth.settings import AuthSettings
    from moto import mock_aws

    from .mcp_server import KeyTableVerifier, build_server, token_caller

    import anyio

    base = "http://127.0.0.1:8765"
    with mock_aws():
        # The scripted reader is a synchronous Strands call, which can't run inside this event loop.
        demo = await anyio.to_thread.run_sync(build_demo)
        server = build_server(demo.orch, token_caller, token_verifier=KeyTableVerifier(demo_keys_json()),
                              auth=AuthSettings(issuer_url="https://auth.example.test",
                                                resource_server_url=f"{base}/mcp", validate_token_resource=False))
        app = server.streamable_http_app()
        summary: dict = {"steps": []}

        async def as_role(key: str, calls: list[tuple[str, dict]]) -> list[Any]:
            results = []
            http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=base,
                                      headers={"Authorization": f"Bearer {key}"})
            async with http, Client(streamable_http_client(f"{base}/mcp", http_client=http)) as c:
                for tool, args in calls:
                    r = await c.call_tool(tool, args)
                    role = DEMO_KEYS[key][1]
                    out(f"[{role}] {tool}({', '.join(f'{k}={v!r}' for k, v in args.items() if k != 'proposals')})"
                        f" -> {_text(r)[:300]}")
                    summary["steps"].append({"role": role, "tool": tool, "is_error": r.is_error})
                    results.append(r)
            return results

        async with server.session_manager.run():
            c01, a01 = demo.runs["C01"], demo.runs["A01"]
            out("An agent proposes for two parked invoices. C01 is under the auto limit; A01 is over it.")
            await as_role("demo-agent-key", [("list_work", {}), ("get_proposal_context", {"run_id": c01}),
                                             ("propose", {"run_id": c01, "proposals": demo.proposals["C01"]})])
            r = await as_role("demo-agent-key", [
                ("search_policy", {"run_id": a01, "query": demo.searches["A01"][0]}),
                ("propose", {"run_id": a01, "proposals": demo.proposals["A01"]}),
                ("propose", {"run_id": a01, "proposals": demo.proposals["A01"]})])
            audit_id = r[1].structured_content["audit_ids"][0]
            out("\nThe agent tries to approve its own proposal, and the approver tries to revert. Both are refused "
                "and recorded.")
            await as_role("demo-agent-key", [("decide", {"audit_id": audit_id, "decision": "approve"})])
            await as_role("demo-approver-key", [("list_pending_approvals", {}),
                                                ("decide", {"audit_id": audit_id, "decision": "approve"}),
                                                ("decide", {"audit_id": audit_id, "decision": "approve"}),
                                                ("revert", {"audit_id": audit_id})])
            out("\nThe admin reverts the approved write, twice, and reads its audit record.")
            await as_role("demo-admin-key", [("revert", {"audit_id": audit_id}), ("revert", {"audit_id": audit_id}),
                                             ("get_audit", {"audit_id": audit_id})])
            out("\nAn agent from another tenant can't see this tenant's run.")
            await as_role("demo-t2-agent-key", [("get_run", {"run_id": a01})])
        records = demo.orch.store.list_access_records("T1") + demo.orch.store.list_access_records("T2")
        denied = [r for r in records if r["decision"] == "denied"]
        summary["access_records"], summary["denied"] = len(records), len(denied)
        out(f"\nAccess records written: {len(records)}, of which denied: {len(denied)}:")
        for r in denied:
            out(f"  {r['access_id']} {r['role']} {r['tool']}: {r['reason']}")
    return summary
