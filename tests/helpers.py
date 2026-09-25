"""Small builders shared by the tests."""

from gwp.agents.scripted import ScriptedModel
from gwp.agents.strands_agents import StrandsProposer, StrandsReader
from gwp.blobs import MemoryBlobs
from gwp.evals import documents
from gwp.executor import Executor
from gwp.orchestrator import Orchestrator
from gwp.runtime import FakeClock, Ids
from gwp.schema import Principal, Role

UPLOADER = Principal(principal_id="user:up", role=Role.uploader)
APPROVER = Principal(principal_id="user:ap", role=Role.approver)

C01_DOC = {
    "kind": "invoice", "vendor_name": "Pine Street Paper Supply", "vendor_tax_id": "84-2210937",
    "remit_to_bank_last4": "4821", "invoice_number": "INV-6001", "invoice_date": "2026-08-03",
    "po_number": "PO-7001",
    "lines": [{"description": "Copy paper, letter (ream)", "qty": 20, "unit_price_cents": 3250},
              {"description": "Toner cartridge, black", "qty": 5, "unit_price_cents": 3850}],
}


def c01_post(**over):
    params = {"vendor_id": "V-101", "invoice_number": "INV-6001", "invoice_date": "2026-08-03", "po_id": "PO-7001",
              "total_cents": 84250,
              "lines": [{"source_line": 1, "po_line_no": 1, "account": "6100", "amount_cents": 65000},
                        {"source_line": 2, "po_line_no": 2, "account": "6100", "amount_cents": 19250}]}
    params.update(over.pop("params", {}))
    return {"action": "post_payable", "params": params, "rationale": "ok", "confidence": 0.9, **over}


def build(store, reader_turns, proposer_turns, clock=None, executor=None):
    clock = clock or FakeClock()
    ids = Ids()
    rm = ScriptedModel(reader_turns, "claude-haiku-4-5")
    pm = ScriptedModel(proposer_turns, "claude-sonnet-5")
    ex = executor or Executor(store, clock, ids)
    orch = Orchestrator(store, MemoryBlobs(), StrandsReader(rm, "claude-haiku-4-5", synthetic=True),
                        StrandsProposer(pm, "claude-sonnet-5", synthetic=True), clock, ids, ex)
    return orch, rm, pm


def run_doc(store, doc, proposals, reader_over=None, **kw):
    ext = documents.faithful_extraction(doc)
    ext.update(reader_over or {})
    orch, rm, pm = build(store, [{"tool": "Extraction", "input": ext}],
                         [{"tool": "propose_write", "input": {"proposals": proposals}}], **kw)
    up = orch.upload("T1", documents.render(doc), "application/pdf", UPLOADER)
    res = orch.process("T1", up.run_id)
    return orch, res, rm, pm
