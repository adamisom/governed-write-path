"""The system of record, on DynamoDB, through boto3.

Two tables:

- records: the tenant's vendors, purchase orders, receipts, contracts, policy
  chunks, documents, runs, payables, ledger entries, outbox messages, human
  tasks, and the idempotency key items. Partition key `pk`, sort key `sk`.
  The records table has a sparse index on `lease_flag` and `lease_until`, which
  lists runs that are not finalized, so the sweep can find a run whose worker
  died without scanning.
- audit: one audit record per proposed write. It has a sparse index on
  `open_flag` and `open_since`, so the staleness check can list records that are
  still in a non-terminal state without scanning.

Every item for a tenant lives under `pk = TENANT#<id>`, so a lookup cannot reach
another tenant's data without naming that tenant. The one exception is the id
registry (`pk = ID#<id>`), which maps a record id to its owner so the policy can
tell a cross-tenant reference apart from a typo. It returns only the owner's id.

Writes come in two groups:

- Bookkeeping writes (documents, runs, audit records, human tasks, and the
  MCP server's access records and agent memory) are methods
  on this class and are called by the orchestrator, `gwp.access` and
  `gwp.memory`.
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
# Sparse index on the records table. A run carries `lease_flag` and `lease_until` from upload until it is
# finalized, so the index lists exactly the runs that are not finished, by when their lease runs out.
LEASE_INDEX = "leased_runs"

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
        # None is stored as DynamoDB NULL, not dropped, so a reader always finds the key it wrote.
        return {k: _to_dynamo(v) for k, v in value.items()}
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
        records = dict(keys)
        records["AttributeDefinitions"] = keys["AttributeDefinitions"] + [
            {"AttributeName": "lease_flag", "AttributeType": "S"},
            {"AttributeName": "lease_until", "AttributeType": "S"},
        ]
        records["GlobalSecondaryIndexes"] = [
            {
                "IndexName": LEASE_INDEX,
                "KeySchema": [
                    {"AttributeName": "lease_flag", "KeyType": "HASH"},
                    {"AttributeName": "lease_until", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ]
        self.client.create_table(TableName=self.tables[RECORDS], **records)
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

    def _run_update(self, tenant_id: str, run_id: str, fields: dict, history: tuple[str, str] | None,
                    expect_state: str | None, lease_before: str | None = None, set_incomplete: bool = False) -> dict:
        """Build the conditional update of a run, as keyword arguments for UpdateItem or a transaction's Update.

        `lease_before`: only if the run's lease, if any, ran out before that time. `set_incomplete`: only if the
        run's proposal set is not yet marked recorded (`audit_set_complete`).
        """
        names: dict[str, str] = {}
        values: dict[str, Any] = {}
        condition = "attribute_exists(pk)"
        if expect_state is not None:
            names["#state"] = "state"
            values[":expect_state"] = expect_state
            condition += " AND #state = :expect_state"
        if lease_before is not None:
            names["#lease"] = "lease_until"
            values[":lease_before"] = lease_before
            condition += " AND (attribute_not_exists(#lease) OR #lease < :lease_before)"
        if set_incomplete:
            names["#complete"] = "audit_set_complete"
            condition += " AND attribute_not_exists(#complete)"
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
        removes = ""
        if fields.get("state") == "finalized":
            names["#lf"], names["#lu"] = "lease_flag", "lease_until"
            removes = " REMOVE #lf, #lu"
        return {"TableName": self.tables[RECORDS],
                "Key": serialize_item({"pk": tenant_pk(tenant_id), "sk": f"RUN#{run_id}"}),
                "UpdateExpression": "SET " + ", ".join(sets) + removes, "ConditionExpression": condition,
                "ExpressionAttributeNames": names, "ExpressionAttributeValues": serialize_values(values)}

    def update_run(self, tenant_id: str, run_id: str, fields: dict, history: tuple[str, str] | None = None,
                   expect_state: str | None = None, **conditions) -> bool:
        """Set fields on a run. With `expect_state`, only if the run is still in that state; returns False if not.
        `conditions` are `_run_update`'s other conditions, and a failed one also returns False.

        Finalizing a run removes its lease, so it leaves the `leased_runs` index.
        """
        conditional = expect_state is not None or any(conditions.values())
        try:
            self.client.update_item(**self._run_update(tenant_id, run_id, fields, history, expect_state,
                                                       **conditions))
        except ClientError as e:
            if conditional and e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def append_to_run(self, tenant_id: str, run_id: str, lists: dict[str, list], expect_state: str,
                      max_len: tuple[str, int] | None = None) -> bool:
        """Append to list fields on a run, only while it is in `expect_state`, and, with `max_len` (field, n), only
        while that list is shorter than n. Returns False if a condition failed.

        An append, not a read and a write, so two calls at once both land, and neither passes the limit.
        """
        names: dict[str, str] = {"#state": "state"}
        values: dict[str, Any] = {":expect_state": expect_state, ":empty": []}
        condition = "attribute_exists(pk) AND #state = :expect_state"
        if max_len is not None:
            names["#cap"] = max_len[0]
            values[":cap"] = max_len[1]
            condition += " AND (attribute_not_exists(#cap) OR size(#cap) < :cap)"
        sets = []
        for i, (k, v) in enumerate(lists.items()):
            names[f"#l{i}"] = k
            values[f":l{i}"] = v
            sets.append(f"#l{i} = list_append(if_not_exists(#l{i}, :empty), :l{i})")
        try:
            self.client.update_item(
                TableName=self.tables[RECORDS],
                Key=serialize_item({"pk": tenant_pk(tenant_id), "sk": f"RUN#{run_id}"}),
                UpdateExpression="SET " + ", ".join(sets),
                ConditionExpression=condition,
                ExpressionAttributeNames=names, ExpressionAttributeValues=serialize_values(values))
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def finalize_run_with_task(self, tenant_id: str, run_id: str, fields: dict, history: tuple[str, str],
                               task: dict, expect_state: str | None = None, **conditions) -> bool:
        """Finalize a run and open its task for a person in one transaction. Returns False if the run had left
        `expect_state`.

        Finalizing drops the run's lease, and the lease is how the sweep finds an unfinished run. Done as two
        writes, a crash between them left a finalized run with no task and nothing that would ever open it.
        """
        if fields.get("state") != "finalized":
            raise ValueError("finalize_run_with_task must finalize the run")
        return self.update_run_with_task(tenant_id, run_id, fields, history, task, expect_state, **conditions)

    def update_run_with_task(self, tenant_id: str, run_id: str, fields: dict, history: tuple[str, str],
                             task: dict, expect_state: str | None = None, **conditions) -> bool:
        """Update a run and open a task for a person in one transaction. Returns False if the run had left
        `expect_state` or failed one of `conditions`. If the task is open already, the run is updated by itself.
        Any other cancellation, e.g. contention, raises the `ClientError` for the caller to handle."""
        update = self._run_update(tenant_id, run_id, fields, history, expect_state, **conditions)
        try:
            self.client.transact_write_items(TransactItems=[
                {"Update": update},
                {"Put": {"TableName": self.tables[RECORDS], "Item": serialize_item(self._task_item(task)),
                         "ConditionExpression": "attribute_not_exists(pk)"}},
            ])
        except ClientError as e:
            if e.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            reasons = [r.get("Code", "None") for r in e.response.get("CancellationReasons", [])]
            if reasons[:1] == ["ConditionalCheckFailed"]:
                return False  # another worker or sweep moved the run on first
            if reasons[1:2] == ["ConditionalCheckFailed"]:
                # The task is open already, so updating the run by itself leaves nothing to lose.
                return self.update_run(tenant_id, run_id, fields, history, expect_state, **conditions)
            raise
        return True

    @staticmethod
    def _task_item(task: dict) -> dict:
        # Keyed by run and reason, not by task id, so a second open of the same task fails its condition.
        return {"pk": tenant_pk(task["tenant_id"]), "sk": f"TASK#{task['run_id']}#{task['reason_code']}",
                "kind": "human_task", **task}

    def open_human_task(self, task: dict) -> bool:
        """Open a task for a person, at most one per run and reason. Return False if that task already exists.

        The item key is the run and the reason, not the task id, so two workers that both decide a run needs the
        same task can't open two: the second conditional put fails.
        """
        try:
            self.client.put_item(TableName=self.tables[RECORDS], Item=serialize_item(self._task_item(task)),
                                 ConditionExpression="attribute_not_exists(pk)")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def list_human_tasks(self, tenant_id: str) -> list[dict]:
        return self._query(RECORDS, tenant_pk(tenant_id), "TASK#")

    # -- access records (bookkeeping) ---------------------------------------

    def put_access_record(self, record: dict) -> None:
        """Append one record of a call to the MCP server, allowed or denied. Never overwritten.

        Access records live in the records table under the caller's tenant, apart from the audit table, which
        holds one record per proposed write.
        """
        item = {"pk": tenant_pk(record["tenant_id"]), "sk": f"ACCESS#{record['at']}#{record['access_id']}",
                "kind": "access_record", **record}
        self.client.put_item(TableName=self.tables[RECORDS], Item=serialize_item(item),
                             ConditionExpression="attribute_not_exists(pk)")

    def list_access_records(self, tenant_id: str) -> list[dict]:
        return self._query(RECORDS, tenant_pk(tenant_id), "ACCESS#")

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

    def list_expired_leases(self, before: str) -> list[dict]:
        """Runs, in any tenant, that are not finalized and whose lease ran out before `before`."""
        out: list[dict] = []
        kwargs: dict[str, Any] = dict(
            TableName=self.tables[RECORDS], IndexName=LEASE_INDEX,
            KeyConditionExpression="lease_flag = :l AND lease_until < :t",
            ExpressionAttributeValues=serialize_values({":l": "LEASED", ":t": before}),
        )
        while True:
            resp = self.client.query(**kwargs)
            out.extend(deserialize_item(i) for i in resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                return out
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

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
