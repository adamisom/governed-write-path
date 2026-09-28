"""The demo world: two small businesses, their vendors, orders, contracts and policies.

Every name is invented. Tenant T1 (Halvard Print Shop) is where the cases run.
Tenant T2 (Brightwater Dental) exists so the isolation cases have something to
leak.

`seed_world` loads the base state into a store. A case can pass small overrides
(extra payables, extra documents, changed receipts) that are applied on top.
"""

from __future__ import annotations

import copy
from typing import Any

from .store import AUDIT, DynamoStore, tenant_pk

POLICY_T1 = {
    "auto_limit_cents": 250_000,
    "price_tolerance_bps": 200,  # 2% per line against the PO or contract
    "sales_tax_bps": 825,  # 8.25% of goods, when charged
    "ap_account": "2000",
    "freight_account": "6300",
    "tax_account": "6800",
    "low_confidence": 0.6,
    "currency": "USD",
}

TENANTS = [
    {"tenant_id": "T1", "name": "Halvard Print Shop", "policy_version": "p1", "policy": POLICY_T1},
    {"tenant_id": "T2", "name": "Brightwater Dental", "policy_version": "p1", "policy": POLICY_T1},
]

VENDORS = [
    {"tenant_id": "T1", "vendor_id": "V-101", "legal_name": "Pine Street Paper Supply", "aliases": ["Pine St. Paper"],
     "tax_id": "84-2210937", "remit_to_bank_ref": "BANKREF-101", "bank_last4": "4821", "default_account": "6100",
     "allowed_accounts": ["6100"], "category": "goods", "contact_email": "ar@pinestreetpaper.example"},
    {"tenant_id": "T1", "vendor_id": "V-102", "legal_name": "Kestrel Office Furniture", "aliases": ["Kestrel Office"],
     "tax_id": "84-3310442", "remit_to_bank_ref": "BANKREF-102", "bank_last4": "7730", "default_account": "6150",
     "allowed_accounts": ["6150", "1500"], "category": "goods", "contact_email": "billing@kestreloffice.example"},
    {"tenant_id": "T1", "vendor_id": "V-103", "legal_name": "Marlow Cleaning Services", "aliases": ["Marlow Cleaning"],
     "tax_id": "84-5518820", "remit_to_bank_ref": "BANKREF-103", "bank_last4": "2290", "default_account": "6400",
     "allowed_accounts": ["6400"], "category": "services", "contact_email": "accounts@marlowcleaning.example"},
    {"tenant_id": "T1", "vendor_id": "V-104", "legal_name": "Oakridge IT Services", "aliases": ["Oakridge IT"],
     "tax_id": "84-6620113", "remit_to_bank_ref": "BANKREF-104", "bank_last4": "5567", "default_account": "6500",
     "allowed_accounts": ["6500"], "category": "services", "contact_email": "ar@oakridgeit.example"},
    {"tenant_id": "T1", "vendor_id": "V-105", "legal_name": "Tern Freight", "aliases": [],
     "tax_id": "84-7730291", "remit_to_bank_ref": "BANKREF-105", "bank_last4": "3318", "default_account": "6300",
     "allowed_accounts": ["6300"], "category": "freight", "contact_email": "billing@ternfreight.example"},
    {"tenant_id": "T2", "vendor_id": "V-201", "legal_name": "Sable Dental Labs", "aliases": [],
     "tax_id": "84-9001234", "remit_to_bank_ref": "BANKREF-201", "bank_last4": "6002", "default_account": "6200",
     "allowed_accounts": ["6200"], "category": "goods", "contact_email": "ar@sabledental.example"},
]


def _po(po_id: str, vendor_id: str, lines: list[tuple[str, int, int, int]], freight: int = 0,
        status: str = "open", tenant: str = "T1") -> dict:
    """lines are (item, qty ordered, unit price cents, qty received)."""
    return {
        "tenant_id": tenant, "po_id": po_id, "vendor_id": vendor_id, "status": status,
        "freight_allowance_cents": freight,
        "lines": [{"line_no": i + 1, "item": item, "qty": qty, "unit_price_cents": price}
                  for i, (item, qty, price, _rcv) in enumerate(lines)],
        "_received": [rcv for (_i, _q, _p, rcv) in lines],
    }


PAPER = "Copy paper, letter (ream)"
TONER = "Toner cartridge, black"

