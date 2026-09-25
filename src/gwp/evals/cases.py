"""Load the eval cases from YAML.

A case file states the document as structured data, the scripted human steps,
the scripted model turns for both offline scripts, and the expected outcome,
written by hand from the policy before any run. A case can `extends` another
case; dicts merge recursively and lists replace.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import documents

REPO_EVALS = Path(__file__).resolve().parents[3] / "evals"
CATEGORIES = {
    "C": "clean", "A": "approval", "F": "forbidden", "D": "duplicate", "R": "revert", "Q": "retrieval",
    "I": "injection", "T": "isolation", "L": "degradation",
}


def deep_merge(base: Any, over: Any) -> Any:
    if isinstance(base, dict) and isinstance(over, dict):
        out = copy.deepcopy(base)
        for k, v in over.items():
            out[k] = deep_merge(base.get(k), v) if k in base else copy.deepcopy(v)
        return out
    return copy.deepcopy(over)


@dataclass
class Case:
    id: str
    title: str
    category: str
    document: dict
    documents: dict[str, dict]
    steps: list[Any]
    runs: list[dict]
    expect: dict
    overrides: dict = field(default_factory=dict)
    attack: dict | None = None
    retrieval_required: list[str] = field(default_factory=list)
    retrieval_forbidden: list[str] = field(default_factory=list)
    offline_only: bool = False
    predicted: dict = field(default_factory=dict)
    tenant: str = "T1"
    raw: dict = field(default_factory=dict)

    def doc_spec(self, name: str = "main") -> dict:
        if name == "main":
            return self.document
        return deep_merge(self.document, self.documents[name])

    def run_script(self, index: int, script: str) -> dict:
        """The reader and proposer turns for the index-th process step under a script."""
        run = self.runs[index] if index < len(self.runs) else self.runs[-1]
        coop = run.get("cooperative", {})
        if script == "cooperative":
            return coop
        adv = run.get("adversarial")
        return deep_merge(coop, adv) if adv else coop


def _load_raw(path: Path, seen: dict[str, dict]) -> dict:
    raw = yaml.safe_load(path.read_text())
    parent = raw.pop("extends", None)
    if parent:
        base = seen.get(parent) or _load_raw(path.parent / f"{parent}.yaml", seen)
        # Expectations, attacks and predictions are never inherited: each case states its own.
        base = {k: v for k, v in base.items() if k not in ("expect", "attack", "predicted", "id", "title",
                                                             "offline_only", "retrieval_required")}
        raw = deep_merge(base, raw)
    seen[raw["id"]] = raw
    return raw


def load_cases(directory: Path | None = None) -> list[Case]:
    directory = directory or REPO_EVALS / "cases"
    seen: dict[str, dict] = {}
    cases = []
    for path in sorted(directory.glob("*.yaml")):
        raw = _load_raw(path, seen)
        cases.append(Case(
            id=raw["id"], title=raw["title"], category=CATEGORIES[raw["id"][0]], document=raw["document"],
            documents=raw.get("documents", {}), steps=raw.get("steps", ["upload", "process"]),
            runs=raw.get("runs", [{}]), expect=raw.get("expect", {}), overrides=raw.get("overrides", {}),
            attack=raw.get("attack"), retrieval_required=raw.get("retrieval_required", []),
            retrieval_forbidden=raw.get("retrieval_forbidden", []), offline_only=raw.get("offline_only", False),
            predicted=raw.get("predicted", {}), tenant=raw.get("tenant", "T1"), raw=raw,
        ))
    order = "CAFDRQITL"
    cases.sort(key=lambda c: (order.index(c.id[0]), c.id))
    return cases


def document_path(case_id: str, name: str = "main", directory: Path | None = None) -> Path:
    directory = directory or REPO_EVALS / "documents"
    return directory / (f"{case_id}.pdf" if name == "main" else f"{case_id}-{name}.pdf")


def generate_documents(cases: list[Case], directory: Path | None = None) -> list[str]:
    """Render every case document to PDF and verify each one against its spec. Returns problems found."""
    directory = directory or REPO_EVALS / "documents"
    directory.mkdir(parents=True, exist_ok=True)
    problems = []
    for case in cases:
        for name in ["main", *case.documents]:
            spec = case.doc_spec(name)
            pdf = documents.render(spec)
            missing = documents.verify(spec, pdf)
            if missing:
                problems.append(f"{case.id}/{name}: missing {missing}")
            document_path(case.id, name, directory).write_bytes(pdf)
    return problems
