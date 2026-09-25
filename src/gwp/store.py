"""The system of record, on DynamoDB, through boto3.

Two tables:

- records: the tenant's vendors, purchase orders, receipts, contracts, policy
  chunks, documents, runs, payables, ledger entries, outbox messages, human
  tasks, and the idempotency key items. Partition key `pk`, sort key `sk`.
- audit: one audit record per proposed write. It has a sparse index on
  `open_flag` and `open_since`, so the staleness check can list records that are
  still in a non-terminal state without scanning.

Every item for a tenant lives under `pk = TENANT#<id>`, so a lookup cannot reach
another tenant's data without naming that tenant. The one exception is the id
registry (`pk = ID#<id>`), which maps a record id to its owner so the policy can
tell a cross-tenant reference apart from a typo. It returns only the owner's id.

Writes come in two groups:

- Bookkeeping writes (documents, runs, audit records, human tasks) are methods
  on this class and are called by the orchestrator.
- Domain writes (payables, ledger entries, receipts, vendors, the outbox) go
  only through `transact_domain`, which runs one DynamoDB TransactWriteItems
  call. Only `gwp.executor` calls it, and a test checks that.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

RECORDS = "records"
AUDIT = "audit"

_ser = TypeSerializer()
_de = TypeDeserializer()


def tenant_pk(tenant_id: str) -> str:
    return f"TENANT#{tenant_id}"


def _to_dynamo(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_to_dynamo(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return set(value)
    return value


def _from_dynamo(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_dynamo(v) for v in value]
    if isinstance(value, set):
        return {_from_dynamo(v) for v in value}
    return value


def serialize_item(item: dict) -> dict:
    return {k: _ser.serialize(v) for k, v in _to_dynamo(item).items()}


def serialize_values(values: dict) -> dict:
    return {k: _ser.serialize(_to_dynamo(v)) for k, v in values.items()}


def deserialize_item(raw: dict) -> dict:
    return _from_dynamo({k: _de.deserialize(v) for k, v in raw.items()})


class TransactionConflict(Exception):
    """A TransactWriteItems call was cancelled. `reasons` has one code per operation."""

    def __init__(self, reasons: list[str], message: str = ""):
        super().__init__(message or f"transaction cancelled: {reasons}")
        self.reasons = reasons


class ConditionFailed(Exception):
    pass


# ---------------------------------------------------------------------------
# Transaction operation builders. Pure functions; they touch nothing.
# ---------------------------------------------------------------------------


def op_put(table: str, item: dict, condition: str | None = None, names: dict | None = None,
           values: dict | None = None) -> dict:
    op: dict[str, Any] = {"Put": {"TableName": table, "Item": serialize_item(item)}}
    if condition:
        op["Put"]["ConditionExpression"] = condition
    if names:
        op["Put"]["ExpressionAttributeNames"] = names
    if values:
        op["Put"]["ExpressionAttributeValues"] = serialize_values(values)
    return op


def op_update(table: str, pk: str, sk: str, update: str, condition: str | None = None,
              names: dict | None = None, values: dict | None = None) -> dict:
    op: dict[str, Any] = {
        "Update": {
            "TableName": table,
            "Key": serialize_item({"pk": pk, "sk": sk}),
            "UpdateExpression": update,
        }
    }
    if condition:
        op["Update"]["ConditionExpression"] = condition
    if names:
        op["Update"]["ExpressionAttributeNames"] = names
    if values:
        op["Update"]["ExpressionAttributeValues"] = serialize_values(values)
    return op


class DynamoStore:
    def __init__(self, client: Any = None, records_table: str = "gwp-records", audit_table: str = "gwp-audit",
                 region: str = "us-east-1"):
        self.client = client or boto3.client("dynamodb", region_name=region)
        self.tables = {RECORDS: records_table, AUDIT: audit_table}

    # -- setup --------------------------------------------------------------

    def create_tables(self) -> None:
        """Create both tables. For tests and the offline eval; Terraform owns them on AWS."""
        keys = dict(
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        self.client.create_table(TableName=self.tables[RECORDS], **keys)
        audit = dict(keys)
        audit["AttributeDefinitions"] = keys["AttributeDefinitions"] + [
            {"AttributeName": "open_flag", "AttributeType": "S"},
            {"AttributeName": "open_since", "AttributeType": "S"},
        ]
        audit["GlobalSecondaryIndexes"] = [
            {
                "IndexName": "open_by_age",
                "KeySchema": [
                    {"AttributeName": "open_flag", "KeyType": "HASH"},
                    {"AttributeName": "open_since", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ]
        self.client.create_table(TableName=self.tables[AUDIT], **audit)

    def t(self, name: str) -> str:
        return self.tables[name]

    # -- generic reads ------------------------------------------------------

    def _get(self, table: str, pk: str, sk: str) -> dict | None:
        resp = self.client.get_item(TableName=self.tables[table], Key=serialize_item({"pk": pk, "sk": sk}),
                                    ConsistentRead=True)
        raw = resp.get("Item")
        return deserialize_item(raw) if raw else None

    def _query(self, table: str, pk: str, prefix: str) -> list[dict]:
        out: list[dict] = []
        kwargs: dict[str, Any] = dict(
            TableName=self.tables[table],
            KeyConditionExpression="pk = :pk AND begins_with(sk, :p)",
            ExpressionAttributeValues=serialize_values({":pk": pk, ":p": prefix}),
            ConsistentRead=True,
        )
        while True:
            resp = self.client.query(**kwargs)
            out.extend(deserialize_item(i) for i in resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                return out
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def scan_all(self, table: str) -> list[dict]:
        """Every item in a table. For the eval grader's snapshots only."""
        out: list[dict] = []
        kwargs: dict[str, Any] = dict(TableName=self.tables[table], ConsistentRead=True)
        while True:
            resp = self.client.scan(**kwargs)
            out.extend(deserialize_item(i) for i in resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                return out
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    # -- seeding (demo world loader only) -------------------------------------

    def seed(self, items: Iterable[dict], table: str = RECORDS) -> None:
        batch = [{"PutRequest": {"Item": serialize_item(i)}} for i in items]
        for start in range(0, len(batch), 25):
            request = {self.tables[table]: batch[start:start + 25]}
            while request:
                resp = self.client.batch_write_item(RequestItems=request)
                request = resp.get("UnprocessedItems") or {}

    # -- domain reads -------------------------------------------------------

    def get_tenant(self, tenant_id: str) -> dict | None:
        return self._get(RECORDS, tenant_pk(tenant_id), "TENANT")

    def get_vendor(self, tenant_id: str, vendor_id: str) -> dict | None:
        return self._get(RECORDS, tenant_pk(tenant_id), f"VENDOR#{vendor_id}")

    def list_vendors(self, tenant_id: str) -> list[dict]:
        return self._query(RECORDS, tenant_pk(tenant_id), "VENDOR#")

    def get_po(self, tenant_id: str, po_id: str) -> dict | None:
        return self._get(RECORDS, tenant_pk(tenant_id), f"PO#{po_id}")

    def list_pos(self, tenant_id: str, vendor_id: str | None = None) -> list[dict]:
        pos = self._query(RECORDS, tenant_pk(tenant_id), "PO#")
        return [p for p in pos if vendor_id is None or p["vendor_id"] == vendor_id]

    def get_receipts(self, tenant_id: str, po_id: str) -> list[dict]:
        return self._query(RECORDS, tenant_pk(tenant_id), f"RECEIPT#{po_id}#")

    def get_contract(self, tenant_id: str, contract_id: str) -> dict | None:
        return self._get(RECORDS, tenant_pk(tenant_id), f"CONTRACT#{contract_id}")

    def list_contracts(self, tenant_id: str, vendor_id: str | None = None) -> list[dict]:
        cs = self._query(RECORDS, tenant_pk(tenant_id), "CONTRACT#")
        return [c for c in cs if vendor_id is None or c["vendor_id"] == vendor_id]

    def get_payable(self, tenant_id: str, payable_id: str) -> dict | None:
        return self._get(RECORDS, tenant_pk(tenant_id), f"PAYABLE#{payable_id}")

    def list_payables(self, tenant_id: str, vendor_id: str | None = None) -> list[dict]:
        ps = self._query(RECORDS, tenant_pk(tenant_id), "PAYABLE#")
        return [p for p in ps if vendor_id is None or p["vendor_id"] == vendor_id]

    def find_invoice(self, tenant_id: str, vendor_id: str, normalized_invoice: str) -> dict | None:
        return self._get(RECORDS, tenant_pk(tenant_id), f"INVKEY#{vendor_id}#{normalized_invoice}")

    def find_credit(self, tenant_id: str, vendor_id: str, normalized_credit: str) -> dict | None:
        return self._get(RECORDS, tenant_pk(tenant_id), f"CREDITKEY#{vendor_id}#{normalized_credit}")

    def list_ledger(self, tenant_id: str) -> list[dict]:
        return self._query(RECORDS, tenant_pk(tenant_id), "ENTRY#")

    def list_outbox(self, tenant_id: str) -> list[dict]:
        return self._query(RECORDS, tenant_pk(tenant_id), "OUTBOX#")

    def get_outbox(self, tenant_id: str, message_id: str) -> dict | None:
        return self._get(RECORDS, tenant_pk(tenant_id), f"OUTBOX#{message_id}")

    def list_chunks(self, tenant_id: str) -> list[dict]:
        return self._query(RECORDS, tenant_pk(tenant_id), "CHUNK#")

    def get_key(self, tenant_id: str, sk: str) -> dict | None:
        return self._get(RECORDS, tenant_pk(tenant_id), sk)

    def owner_of(self, record_id: str) -> str | None:
        item = self._get(RECORDS, f"ID#{record_id}", "OWNER")
        return item["tenant_id"] if item else None

    # -- documents and runs (bookkeeping) -----------------------------------

    def put_document(self, doc: dict) -> None:
        item = {"pk": tenant_pk(doc["tenant_id"]), "sk": f"DOC#{doc['document_id']}", "kind": "document", **doc}
        self.client.put_item(TableName=self.tables[RECORDS], Item=serialize_item(item),
                             ConditionExpression="attribute_not_exists(pk)")

    def get_document(self, tenant_id: str, document_id: str) -> dict | None:
        return self._get(RECORDS, tenant_pk(tenant_id), f"DOC#{document_id}")

    def list_documents(self, tenant_id: str) -> list[dict]:
        return self._query(RECORDS, tenant_pk(tenant_id), "DOC#")

    def create_run(self, run: dict, run_key: str) -> tuple[dict, bool]:
        """Insert the run and its run key together. If the key exists, return the existing run."""
        pk = tenant_pk(run["tenant_id"])
        try:
            self.client.transact_write_items(TransactItems=[
                op_put(self.tables[RECORDS], {"pk": pk, "sk": f"RUNKEY#{run_key}", "kind": "run_key",
                                              "run_id": run["run_id"]}, "attribute_not_exists(pk)"),
                op_put(self.tables[RECORDS], {"pk": pk, "sk": f"RUN#{run['run_id']}", "kind": "run", **run},
                       "attribute_not_exists(pk)"),
            ])
            return run, True
        except ClientError as e:
            if e.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            key = self._get(RECORDS, pk, f"RUNKEY#{run_key}")
            if key is None:
                raise
            existing = self.get_run(run["tenant_id"], key["run_id"])
            assert existing is not None
            return existing, False

    def find_run_by_key(self, tenant_id: str, run_key: str) -> dict | None:
        key = self._get(RECORDS, tenant_pk(tenant_id), f"RUNKEY#{run_key}")
        return self.get_run(tenant_id, key["run_id"]) if key else None

    def get_run(self, tenant_id: str, run_id: str) -> dict | None:
        return self._get(RECORDS, tenant_pk(tenant_id), f"RUN#{run_id}")

    def list_runs(self, tenant_id: str) -> list[dict]:
        return self._query(RECORDS, tenant_pk(tenant_id), "RUN#")

    def update_run(self, tenant_id: str, run_id: str, fields: dict, history: tuple[str, str] | None = None) -> None:
        names: dict[str, str] = {}
        values: dict[str, Any] = {}
        sets = []
        for i, (k, v) in enumerate(fields.items()):
            names[f"#f{i}"] = k
            values[f":v{i}"] = v
            sets.append(f"#f{i} = :v{i}")
        if history:
            names["#h"] = "history"
            values[":h"] = [{"state": history[0], "at": history[1]}]
            values[":empty"] = []
            sets.append("#h = list_append(if_not_exists(#h, :empty), :h)")
        self.client.update_item(
            TableName=self.tables[RECORDS],
            Key=serialize_item({"pk": tenant_pk(tenant_id), "sk": f"RUN#{run_id}"}),
            UpdateExpression="SET " + ", ".join(sets),
            ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=serialize_values(values),
        )

    def put_human_task(self, task: dict) -> None:
        item = {"pk": tenant_pk(task["tenant_id"]), "sk": f"TASK#{task['task_id']}", "kind": "human_task", **task}
        self.client.put_item(TableName=self.tables[RECORDS], Item=serialize_item(item),
                             ConditionExpression="attribute_not_exists(pk)")

    def list_human_tasks(self, tenant_id: str) -> list[dict]:
        return self._query(RECORDS, tenant_pk(tenant_id), "TASK#")

    # -- audit records (bookkeeping) ----------------------------------------

    def put_audit(self, audit: dict) -> None:
        item = {"pk": tenant_pk(audit["tenant_id"]), "sk": f"AUDIT#{audit['audit_id']}", **audit}
        self.client.put_item(TableName=self.tables[AUDIT], Item=serialize_item(item),
                             ConditionExpression="attribute_not_exists(pk)")

    def get_audit(self, tenant_id: str, audit_id: str) -> dict | None:
        return self._get(AUDIT, tenant_pk(tenant_id), f"AUDIT#{audit_id}")

    def list_audits(self, tenant_id: str, run_id: str | None = None) -> list[dict]:
        items = self._query(AUDIT, tenant_pk(tenant_id), "AUDIT#")
        return [a for a in items if run_id is None or a.get("run_id") == run_id]

    def transition_audit(self, tenant_id: str, audit_id: str, from_statuses: Iterable[str], to_status: str,
                         at: str, fields: dict | None = None, terminal: bool = False) -> bool:
        """Compare-and-set on the audit status. Returns False if the status had moved on."""
        update, names, values = audit_transition_expr(from_statuses, to_status, at, fields or {}, terminal)
        try:
            self.client.update_item(
                TableName=self.tables[AUDIT],
                Key=serialize_item({"pk": tenant_pk(tenant_id), "sk": f"AUDIT#{audit_id}"}),
                UpdateExpression=update[0], ConditionExpression=update[1],
                ExpressionAttributeNames=names, ExpressionAttributeValues=serialize_values(values),
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def append_audit_note(self, tenant_id: str, audit_id: str, at: str, state: str, fields: dict) -> None:
        """Record something on an audit record without changing its status, e.g. a refused revert."""
        names = {"#h": "history"}
        values: dict[str, Any] = {":h": [{"state": state, "at": at}], ":empty": []}
        sets = ["#h = list_append(if_not_exists(#h, :empty), :h)"]
        for i, (k, v) in enumerate(fields.items()):
            names[f"#f{i}"] = k
            values[f":v{i}"] = v
            sets.append(f"#f{i} = :v{i}")
        self.client.update_item(
            TableName=self.tables[AUDIT],
            Key=serialize_item({"pk": tenant_pk(tenant_id), "sk": f"AUDIT#{audit_id}"}),
            UpdateExpression="SET " + ", ".join(sets), ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeNames=names, ExpressionAttributeValues=serialize_values(values),
        )

    def list_open_audits(self, older_than: str) -> list[dict]:
        """Audit records in a non-terminal state whose current state began before `older_than`."""
        resp = self.client.query(
            TableName=self.tables[AUDIT], IndexName="open_by_age",
            KeyConditionExpression="open_flag = :o AND open_since < :t",
            ExpressionAttributeValues=serialize_values({":o": "OPEN", ":t": older_than}),
        )
        return [deserialize_item(i) for i in resp.get("Items", [])]

    # -- domain writes -------------------------------------------------------

    def transact_domain(self, ops: list[dict]) -> None:
        """Run one all-or-nothing TransactWriteItems call. Called only by gwp.executor."""
        if not ops:
            return
        if len(ops) > 100:
            raise ValueError("a DynamoDB transaction holds at most 100 operations")
        try:
            self.client.transact_write_items(TransactItems=ops)
        except ClientError as e:
            if e.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            reasons = [r.get("Code", "None") for r in e.response.get("CancellationReasons", [])]
            raise TransactionConflict(reasons, str(e)) from e


def audit_transition_expr(from_statuses: Iterable[str], to_status: str, at: str, fields: dict,
                          terminal: bool, require: dict | None = None) -> tuple[tuple[str, str], dict, dict]:
    """Build the update for a guarded audit status change.

    Returns ((update_expression, condition_expression), names, values). The
    history list only ever grows. Terminal states drop the sparse index keys.
    `require` adds equality conditions on other attributes, e.g. the tier.
    """
    froms = list(from_statuses)
    names: dict[str, str] = {"#s": "status", "#h": "history", "#u": "updated_at"}
    values: dict[str, Any] = {":to": to_status, ":h": [{"state": to_status, "at": at}], ":empty": [], ":at": at}
    sets = ["#s = :to", "#h = list_append(if_not_exists(#h, :empty), :h)", "#u = :at"]
    for i, (k, v) in enumerate(fields.items()):
        names[f"#f{i}"] = k
        values[f":v{i}"] = v
        sets.append(f"#f{i} = :v{i}")
    removes = []
    if terminal:
        names["#of"], names["#os"] = "open_flag", "open_since"
        removes = ["#of", "#os"]
    else:
        names["#of"], names["#os"] = "open_flag", "open_since"
        values[":open"] = "OPEN"
        sets += ["#of = :open", "#os = :at"]
    cond_parts = []
    for i, s in enumerate(froms):
        values[f":from{i}"] = s
        cond_parts.append(f"#s = :from{i}")
    update = "SET " + ", ".join(sets) + (" REMOVE " + ", ".join(removes) if removes else "")
    condition = "(" + " OR ".join(cond_parts) + ")"
    for i, (k, v) in enumerate((require or {}).items()):
        names[f"#r{i}"] = k
        values[f":r{i}"] = v
        condition += f" AND #r{i} = :r{i}"
    return (update, condition), names, values
