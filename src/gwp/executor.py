"""The executor: the only module that changes the ledger, the vendor records or the outbox.

Each apply or revert is one DynamoDB TransactWriteItems call that holds:

- the domain changes (payable, ledger entry, receipts, credit, outbox message),
- an idempotency key item, inserted only if it does not exist yet,
- the audit record's status change, guarded by its current status.

Either all of it commits or none of it does, so a change is never visible
without its audit record saying `applied`, and a redelivered request finds its
key and changes nothing.

Reverts are compensating writes built from the audit record's `apply_record`.
Nothing is deleted: a reverted payable keeps its row with status `reversed`, and
the ledger gets a reversing entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .runtime import Clock, Ids, sha256_hex
from .store import AUDIT, RECORDS, DynamoStore, TransactionConflict, audit_transition_expr, op_put, op_update, tenant_pk
from .world import normalize_ref


class SimulatedCrash(Exception):
    """Raised by the fault hook after a commit, before the executor acknowledges it."""


@dataclass
class ExecResult:
    status: str  # applied | already_applied | failed | conflict | reverted | already_reverted | refused
    write_ids: list[str] = field(default_factory=list)
    error: str | None = None
    refusal_reason: str | None = None


def execution_key(tenant_id: str, proposal_id: str, action: str) -> str:
    return sha256_hex(tenant_id, proposal_id, action)


def revert_key(write_id: str) -> str:
    return sha256_hex(write_id, "revert")


def _audit_op(store: DynamoStore, tenant_id: str, audit_id: str, froms: list[str], to: str, at: str,
              fields: dict, terminal: bool) -> dict:
    (update, cond), names, values = audit_transition_expr(froms, to, at, fields, terminal)
    return op_update(store.t(AUDIT), tenant_pk(tenant_id), f"AUDIT#{audit_id}", update, cond, names, values)


def _swap(lines: list[dict]) -> list[dict]:
    return [{"account": ln["account"], "debit_cents": ln["credit_cents"], "credit_cents": ln["debit_cents"]}
            for ln in lines]


class Executor:
    def __init__(self, store: DynamoStore, clock: Clock, ids: Ids,
                 after_commit: Callable[[str], None] | None = None):
        self.store = store
        self.clock = clock
        self.ids = ids
        # Fault hook for tests and case D04: called after a commit succeeds.
        self.after_commit = after_commit

    # -- apply ---------------------------------------------------------------

    def apply(self, tenant_id: str, audit_id: str) -> ExecResult:
        s = self.store
        audit = s.get_audit(tenant_id, audit_id)
        if audit is None:
            return ExecResult("failed", error="no_such_audit")
        if audit["status"] == "applied":
            return ExecResult("already_applied", audit.get("write_ids", []))
        if audit["status"] not in ("proposed", "approved"):
            return ExecResult("conflict", error=f"audit status is {audit['status']}")
        if audit["tier"] == "approval" and audit["status"] != "approved":
            return ExecResult("conflict", error="approval tier needs an approved decision")

        builder = {
            "post_payable": self._plan_post_payable,
            "apply_credit_memo": self._plan_credit_memo,
            "recode_line": self._plan_recode,
            "send_vendor_query": self._plan_vendor_query,
            "hold_invoice": self._plan_hold,
        }.get(audit["action"])
        if builder is None:
            return ExecResult("failed", error=f"no executor for {audit['action']}")

        write_id = self.ids.new("W")
        ops, before, after, apply_record = builder(tenant_id, audit, write_id)
        xkey = execution_key(tenant_id, audit["proposal_id"], audit["action"])
        at = self.clock.now()
        ops.append(op_put(s.t(RECORDS), {"pk": tenant_pk(tenant_id), "sk": f"XKEY#{xkey}", "kind": "exec_key",
                                         "audit_id": audit_id, "write_id": write_id, "at": at},
                          "attribute_not_exists(pk)"))
        xkey_index = len(ops) - 1
        ops.append(_audit_op(s, tenant_id, audit_id, ["proposed", "approved"], "applied", at, {
            "write_ids": [write_id], "before_image": before, "after_image": after, "apply_record": apply_record,
            "applied_at": at,
        }, terminal=True))
        audit_index = len(ops) - 1
        try:
            s.transact_domain(ops)
        except TransactionConflict as e:
            reasons = e.reasons
            if len(reasons) > xkey_index and reasons[xkey_index] == "ConditionalCheckFailed":
                current = s.get_audit(tenant_id, audit_id) or {}
                return ExecResult("already_applied", current.get("write_ids", []))
            if len(reasons) > audit_index and reasons[audit_index] == "ConditionalCheckFailed":
                current = s.get_audit(tenant_id, audit_id) or {}
                if current.get("status") == "applied":
                    return ExecResult("already_applied", current.get("write_ids", []))
                return ExecResult("conflict", error=f"audit status is {current.get('status')}")
            failed = [i for i, r in enumerate(reasons) if r == "ConditionalCheckFailed"]
            error = "precondition_failed:" + ",".join(str(i) for i in failed)
            s.transition_audit(tenant_id, audit_id, ["proposed", "approved"], "failed", self.clock.now(),
                               {"error": error}, terminal=True)
            return ExecResult("failed", error=error)
        if self.after_commit:
            self.after_commit(audit_id)
        return ExecResult("applied", [write_id])

    def _plan_post_payable(self, tenant_id: str, audit: dict, write_id: str):
        s = self.store
        prm = audit["params"]
        lines = audit["apply_input"]["lines"]
        pk = tenant_pk(tenant_id)
        payable_id = self.ids.new("P")
        entry_id = self.ids.new("E")
        policy = (s.get_tenant(tenant_id) or {})["policy"]
        payable = {
            "tenant_id": tenant_id, "payable_id": payable_id, "vendor_id": prm["vendor_id"],
            "invoice_number": prm["invoice_number"], "invoice_date": prm["invoice_date"],
            "po_id": prm.get("po_id"), "contract_id": prm.get("contract_id"), "status": "open",
            "lines": lines, "total_cents": prm["total_cents"], "credits_cents": 0, "entry_id": entry_id,
            "created_by_write_id": write_id, "document_id": audit["document_id"], "version": 1,
        }
        entry = {
            "tenant_id": tenant_id, "entry_id": entry_id, "payable_id": payable_id, "write_id": write_id,
            "reverses_entry_id": None,
            "lines": [{"account": ln["account"], "debit_cents": ln["amount_cents"], "credit_cents": 0} for ln in lines]
            + [{"account": policy["ap_account"], "debit_cents": 0, "credit_cents": prm["total_cents"]}],
        }
        ops = [
            op_put(s.t(RECORDS), {"pk": pk, "sk": f"PAYABLE#{payable_id}", "kind": "payable", **payable},
                   "attribute_not_exists(pk)"),
            op_put(s.t(RECORDS), {"pk": pk, "sk": f"INVKEY#{prm['vendor_id']}#{normalize_ref(prm['invoice_number'])}",
                                  "kind": "invoice_key", "payable_id": payable_id}, "attribute_not_exists(pk)"),
            op_put(s.t(RECORDS), {"pk": pk, "sk": f"ENTRY#{entry_id}", "kind": "ledger_entry", **entry},
                   "attribute_not_exists(pk)"),
            op_put(s.t(RECORDS), {"pk": f"ID#{payable_id}", "sk": "OWNER", "kind": "id_owner",
                                  "tenant_id": tenant_id}, "attribute_not_exists(pk)"),
        ]
        receipt_before = []
        receipt_updates = []
        if prm.get("po_id"):
            receipts = {r["line_no"]: r for r in s.get_receipts(tenant_id, prm["po_id"])}
            wanted: dict[int, int] = {}
            for ln in lines:
                if ln.get("po_line_no"):
                    wanted[ln["po_line_no"]] = wanted.get(ln["po_line_no"], 0) + ln.get("qty", 0)
            for line_no, qty in sorted(wanted.items()):
                r = receipts[line_no]
                receipt_before.append({"po_id": prm["po_id"], "line_no": line_no, "qty_invoiced": r["qty_invoiced"],
                                       "version": r["version"]})
                receipt_updates.append({"po_id": prm["po_id"], "line_no": line_no, "qty": qty})
                ops.append(op_update(
                    s.t(RECORDS), pk, f"RECEIPT#{prm['po_id']}#{line_no:02d}",
                    "SET qty_invoiced = qty_invoiced + :q, version = version + :one",
                    "version = :v", values={":q": qty, ":one": 1, ":v": r["version"]}))
        before = {"payable": None, "receipts": receipt_before}
        after = {"payable_id": payable_id, "status": "open", "total_cents": prm["total_cents"], "entry_id": entry_id,
                 "receipts": [{"po_id": u["po_id"], "line_no": u["line_no"],
                               "qty_invoiced": b["qty_invoiced"] + u["qty"]}
                              for u, b in zip(receipt_updates, receipt_before)]}
        record = {"payable_id": payable_id, "entry_id": entry_id, "receipt_updates": receipt_updates}
        return ops, before, after, record

    def _plan_credit_memo(self, tenant_id: str, audit: dict, write_id: str):
        s = self.store
        prm = audit["params"]
        pk = tenant_pk(tenant_id)
        payable = s.get_payable(tenant_id, prm["payable_id"])
        assert payable is not None
        policy = (s.get_tenant(tenant_id) or {})["policy"]
        entry_id = self.ids.new("E")
        expense_account = next(ln["account"] for ln in payable["lines"] if ln.get("kind", "item") == "item")
        amount = prm["amount_cents"]
        entry = {"tenant_id": tenant_id, "entry_id": entry_id, "payable_id": payable["payable_id"],
                 "write_id": write_id, "reverses_entry_id": None,
                 "lines": [{"account": policy["ap_account"], "debit_cents": amount, "credit_cents": 0},
                           {"account": expense_account, "debit_cents": 0, "credit_cents": amount}]}
        ops = [
            op_put(s.t(RECORDS), {"pk": pk, "sk": f"CREDITKEY#{payable['vendor_id']}#{normalize_ref(prm['credit_number'])}",
                                  "kind": "credit_key", "payable_id": payable["payable_id"], "write_id": write_id,
                                  "amount_cents": amount, "status": "applied"}, "attribute_not_exists(pk)"),
            op_update(s.t(RECORDS), pk, f"PAYABLE#{payable['payable_id']}",
                      "SET credits_cents = credits_cents + :a, version = version + :one ADD dependents :w",
                      "#st = :open AND version = :v", names={"#st": "status"},
                      values={":a": amount, ":one": 1, ":w": {write_id}, ":open": "open", ":v": payable["version"]}),
            op_put(s.t(RECORDS), {"pk": pk, "sk": f"ENTRY#{entry_id}", "kind": "ledger_entry", **entry},
                   "attribute_not_exists(pk)"),
        ]
        before = {"payable_id": payable["payable_id"], "credits_cents": payable["credits_cents"]}
        after = {"payable_id": payable["payable_id"], "credits_cents": payable["credits_cents"] + amount}
        record = {"payable_id": payable["payable_id"], "entry_id": entry_id, "amount_cents": amount,
                  "credit_sk": f"CREDITKEY#{payable['vendor_id']}#{normalize_ref(prm['credit_number'])}"}
        return ops, before, after, record

    def _plan_recode(self, tenant_id: str, audit: dict, write_id: str):
        s = self.store
        prm = audit["params"]
        pk = tenant_pk(tenant_id)
        payable = s.get_payable(tenant_id, prm["payable_id"])
        assert payable is not None
        idx = next(i for i, ln in enumerate(payable["lines"]) if ln["line_no"] == prm["line_no"])
        line = payable["lines"][idx]
        entry_id = self.ids.new("E")
        entry = {"tenant_id": tenant_id, "entry_id": entry_id, "payable_id": payable["payable_id"],
                 "write_id": write_id, "reverses_entry_id": None,
                 "lines": [{"account": prm["account"], "debit_cents": line["amount_cents"], "credit_cents": 0},
                           {"account": line["account"], "debit_cents": 0, "credit_cents": line["amount_cents"]}]}
        ops = [
            op_update(s.t(RECORDS), pk, f"PAYABLE#{payable['payable_id']}",
                      f"SET #ln[{idx}].account = :acct, version = version + :one ADD dependents :w",
                      "#st = :open AND version = :v", names={"#ln": "lines", "#st": "status"},
                      values={":acct": prm["account"], ":one": 1, ":w": {write_id}, ":open": "open",
                              ":v": payable["version"]}),
            op_put(s.t(RECORDS), {"pk": pk, "sk": f"ENTRY#{entry_id}", "kind": "ledger_entry", **entry},
                   "attribute_not_exists(pk)"),
        ]
        before = {"payable_id": payable["payable_id"], "line_no": prm["line_no"], "account": line["account"]}
        after = {"payable_id": payable["payable_id"], "line_no": prm["line_no"], "account": prm["account"]}
        record = {"payable_id": payable["payable_id"], "line_index": idx, "entry_id": entry_id,
                  "old_account": line["account"], "new_account": prm["account"], "amount_cents": line["amount_cents"]}
        return ops, before, after, record

    def _plan_vendor_query(self, tenant_id: str, audit: dict, write_id: str):
        s = self.store
        prm = audit["params"]
        vendor = s.get_vendor(tenant_id, prm["vendor_id"])
        assert vendor is not None
        message_id = self.ids.new("M")
        msg = {"tenant_id": tenant_id, "message_id": message_id, "vendor_id": prm["vendor_id"],
               "to": vendor["contact_email"],  # always the contact on file, never a model-chosen address
               "template_id": prm["template_id"], "fields": prm.get("fields", {}), "status": "queued",
               "write_id": write_id}
        ops = [op_put(s.t(RECORDS), {"pk": tenant_pk(tenant_id), "sk": f"OUTBOX#{message_id}", "kind": "outbox", **msg},
                      "attribute_not_exists(pk)")]
        return ops, {"message": None}, {"message_id": message_id, "status": "queued"}, {"message_id": message_id}

    def _plan_hold(self, tenant_id: str, audit: dict, write_id: str):
        s = self.store
        prm = audit["params"]
        ops = [op_put(s.t(RECORDS), {"pk": tenant_pk(tenant_id), "sk": f"HOLD#{prm['document_id']}", "kind": "hold",
                                     "document_id": prm["document_id"], "reason_code": prm["reason_code"],
                                     "status": "on_hold", "write_id": write_id}, "attribute_not_exists(pk)")]
        return ops, {"hold": None}, {"document_id": prm["document_id"], "status": "on_hold"}, \
            {"hold_sk": f"HOLD#{prm['document_id']}"}

    # -- revert ----------------------------------------------------------------

    def revert(self, tenant_id: str, audit_id: str, requested_by: str) -> ExecResult:
        """Apply the compensating write for an applied audit record, or refuse and record why."""
        s = self.store
        audit = s.get_audit(tenant_id, audit_id)
        if audit is None:
            return ExecResult("refused", refusal_reason="no_such_audit")
        if audit["status"] == "reverted":
            return ExecResult("already_reverted", [audit.get("reversing_write_id", "")])
        if audit["status"] != "applied":
            return self._refuse(tenant_id, audit_id, requested_by, "not_applied")
        rec = audit.get("apply_record") or {}
        write_id = audit["write_ids"][0]
        pk = tenant_pk(tenant_id)
        rev_write = self.ids.new("W")
        ops: list[dict] = []
        action = audit["action"]

        if action == "post_payable":
            payable = s.get_payable(tenant_id, rec["payable_id"])
            if payable is None or payable["status"] != "open":
                return self._refuse(tenant_id, audit_id, requested_by, "not_open")
            if payable.get("dependents"):
                return self._refuse(tenant_id, audit_id, requested_by, "dependent_write")
            ops += self._reversing_entry(tenant_id, rec["entry_id"], rec["payable_id"], rev_write)
            ops.append(op_update(s.t(RECORDS), pk, f"PAYABLE#{rec['payable_id']}",
                                 "SET #st = :rev, version = version + :one, reversed_by_write_id = :w",
                                 "#st = :open AND version = :v AND attribute_not_exists(dependents)",
                                 names={"#st": "status"},
                                 values={":rev": "reversed", ":open": "open", ":one": 1, ":v": payable["version"],
                                         ":w": rev_write}))
            for u in rec.get("receipt_updates", []):
                r = next(x for x in s.get_receipts(tenant_id, u["po_id"]) if x["line_no"] == u["line_no"])
                ops.append(op_update(s.t(RECORDS), pk, f"RECEIPT#{u['po_id']}#{u['line_no']:02d}",
                                     "SET qty_invoiced = qty_invoiced - :q, version = version + :one",
                                     "version = :v", values={":q": u["qty"], ":one": 1, ":v": r["version"]}))
        elif action == "apply_credit_memo":
            payable = s.get_payable(tenant_id, rec["payable_id"])
            assert payable is not None
            ops += self._reversing_entry(tenant_id, rec["entry_id"], rec["payable_id"], rev_write)
            ops.append(op_update(s.t(RECORDS), pk, f"PAYABLE#{rec['payable_id']}",
                                 "SET credits_cents = credits_cents - :a, version = version + :one DELETE dependents :w",
                                 "version = :v", values={":a": rec["amount_cents"], ":one": 1, ":w": {write_id},
                                                         ":v": payable["version"]}))
            ops.append(op_update(s.t(RECORDS), pk, rec["credit_sk"], "SET #st = :rev", "#st = :applied",
                                 names={"#st": "status"}, values={":rev": "reversed", ":applied": "applied"}))
        elif action == "recode_line":
            payable = s.get_payable(tenant_id, rec["payable_id"])
            assert payable is not None
            ops += self._reversing_entry(tenant_id, rec["entry_id"], rec["payable_id"], rev_write)
            ops.append(op_update(s.t(RECORDS), pk, f"PAYABLE#{rec['payable_id']}",
                                 f"SET #ln[{rec['line_index']}].account = :old, version = version + :one "
                                 "DELETE dependents :w", "version = :v", names={"#ln": "lines"},
                                 values={":old": rec["old_account"], ":one": 1, ":w": {write_id},
                                         ":v": payable["version"]}))
        elif action == "send_vendor_query":
            msg = s.get_outbox(tenant_id, rec["message_id"])
            if msg is None or msg["status"] != "queued":
                # A sent message can't be unsent. Refuse, and never send a "please ignore" follow-up,
                # since a revert must not create a side effect the original write didn't have.
                return self._refuse(tenant_id, audit_id, requested_by, "not_revertible")
            ops.append(op_update(s.t(RECORDS), pk, f"OUTBOX#{rec['message_id']}", "SET #st = :c", "#st = :q",
                                 names={"#st": "status"}, values={":c": "cancelled", ":q": "queued"}))
        elif action == "hold_invoice":
            ops.append(op_update(s.t(RECORDS), pk, rec["hold_sk"], "SET #st = :r", "#st = :h",
                                 names={"#st": "status"}, values={":r": "released", ":h": "on_hold"}))
        else:
            return self._refuse(tenant_id, audit_id, requested_by, "not_revertible")

        rkey = revert_key(write_id)
        at = self.clock.now()
        ops.append(op_put(s.t(RECORDS), {"pk": pk, "sk": f"RKEY#{rkey}", "kind": "revert_key", "audit_id": audit_id,
                                         "write_id": rev_write, "at": at}, "attribute_not_exists(pk)"))
        rkey_index = len(ops) - 1
        ops.append(_audit_op(s, tenant_id, audit_id, ["applied"], "reverted", at, {
            "revert_requested_by": requested_by, "revert_at": at, "reversing_write_id": rev_write,
        }, terminal=True))
        try:
            s.transact_domain(ops)
        except TransactionConflict as e:
            current = s.get_audit(tenant_id, audit_id) or {}
            if (len(e.reasons) > rkey_index and e.reasons[rkey_index] == "ConditionalCheckFailed") \
                    or current.get("status") == "reverted":
                return ExecResult("already_reverted", [current.get("reversing_write_id", "")])
            return self._refuse(tenant_id, audit_id, requested_by, "conflict")
        return ExecResult("reverted", [rev_write])

    def _reversing_entry(self, tenant_id: str, entry_id: str, payable_id: str, rev_write: str) -> list[dict]:
        s = self.store
        original = s.get_key(tenant_id, f"ENTRY#{entry_id}")
        assert original is not None
        rev_id = self.ids.new("E")
        entry = {"tenant_id": tenant_id, "entry_id": rev_id, "payable_id": payable_id, "write_id": rev_write,
                 "reverses_entry_id": entry_id, "lines": _swap(original["lines"])}
        return [op_put(s.t(RECORDS), {"pk": tenant_pk(tenant_id), "sk": f"ENTRY#{rev_id}", "kind": "ledger_entry",
                                      **entry}, "attribute_not_exists(pk)")]

    def _refuse(self, tenant_id: str, audit_id: str, requested_by: str, reason: str) -> ExecResult:
        at = self.clock.now()
        if self.store.get_audit(tenant_id, audit_id) is not None:
            self.store.append_audit_note(tenant_id, audit_id, at, f"revert_refused:{reason}", {
                "revert_requested_by": requested_by, "revert_at": at, "refusal_reason": reason})
        return ExecResult("refused", refusal_reason=reason)

    # -- the fake mailer -----------------------------------------------------------

    def deliver_outbox(self, tenant_id: str) -> list[str]:
        """Mark queued messages sent. Stands in for a mailer; each send is its own guarded update."""
        sent = []
        for msg in self.store.list_outbox(tenant_id):
            if msg["status"] != "queued":
                continue
            try:
                self.store.transact_domain([op_update(
                    self.store.t(RECORDS), tenant_pk(tenant_id), f"OUTBOX#{msg['message_id']}",
                    "SET #st = :s, sent_at = :at", "#st = :q", names={"#st": "status"},
                    values={":s": "sent", ":q": "queued", ":at": self.clock.now()})])
                sent.append(msg["message_id"])
            except TransactionConflict:
                continue
        return sent
