"""Markdown and JSON reports for an eval run."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from ..cost import PRICES
from .cases import Case
from .grader import Grade
from .metrics import compute


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _ms(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0f} ms"


def predictions(script: str, grades: list[Grade], cases: list[Case]) -> dict:
    """Compare the spec's advance predictions with what happened."""
    by_id = {c.id: c for c in cases}
    out: dict = {}
    if script == "cooperative":
        passed = sum(g.verdict == "success" for g in grades)
        out["all_cases_pass"] = {"predicted": f"{len(grades)} of {len(grades)}", "observed": f"{passed} of {len(grades)}",
                                 "held": passed == len(grades)}
    else:
        predicted = sorted(c.id for c in cases if c.predicted.get("adversarial_system_success"))
        attacked = [g for g in grades if by_id[g.case_id].attack]
        observed = sorted(g.case_id for g in attacked if g.injection_system)
        unsafe = sorted(g.case_id for g in grades if g.verdict == "unsafe")
        out["attacks_that_get_through"] = {"predicted": predicted, "observed": observed, "held": predicted == observed}
        out["unsafe_cases"] = {"predicted": predicted, "observed": unsafe, "held": unsafe == predicted}
        out["H1_code_layer_stops_the_rest"] = {
            "predicted": "no forbidden or injection attack changes a store except the predicted ones",
            "observed": observed, "held": set(observed) <= set(predicted),
        }
    return out


def build(results: dict[str, list[Grade]], cases: list[Case], mode: str, meta: dict) -> tuple[str, dict]:
    data: dict = {"mode": mode, "meta": meta, "price_table": {"checked": PRICES["checked"], "source": PRICES["source"]},
                  "scripts": {}}
    lines = [f"# Eval report ({mode})", ""]
    for k, v in meta.items():
        lines.append(f"- {k}: {v}")
    lines.append(f"- Price table: checked {PRICES['checked']}, source {PRICES['source']}")
    lines.append("")
    for script, grades in results.items():
        m = compute(grades)
        preds = predictions(script, grades, cases) if mode == "offline" else {}
        data["scripts"][script] = {"metrics": m, "predictions": preds, "cases": [asdict(g) for g in grades]}
        ts, us = m["task_success"], m["unsafe_write_rate"]
        im, isys = m["injection_success_model_level"], m["injection_success_system_level"]
        cost = m["cost"]
        label = " (SYNTHETIC: offline token counts are characters divided by 4 from the scripted model, priced as " \
                "Haiku 4.5 for the reader and Sonnet 5 for the proposer; they are not real usage)" if cost["synthetic"] else ""
        lines += [
            f"## Script: {script}", "",
            "| Metric | Value |", "| --- | --- |",
            f"| Task success | {ts['k']} of {ts['n']} ({_pct(ts['rate'])}), Wilson 95% lower bound {_pct(ts['wilson_lower'])} |",
            f"| Unsafe-write rate | {us['k']} of {us['n']} ({_pct(us['rate'])}), Wilson 95% upper bound {_pct(us['wilson_upper'])} |",
            f"| Injection success, model level | {im['k']} of {im['n']} ({_pct(im['rate'])}) |",
            f"| Injection success, system level | {isys['k']} of {isys['n']} ({_pct(isys['rate'])}), Wilson 95% upper bound {_pct(isys['wilson_upper'])} |",
            f"| Approval precision | {m['approval_precision']['k']} of {m['approval_precision']['n']} ({_pct(m['approval_precision']['value'])}) |",
            f"| Approval recall | {m['approval_recall']['k']} of {m['approval_recall']['n']} ({_pct(m['approval_recall']['value'])}) |",
            f"| Approvals per 100 documents | {m['approvals_per_100_documents']} |",
            f"| Cost per run{' (synthetic)' if cost['synthetic'] else ''} | ${cost['per_run_usd']} |",
            f"| Cost per successful task{' (synthetic)' if cost['synthetic'] else ''} | ${cost['per_successful_task_usd']} |",
            f"| Tokens in / out{' (synthetic)' if cost['synthetic'] else ''} | {cost['input_tokens']} / {cost['output_tokens']} over {cost['model_calls']} model calls and {cost['runs']} runs |",
            f"| Run latency p50 / p95 (harness only) | {_ms(m['latency_ms']['run_p50'])} / {_ms(m['latency_ms']['run_p95'])} |",
            f"| Retrieval recall | {_pct(m['retrieval_recall'])} |",
            "",
        ]
        if label:
            lines += [f"Cost note{label}.", ""]
        lines += ["Verdicts: " + ", ".join(f"{k} {v}" for k, v in m["verdicts"].items()), ""]
        lines += ["| Category | Success |", "| --- | --- |"]
        for cat, r in m["task_success_by_category"].items():
            lines.append(f"| {cat} | {r['k']} of {r['n']} |")
        lines.append("")
        if preds:
            lines += ["Advance predictions from the spec:", ""]
            for name, p in preds.items():
                lines.append(f"- {name}: predicted {p['predicted']}, observed {p['observed']}, "
                             f"{'held' if p['held'] else 'DID NOT HOLD'}")
            lines.append("")
        lines += ["| Case | Category | Verdict | Final outcome | Injection model / system | Notes |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for g in grades:
            inj = "" if g.injection_model is None else f"{'yes' if g.injection_model else 'no'} / " \
                                                        f"{'yes' if g.injection_system else 'no'}"
            note = "; ".join(g.mismatches)[:300].replace("|", "/")
            lines.append(f"| {g.case_id} | {g.category} | {g.verdict} | {g.final_outcome} | {inj} | {note} |")
        lines.append("")
    return "\n".join(lines), data


def write(results: dict[str, list[Grade]], cases: list[Case], mode: str, meta: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    md, data = build(results, cases, mode, meta)
    stem = f"eval-{mode}-{'-'.join(results)}"
    (out_dir / f"{stem}.md").write_text(md)
    (out_dir / f"{stem}.json").write_text(json.dumps(data, indent=1, default=str))
    return out_dir / f"{stem}.md"
