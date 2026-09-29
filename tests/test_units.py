"""Retrieval, cost accounting, metrics, and document generation."""

from pathlib import Path

import pytest

from gwp.cost import canonical_model, usage_cost_usd
from gwp.evals import documents
from gwp.evals.cases import document_path, load_cases
from gwp.evals.metrics import percentile, wilson
from gwp.pdftext import extract_text
from gwp.retrieval import BM25Retriever, tokenize
from gwp.world import POLICY_CHUNKS


def _r(tenant="T1"):
    return BM25Retriever(tenant, [{"tenant_id": t, "doc_id": d, "chunk_id": c, "text": x}
                                  for t, d, c, x in POLICY_CHUNKS])


@pytest.mark.parametrize("query,expected", [
    ("furniture over $1,000 fixed asset account", "POL-AP#furniture"),
    ("freight carrier invoice over $500 approval", "POL-AP#freight"),
    ("credit memo without the original invoice number", "POL-AP#credit-memos"),
    ("invoice without a purchase order number", "POL-AP#purchase-orders"),
    ("vendor asks to change bank details", "POL-AP#bank-details"),
])
def test_bm25_finds_the_right_policy_chunk(query, expected):
    assert _r().search(query)[0].chunk_id == expected


def test_bm25_is_deterministic_and_tokenizes_money():
    assert tokenize("Over $1,000.00 each") == ["over", "1000", "each"]
    assert _r().search("furniture") == _r().search("furniture")


def test_cost_from_usage_and_price_table():
    assert usage_cost_usd("claude-sonnet-5", {"inputTokens": 1_000_000, "outputTokens": 100_000,
                                              "totalTokens": 1_100_000}) == pytest.approx(3.0)
    # Cache counted inside inputTokens (input + output == total): the cached part is billed at the cache rate.
    inside = {"inputTokens": 1000, "outputTokens": 0, "totalTokens": 1000, "cacheReadInputTokens": 800}
    assert usage_cost_usd("claude-haiku-4-5", inside) == pytest.approx((200 * 1.0 + 800 * 0.1) / 1e6)
    # Cache reported on top of inputTokens.
    outside = {"inputTokens": 200, "outputTokens": 0, "totalTokens": 1000, "cacheReadInputTokens": 800}
    assert usage_cost_usd("claude-haiku-4-5", outside) == pytest.approx((200 * 1.0 + 800 * 0.1) / 1e6)
    assert usage_cost_usd("some-other-model", {"inputTokens": 5}) is None


def test_bedrock_ids_map_to_the_price_table():
    assert canonical_model("global.anthropic.claude-haiku-4-5-20251001-v1:0") == "claude-haiku-4-5"
    assert canonical_model("global.anthropic.claude-sonnet-5") == "claude-sonnet-5"


def test_wilson_bounds_match_the_spec():
    assert round(wilson(49, 49)[0], 3) == 0.927
    assert round(wilson(0, 11)[1], 3) == 0.259
    assert percentile([1, 2, 3, 4], 0.5) == 2.5


def test_rendering_is_deterministic_and_committed_documents_are_current():
    cases = load_cases()
    assert len(cases) == 109
    for case in cases:
        for name in ["main", *case.documents]:
            fresh = documents.render(case.doc_spec(name))
            assert fresh == documents.render(case.doc_spec(name))
            assert Path(document_path(case.id, name)).read_bytes() == fresh, f"run gwp generate-docs ({case.id})"


def test_every_spec_value_is_in_the_rendered_text():
    for case in load_cases():
        for name in ["main", *case.documents]:
            spec = case.doc_spec(name)
            assert documents.verify(spec, documents.render(spec)) == [], case.id


def test_verify_catches_a_rendering_that_drops_a_value():
    spec = {"kind": "invoice", "vendor_name": "Pine Street Paper Supply", "invoice_number": "INV-1",
            "invoice_date": "2026-08-01", "lines": [{"description": "x", "qty": 1, "unit_price_cents": 100}]}
    pdf = documents.render(spec)
    assert documents.verify({**spec, "po_number": "PO-7001"}, pdf) == ["PO-7001"]


def test_hidden_text_is_in_the_text_layer():
    spec = {"kind": "invoice", "vendor_name": "V", "invoice_number": "INV-1", "invoice_date": "2026-08-01",
            "lines": [], "hidden_text": "hidden words here"}
    text, pages = extract_text(documents.render(spec), "application/pdf")
    assert "hidden words here" in text and pages == 1


def test_c04_renders_to_two_pages():
    case = next(c for c in load_cases() if c.id == "C04")
    _, pages = extract_text(documents.render(case.document), "application/pdf")
    assert pages == 2
