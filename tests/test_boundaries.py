"""Structural rules: one module writes the domain, and the write path does not depend on the agent framework."""

import ast
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "gwp"
WRITE_PATH = ["store.py", "policy.py", "executor.py", "orchestrator.py", "schema.py", "world.py", "runtime.py",
              "blobs.py", "pdftext.py", "retrieval.py", "cost.py", "agents/__init__.py"]


def _calls(path: Path, attr: str) -> list[int]:
    tree = ast.parse(path.read_text())
    return [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == attr]


def test_only_the_executor_calls_the_domain_write_method():
    callers = {p.relative_to(SRC).as_posix() for p in SRC.rglob("*.py") if _calls(p, "transact_domain")}
    assert callers == {"executor.py"}


def test_nothing_outside_the_store_calls_the_boto3_write_apis():
    raw = {"put_item", "update_item", "delete_item", "transact_write_items", "batch_write_item"}
    offenders = {p.relative_to(SRC).as_posix() for p in SRC.rglob("*.py")
                 for a in raw if _calls(p, a) and p.name != "store.py"}
    assert offenders == set()


def test_the_write_path_imports_no_agent_framework():
    for rel in WRITE_PATH:
        tree = ast.parse((SRC / rel).read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            assert not any(n.split(".")[0] == "strands" for n in names), rel


def test_importing_the_orchestrator_does_not_load_strands():
    code = "import sys, gwp.orchestrator, gwp.executor, gwp.policy; print('strands' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_pytest_q_still_prints_the_pass_count():
    """With `-q` in addopts, `pytest -q` ran at -qq and printed only dots, so a reviewer saw no count."""
    root = Path(__file__).resolve().parents[1]
    out = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                          "tests/test_boundaries.py", "-k", "not pytest_q"], cwd=root, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "passed" in out.stdout.splitlines()[-1], out.stdout


FRAMEWORKS = {"strands", "mcp", "langchain", "langchain_core", "langgraph", "opentelemetry", "fastmcp"}


def test_the_write_path_and_the_access_rules_import_no_agent_or_protocol_framework():
    """The MCP server and the planner sit outside the write path. Nothing inside it depends on them."""
    for rel in WRITE_PATH + ["access.py"]:
        tree = ast.parse((SRC / rel).read_text())
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else [])
            assert not any(n.split(".")[0] in FRAMEWORKS for n in names), (rel, names)


def test_the_mcp_server_writes_only_through_the_orchestrator_and_the_access_log():
    tree = ast.parse((SRC / "mcp_server.py").read_text())
    writes = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr.startswith(("put_", "update_", "transition_", "append_", "open_", "finalize_",
                                          "create_", "transact_", "seed"))}
    assert writes == set()