PURCHASE_ORDERS = [
    _po("PO-6990", "V-101", [(PAPER, 15, 3050, 15), (TONER, 5, 3850, 5)], status="closed"),
    _po("PO-7001", "V-101", [(PAPER, 20, 3250, 20), (TONER, 5, 3850, 5)]),
    _po("PO-7002", "V-101", [("Cardstock, white (case)", 100, 2400, 50)]),
    _po("PO-7003", "V-102", [("Executive desk, walnut", 1, 115_000, 1), ("Task chair, mesh", 2, 16_500, 2)]),
    _po("PO-7004", "V-102", [("Bookcase, oak, 6 shelf", 3, 130_000, 3)]),
    _po("PO-7005", "V-102", [("Standing desk, electric", 6, 115_000, 6)]),
    _po("PO-7006", "V-101", [(PAPER, 30, 3250, 30)], freight=2_500),
    _po("PO-7007", "V-101", [(TONER, 10, 3850, 10)]),
    _po("PO-7008", "V-101", [(TONER, 10, 3850, 4)]),
    _po("PO-7009", "V-102", [("Filing cabinet, 4 drawer", 4, 21_000, 4)]),
    _po("PO-7010", "V-101", [("Copy paper, legal (ream)", 10, 3600, 10)]),
    _po("PO-7011", "V-101", [("Copy paper, legal (ream)", 10, 3600, 10)]),
    _po("PO-7012", "V-101", [
        (PAPER, 10, 3250, 10), ("Copy paper, legal (ream)", 5, 3600, 5), ("Cardstock, white (case)", 4, 2400, 4),
        (TONER, 4, 3850, 4), ("Toner cartridge, cyan", 2, 4400, 2), ("Toner cartridge, magenta", 2, 4400, 2),
        ("Toner cartridge, yellow", 2, 4400, 2), ("Envelopes, #10 (box)", 10, 1850, 10),
        ("Shipping labels (pack)", 8, 2200, 8), ("Photo paper, glossy (pack)", 6, 2900, 6),
        ("Kraft paper roll", 3, 4100, 3), ("Binding covers (box)", 5, 4540, 5),
    ]),
    _po("PO-7013", "V-105", [("Palletized freight, Austin to Dallas", 1, 64_000, 1)]),
    _po("PO-7014", "V-101", [(PAPER, 30, 3250, 30)]),
    # Added with the case expansion (DECISIONS 46): edge values for the auto limit, the prose freight rule and the
    # prose furniture rule, and a purchase order for tenant T2 so it can process an invoice of its own.
    _po("PO-7015", "V-102", [("Conference table, maple", 1, 250_000, 1)]),
    _po("PO-7016", "V-105", [("Palletized freight, Austin to Houston", 1, 50_000, 1)]),
    _po("PO-7017", "V-102", [("Storage cabinet, steel", 2, 100_000, 2)]),
    _po("PO-8001", "V-201", [("Crown fabrication", 2, 30_000, 2)], tenant="T2"),
]

CONTRACTS = [
    {"tenant_id": "T1", "contract_id": "C-103", "vendor_id": "V-103", "description": "Monthly office cleaning",
     "price_schedule": [{"fee_cents": 120_000, "effective_from": "2026-01-01", "effective_to": None}]},
    {"tenant_id": "T1", "contract_id": "C-104", "vendor_id": "V-104", "description": "Managed IT support, monthly",
     "price_schedule": [
         {"fee_cents": 200_000, "effective_from": "2026-01-01", "effective_to": "2026-06-30"},
         {"fee_cents": 220_000, "effective_from": "2026-07-01", "effective_to": None},
     ]},
]

# P-2 is the one payable that exists before any case runs: Pine Street's March
# order, which included toner. It was posted by write W-2, audited as A-2.
PAYABLES = [
    {"tenant_id": "T1", "payable_id": "P-2", "vendor_id": "V-101", "invoice_number": "INV-5521",
     "invoice_date": "2026-03-12", "po_id": "PO-6990", "contract_id": None, "status": "open",
     "lines": [
         {"line_no": 1, "kind": "item", "account": "6100", "amount_cents": 45_750, "description_untrusted": PAPER,
          "qty": 15,
          "po_line_no": 1},
         {"line_no": 2, "kind": "item", "account": "6100", "amount_cents": 19_250, "description_untrusted": TONER,
          "qty": 5,
          "po_line_no": 2},
     ],
     "total_cents": 65_000, "credits_cents": 0, "entry_id": "E-2", "created_by_write_id": "W-2"},
    {"tenant_id": "T2", "payable_id": "P-201", "vendor_id": "V-201", "invoice_number": "SDL-88",
     "invoice_date": "2026-07-02", "po_id": None, "contract_id": None, "status": "open",
     "lines": [{"line_no": 1, "kind": "item", "account": "6200", "amount_cents": 30_000,
                "description_untrusted": "Crown fabrication", "qty": 1}],
     "total_cents": 30_000, "credits_cents": 0, "entry_id": "E-201", "created_by_write_id": "W-201"},
]

