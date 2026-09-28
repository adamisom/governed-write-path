"""`gwp mcp`: serve the governed write path over MCP, or run the offline walkthrough.

    gwp mcp walkthrough                                  propose, approve and revert over HTTP, offline
    gwp mcp serve --demo --transport http --port 8765    the demo over streamable HTTP; clients send a demo key
    gwp mcp serve --demo --transport stdio --key demo-agent-key
    gwp mcp serve --transport http                       live: DynamoDB tables and a reader model from the
                                                         environment, keys from GWP_API_KEYS (never run yet)

On stdio one process serves one caller, whose key comes from `--key` or GWP_MCP_API_KEY. On streamable HTTP every
request carries its own key as a bearer token. Either way the key maps to a principal, a role and a tenant through
the same hashed table the HTTP API uses, and nothing in a tool call can change them.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys


def _live_orchestrator():
    """Like the Lambda's orchestrator, in external-proposal mode. Written against moto; never run live."""
    from .agents.strands_agents import DEFAULT_MODELS, StrandsReader, live_model
    from .blobs import S3Blobs
    from .mcp_demo import NoInternalProposer
    from .orchestrator import Orchestrator
    from .runtime import Clock, UuidIds
    from .store import DynamoStore

    provider = os.environ.get("GWP_MODEL_PROVIDER", "bedrock")
    region = os.environ.get("AWS_REGION", "us-east-1")
    reader_id = os.environ.get("GWP_READER_MODEL", DEFAULT_MODELS[provider]["reader"])
    store = DynamoStore(records_table=os.environ["GWP_RECORDS_TABLE"], audit_table=os.environ["GWP_AUDIT_TABLE"],
                        region=region)
    return Orchestrator(store, S3Blobs(os.environ["GWP_DOCUMENT_BUCKET"], region=region),
                        StrandsReader(live_model(provider, reader_id, region=region), reader_id, budget_s=30),
                        NoInternalProposer(), Clock(), UuidIds(), external_proposals=True)


def cmd_mcp(args: argparse.Namespace) -> int:
    logging.getLogger("strands").setLevel(logging.CRITICAL)
    import anyio

    from .mcp_demo import DEMO_KEYS, build_demo, demo_keys_json, walkthrough

    if args.action == "walkthrough":
        for name in ("mcp", "httpx", "httpx2"):  # request logs would bury the walkthrough
            logging.getLogger(name).setLevel(logging.WARNING)
        anyio.run(walkthrough)
        return 0

    from mcp.server.auth.settings import AuthSettings

    from .access import caller_from_key
    from .mcp_server import KeyTableVerifier, build_server, fixed_caller, token_caller

    log = lambda msg: print(msg, file=sys.stderr)  # noqa: E731 - stdout carries the protocol on stdio
    if args.demo:
        from moto import mock_aws

        mock_aws().start()  # for the life of the process
        demo = build_demo()
        orch, keys_json = demo.orch, demo_keys_json()
        log("Demo store seeded. Runs waiting for a proposal: "
            + ", ".join(f"{rid} ({cid})" for cid, rid in demo.runs.items()))
        log("Demo keys: " + ", ".join(f"{k} ({v[1]}, tenant {v[2]})" for k, v in DEMO_KEYS.items()))
    else:
        orch, keys_json = _live_orchestrator(), os.environ.get("GWP_API_KEYS", "{}")

    if args.transport == "stdio":
        key = args.key or os.environ.get("GWP_MCP_API_KEY", "")
        who = caller_from_key(key, keys_json)
        if who is None:
            log("stdio needs a known key in --key or GWP_MCP_API_KEY")
            return 2
        log(f"Serving on stdio as {who.principal.principal_id} ({who.principal.role.value}, tenant {who.tenant_id}).")
        anyio.run(build_server(orch, fixed_caller(who)).run_stdio_async)
        return 0

    url = f"http://{args.host}:{args.port}"
    server = build_server(orch, token_caller, token_verifier=KeyTableVerifier(keys_json),
                          auth=AuthSettings(issuer_url=args.issuer_url, resource_server_url=f"{url}/mcp",
                                            validate_token_resource=False))
    log(f"Serving streamable HTTP at {url}/mcp. Send 'Authorization: Bearer <key>'.")
    anyio.run(lambda: server.run_streamable_http_async(host=args.host, port=args.port))
    return 0


def add_parser(sub) -> None:
    m = sub.add_parser("mcp", help="serve the write path over MCP, or run the offline walkthrough")
    m.add_argument("action", choices=["serve", "walkthrough"])
    m.add_argument("--demo", action="store_true", help="a seeded in-process store and a scripted reader, offline")
    m.add_argument("--transport", choices=["stdio", "http"], default="http")
    m.add_argument("--host", default="127.0.0.1")
    m.add_argument("--port", type=int, default=8765)
    m.add_argument("--key", default=None, help="stdio: the caller's API key (or set GWP_MCP_API_KEY)")
    m.add_argument("--issuer-url", default="https://auth.example.test",
                   help="http: the authorization server named in the protected resource metadata")
    m.set_defaults(func=cmd_mcp)
