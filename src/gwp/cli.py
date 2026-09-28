"""Command line: run the evals and regenerate the case documents.

    gwp eval --mode offline                      both scripted models, no network, no cost
    gwp eval --mode offline --script adversarial
    gwp eval --mode live --provider anthropic --confirm-spend --max-usd 7
    gwp generate-docs
    gwp mcp walkthrough                          the MCP server: propose, approve and revert, offline
    gwp mcp serve --demo                         see gwp.mcp_cli
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from .agents.strands_agents import DEFAULT_MODELS
from .cost import RESEARCH_USD_PER_RUN
from .evals.cases import REPO_EVALS, generate_documents, load_cases
from .evals.grader import grade
from .evals.report import write
from .evals.runner import LiveConfig, run_case


def _live_credentials_present(provider: str) -> bool:
    if provider == "anthropic":
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    import boto3

    return boto3.Session().get_credentials() is not None


def estimate_live_cost(cases: list, repeats: int) -> tuple[int, int, float]:
    """(live cases, model runs, dollars) for a live pass, from the research's per-run estimate.

    A run is one `process` step, i.e. one reader call and one proposer call, before retries. Setup steps count,
    since they call the models too. Offline-only cases are skipped in live mode.
    """
    live = [c for c in cases if not c.offline_only]
    runs = 0
    for c in live:
        for step in c.setup + c.steps:
            name = step if isinstance(step, str) else next(iter(step))
            runs += name == "process"
    runs *= repeats
    return len(live), runs, round(runs * RESEARCH_USD_PER_RUN, 2)


def cmd_eval(args: argparse.Namespace) -> int:
    logging.getLogger("strands").setLevel(logging.CRITICAL)  # scripted faults log noisy tracebacks
    cases = load_cases(Path(args.cases) if args.cases else None)
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c.id in wanted]
    out = Path(args.out)
    live = None
    if args.mode == "live":
        n_cases, n_runs, usd = estimate_live_cost(cases, args.repeats)
        estimate = (f"Estimated spend: {n_cases} live cases, {n_runs} model runs at {args.repeats} repeats, about "
                    f"${usd:.2f} at ${RESEARCH_USD_PER_RUN} a run (the research estimate, before retries).")
        if not args.confirm_spend or args.max_usd is None:
            print("Live mode calls a paid model API. Pass --confirm-spend and --max-usd to run it. " + estimate,
                  file=sys.stderr)
            return 2
        if args.max_usd < usd:
            print(f"Warning: --max-usd {args.max_usd} is below the estimate, so the run will likely stop before "
                  f"every case has run {args.repeats} times. {estimate}", file=sys.stderr)
        if not _live_credentials_present(args.provider):
            print(f"No credentials for {args.provider} on this machine. Nothing was run.", file=sys.stderr)
            return 2
        models = DEFAULT_MODELS[args.provider]
        live = LiveConfig(args.provider, args.reader_model or models["reader"],
                          args.proposer_model or models["proposer"], args.region, not args.disable_search)
        cases = [c for c in cases if not c.offline_only]
        scripts = ["live"]
    else:
        scripts = ["cooperative", "adversarial"] if args.script == "both" else [args.script]

    results = {}
    t0 = time.monotonic()
    for script in scripts:
        grades = []
        spent = 0.0
        for case in cases:
            for _rep in range(args.repeats if live else 1):
                if live and spent >= args.max_usd:
                    print(f"Stopped: spend ${spent:.4f} reached the cap of ${args.max_usd}.", file=sys.stderr)
                    break
                run = run_case(case, "cooperative" if live else script, args.mode, live)
                g = grade(case, run)
                spent += g.cost_usd
                grades.append(g)
        results[script] = grades
    elapsed = round(time.monotonic() - t0, 2)
    meta = {"cases": len(cases), "elapsed_seconds": elapsed,
            "models": (f"{live.reader_model_id} (reader), {live.proposer_model_id} (proposer)" if live
                       else "scripted (offline)")}
    path = write(results, cases, args.mode, meta, out)
    for script, grades in results.items():
        ok = sum(g.verdict == "success" for g in grades)
        unsafe = [g.case_id for g in grades if g.verdict == "unsafe"]
        print(f"{script}: {ok}/{len(grades)} success, unsafe {unsafe or 'none'}")
    print(f"report: {path} ({elapsed}s)")
    if args.fail_on_regression and args.mode == "offline":
        coop = results.get("cooperative", [])
        if any(g.verdict != "success" for g in coop):
            return 1
        adv = results.get("adversarial", [])
        predicted = {c.id for c in cases if c.predicted.get("adversarial_system_success")}
        if adv and {g.case_id for g in adv if g.verdict == "unsafe"} - predicted:
            return 1
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    problems = generate_documents(load_cases())
    for p in problems:
        print(p, file=sys.stderr)
    print(f"wrote documents to {REPO_EVALS / 'documents'}")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gwp")
    sub = parser.add_subparsers(dest="command", required=True)
    e = sub.add_parser("eval", help="run the eval set")
    e.add_argument("--mode", choices=["offline", "live"], default="offline")
    e.add_argument("--script", choices=["cooperative", "adversarial", "both"], default="both")
    e.add_argument("--out", default="eval-out")
    e.add_argument("--cases", default=None, help="directory of case YAML files")
    e.add_argument("--only", default=None, help="comma-separated case ids")
    e.add_argument("--provider", choices=["anthropic", "bedrock"], default="anthropic")
    e.add_argument("--reader-model", default=None)
    e.add_argument("--proposer-model", default=None)
    e.add_argument("--region", default=None)
    e.add_argument("--repeats", type=int, default=3, help="live mode: runs per case")
    e.add_argument("--max-usd", type=float, default=None,
                   help="live mode, required: stop starting cases once spend reaches this. A full pass at 3 repeats "
                        "is about $5 by the research estimate; 7 leaves room for retries")
    e.add_argument("--confirm-spend", action="store_true")
    e.add_argument("--disable-search", action="store_true",
                   help="live mode: remove the proposer's search tool (the H2 ablation)")
    e.add_argument("--fail-on-regression", action="store_true",
                   help="offline: exit 1 if any cooperative case fails or an unpredicted case is unsafe")
    e.set_defaults(func=cmd_eval)
    g = sub.add_parser("generate-docs", help="render every case document to PDF and check it against its spec")
    g.set_defaults(func=cmd_generate)
    from .mcp_cli import add_parser as add_mcp

    add_mcp(sub)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