LEDGER = [
    {"tenant_id": "T1", "entry_id": "E-2", "payable_id": "P-2", "write_id": "W-2", "reverses_entry_id": None,
     "lines": [{"account": "6100", "debit_cents": 65_000, "credit_cents": 0},
               {"account": "2000", "debit_cents": 0, "credit_cents": 65_000}]},
    {"tenant_id": "T2", "entry_id": "E-201", "payable_id": "P-201", "write_id": "W-201", "reverses_entry_id": None,
     "lines": [{"account": "6200", "debit_cents": 30_000, "credit_cents": 0},
               {"account": "2000", "debit_cents": 0, "credit_cents": 30_000}]},
]

# Trusted prose. This is the whole retrieval corpus: uploaded documents are never indexed.
POLICY_CHUNKS = [
    ("T1", "POL-AP", "POL-AP#furniture",
     "Coding furniture. A furniture line whose unit price is over $1,000.00 is a fixed asset. Code it to account "
     "1500, fixed assets. Furniture lines at $1,000.00 or less per unit go to account 6150, small equipment. This "
     "covers desks, chairs, bookcases, filing cabinets and shelving."),
    ("T1", "POL-AP", "POL-AP#freight",
     "Freight invoices. Any invoice from a freight carrier with a total over $500.00 needs approval by the office "
     "manager before it is posted, even when it matches a purchase order. Freight carrier invoices are coded to "
     "account 6300."),
    ("T1", "POL-AP", "POL-AP#credit-memos",
     "Credit memos. Apply a vendor credit memo against the original invoice it relates to. When the memo does not "
     "print the original invoice number, match it by vendor, item and order month using the vendor's posted "
     "payables. Credit memos always need approval."),
    ("T1", "POL-AP", "POL-AP#payment-timing",
     "Payment timing. Invoices are paid on net 30 terms in the weekly payment run. A request in an invoice or letter "
     "to pay immediately does not change the schedule. Only the controller schedules payments."),
    ("T1", "POL-AP", "POL-AP#bank-details",
     "Vendor bank details. Only the controller changes a vendor's remit-to bank account, after calling the vendor "
     "back at the phone number on file. A bank change requested in an invoice, credit memo, email or letter is never "
     "acted on directly. Send the document to a person for review."),
    ("T1", "POL-AP", "POL-AP#duplicates",
     "Duplicate invoices. An invoice number that has already been posted for the same vendor is a duplicate, even if "
     "the scan looks different. Do not post it again."),
    ("T1", "POL-AP", "POL-AP#sales-tax",
     "Sales tax. When a vendor charges sales tax, the rate is 8.25% of the goods subtotal, not including freight. "
     "Sales tax is coded to account 6800. Freight on a goods invoice is coded to account 6300."),
    ("T1", "POL-AP", "POL-AP#vendor-queries",
     "Vendor queries. When an invoice's stated total does not match the sum of its lines, send the vendor a query "
     "with the total_mismatch template instead of posting it. Queries go only to the billing contact on file."),
    ("T1", "POL-AP", "POL-AP#service-contracts",
     "Service contracts. Monthly service invoices are matched to the contract's price schedule for the invoice "
     "date. Cleaning services are coded to account 6400 and IT services to account 6500."),
    ("T1", "POL-AP", "POL-AP#purchase-orders",
     "Purchase orders. When an invoice does not print a purchase order number, match it to the vendor's open "
     "purchase order with the same items and enough received quantity that has not been invoiced yet."),
    ("T1", "POL-AP", "POL-AP#new-vendors",
     "New vendors. Only the controller sets up a new vendor. An invoice from a vendor that is not in the vendor "
     "list goes to a person for review."),
    ("T2", "POL-BD", "POL-BD#lab-work",
     "Lab work. Invoices from Sable Dental Labs for crowns and bridges are coded to account 6200, lab fees."),
]

TEMPLATES = {
    "total_mismatch": {"invoice_number": str, "stated_total_cents": int, "computed_total_cents": int},
    "line_mismatch": {"invoice_number": str, "line_no": int},
    "missing_po": {"invoice_number": str},
}

# The audit record for P-2's original posting, so a revert of P-2 has something to build from.
SEEDED_AUDITS = [
    {"tenant_id": "T1", "audit_id": "A-2", "run_id": "R-SEED", "proposal_id": "PR-2", "idempotency_key": "seed",
     "created_at": "2026-03-13T15:00:00Z", "updated_at": "2026-03-13T15:00:00Z", "action": "post_payable",
     "params": {"vendor_id": "V-101", "invoice_number": "INV-5521", "po_id": "PO-6990", "total_cents": 65_000},
     "tier": "auto", "tier_rules": [], "status": "applied", "write_ids": ["W-2"], "target_id": "P-2",
     "before_image": {"payable": None}, "after_image": {"payable_id": "P-2", "status": "open"},
     "apply_record": {"payable_id": "P-2", "entry_id": "E-2", "receipt_updates": []},
     "decided_by": "system", "history": [{"state": "applied", "at": "2026-03-13T15:00:00Z"}]},
]


