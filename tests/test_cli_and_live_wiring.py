"""The CLI's guard rails, and that the live model wiring builds without calling anything."""

from gwp.agents.strands_agents import live_model
from gwp.cli import main


def test_live_eval_refuses_without_explicit_spend_confirmation(capsys):
    assert main(["eval", "--mode", "live"]) == 2
    assert "--confirm-spend" in capsys.readouterr().err


def test_live_eval_refuses_without_credentials(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main(["eval", "--mode", "live", "--provider", "anthropic", "--confirm-spend", "--max-usd", "1"]) == 2
    assert "No credentials" in capsys.readouterr().err


def test_live_models_construct_offline(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    a = live_model("anthropic", "claude-sonnet-5")
    assert a.get_config()["model_id"] == "claude-sonnet-5"
    b = live_model("bedrock", "global.anthropic.claude-sonnet-5", region="us-east-1")
    assert b.get_config()["model_id"] == "global.anthropic.claude-sonnet-5"


def test_offline_eval_cli_on_two_cases(tmp_path, capsys):
    assert main(["eval", "--only", "C01,I06", "--out", str(tmp_path), "--fail-on-regression"]) == 0
    out = capsys.readouterr().out
    assert "cooperative: 2/2 success" in out and "unsafe ['I06']" in out
    assert (tmp_path / "eval-offline-cooperative-adversarial.json").exists()


def test_offline_report_labels_retrieval_recall_as_trivial(tmp_path):
    """Audit finding 6: offline retrieval recall is 100% by construction, and the report must say so."""
    assert main(["eval", "--only", "Q05", "--script", "cooperative", "--out", str(tmp_path)]) == 0
    md = (tmp_path / "eval-offline-cooperative.md").read_text()
    assert "Retrieval recall (offline: trivially 100%" in md and "measures nothing" in md


def test_live_cost_estimate_comes_from_the_research_figure_not_synthetic_tokens(capsys):
    """Audit finding 8: the doc's $2 to $3 came from synthetic tokens. 44 live cases, 46 runs a repeat, $0.036 each."""
    from gwp.cli import estimate_live_cost
    from gwp.evals.cases import load_cases

    assert estimate_live_cost(load_cases(), 3) == (44, 138, 4.97)
    assert main(["eval", "--mode", "live", "--confirm-spend"]) == 2  # no --max-usd: refuse, and show the estimate
    assert "about $4.97" in capsys.readouterr().err