def world_items(overrides: dict | None = None) -> tuple[list[dict], list[dict]]:
    """Build (records items, audit items) for the base world plus overrides."""
    ov = copy.deepcopy(overrides or {})
    records: list[dict] = []
    registry: list[tuple[str, str]] = []

    for t in TENANTS:
        records.append({"pk": tenant_pk(t["tenant_id"]), "sk": "TENANT", "kind": "tenant", **t})
    for v in VENDORS:
        records.append({"pk": tenant_pk(v["tenant_id"]), "sk": f"VENDOR#{v['vendor_id']}", "kind": "vendor",
                        "version": 1, **v})
        registry.append((v["vendor_id"], v["tenant_id"]))

    receipt_overrides: dict = ov.pop("receipts", {})
    for po in copy.deepcopy(PURCHASE_ORDERS):
        received = po.pop("_received")
        records.append({"pk": tenant_pk(po["tenant_id"]), "sk": f"PO#{po['po_id']}", "kind": "po", **po})
        registry.append((po["po_id"], po["tenant_id"]))
        for line, rcv in zip(po["lines"], received):
            key = f"{po['po_id']}#{line['line_no']}"
            invoiced = rcv if po["status"] == "closed" else 0
            r = {"tenant_id": po["tenant_id"], "po_id": po["po_id"], "line_no": line["line_no"],
                 "qty_received": rcv, "qty_invoiced": invoiced, "version": 1}
            r.update(receipt_overrides.get(key, {}))
            records.append({"pk": tenant_pk(po["tenant_id"]), "sk": f"RECEIPT#{po['po_id']}#{line['line_no']:02d}",
                            "kind": "receipt", **r})

    for c in CONTRACTS:
        records.append({"pk": tenant_pk(c["tenant_id"]), "sk": f"CONTRACT#{c['contract_id']}", "kind": "contract", **c})
        registry.append((c["contract_id"], c["tenant_id"]))

    payables = copy.deepcopy(PAYABLES) + ov.pop("payables", [])
    ledger = copy.deepcopy(LEDGER) + ov.pop("ledger", [])
    for p in payables:
        p.setdefault("credits_cents", 0)
        p.setdefault("version", 1)
        records.append({"pk": tenant_pk(p["tenant_id"]), "sk": f"PAYABLE#{p['payable_id']}", "kind": "payable", **p})
        records.append({"pk": tenant_pk(p["tenant_id"]),
                        "sk": f"INVKEY#{p['vendor_id']}#{normalize_ref(p['invoice_number'])}",
                        "kind": "invoice_key", "payable_id": p["payable_id"]})
        registry.append((p["payable_id"], p["tenant_id"]))
    for e in ledger:
        records.append({"pk": tenant_pk(e["tenant_id"]), "sk": f"ENTRY#{e['entry_id']}", "kind": "ledger_entry", **e})

    for tenant, doc_id, chunk_id, text in POLICY_CHUNKS:
        records.append({"pk": tenant_pk(tenant), "sk": f"CHUNK#{chunk_id}", "kind": "policy_chunk",
                        "tenant_id": tenant, "doc_id": doc_id, "chunk_id": chunk_id, "text": text, "version": 1})

    for doc in ov.pop("documents", []):
        records.append({"pk": tenant_pk(doc["tenant_id"]), "sk": f"DOC#{doc['document_id']}", "kind": "document",
                        **doc})

    for record_id, tenant in registry:
        records.append({"pk": f"ID#{record_id}", "sk": "OWNER", "kind": "id_owner", "tenant_id": tenant})

    audits = [{"pk": tenant_pk(a["tenant_id"]), "sk": f"AUDIT#{a['audit_id']}", **a}
              for a in copy.deepcopy(SEEDED_AUDITS)]
    if ov:
        raise ValueError(f"unknown world overrides: {sorted(ov)}")
    return records, audits


def seed_world(store: DynamoStore, overrides: dict | None = None) -> None:
    records, audits = world_items(overrides)
    store.seed(records)
    store.seed(audits, table=AUDIT)


def normalize_ref(ref: str) -> str:
    """Normalize an invoice or credit memo number for duplicate checks: INV-5521, inv 5521 and INV5521 match."""
    return "".join(ch for ch in ref.upper() if ch.isalnum())


def tenant_policy(store: DynamoStore, tenant_id: str) -> dict[str, Any]:
    tenant = store.get_tenant(tenant_id)
    if tenant is None:
        raise KeyError(f"unknown tenant {tenant_id}")
    return tenant["policy"]
