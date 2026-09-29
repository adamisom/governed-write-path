"""Who is calling the MCP server, what each role may call, and the record of every call.

The caller's principal, role and tenant always come from its credential, never from a tool argument, the same as
the HTTP API. Each write verb belongs to exactly one role on the MCP server:

- an agent reads a parked run's context, searches the policy and proposes, and nothing else;
- an approver approves or declines;
- an admin reverts.

This is stricter than the orchestrator's own checks, where an admin may also approve and an approver may also
revert. Those checks stay underneath as a second layer.

Every call gets an access record before it runs, whether it is allowed or denied. A call whose record can't be
written doesn't run, so there is no unrecorded call and no unrecorded denial. Each record carries `expires_at`, and
the records table's TTL deletes it after `ACCESS_RECORD_RETENTION_DAYS`, so a caller can't grow the table without
bound (DECISIONS M11). How many calls a caller may make is the gateway's limit, not this module's.

This module imports no MCP or agent framework. `gwp.mcp_server` uses it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from .runtime import Clock, Ids, sha256_hex
from .schema import Principal, Role
from .store import DynamoStore

READ_ANY = frozenset({Role.agent, Role.approver, Role.admin})

# Longer than a year, so a year of calls can always be reviewed. DynamoDB's TTL deletes a record after this.
ACCESS_RECORD_RETENTION_DAYS = 400

# Tool name -> the roles that may call it.
TOOL_ROLES: dict[str, frozenset[Role]] = {
    "list_work": frozenset({Role.agent}),
    "get_proposal_context": frozenset({Role.agent}),
    "search_policy": frozenset({Role.agent}),
    "propose": frozenset({Role.agent}),
    "cannot_propose": frozenset({Role.agent}),
    "list_pending_approvals": frozenset({Role.approver}),
    "get_approval_view": frozenset({Role.approver}),
    "decide": frozenset({Role.approver}),
    "revert": frozenset({Role.admin}),
    "get_run": READ_ANY,
    "list_runs": READ_ANY,
    "get_audit": READ_ANY,
}


@dataclass(frozen=True)
class Caller:
    principal: Principal
    tenant_id: str


class AccessDenied(Exception):
    def __init__(self, message: str, access_id: str):
        super().__init__(message)
        self.access_id = access_id


def caller_from_key(api_key: str, keys_json: str) -> Caller | None:
    """Look an API key up in the same table the HTTP API uses: JSON {sha256(key): {principal_id, role, tenant_id}}."""
    entry = json.loads(keys_json or "{}").get(sha256_hex(api_key))
    if not entry:
        return None
    return Caller(Principal(principal_id=entry["principal_id"], role=Role(entry["role"])), entry["tenant_id"])


def expires_at(at: str) -> int:
    """The epoch second at which an access record written at `at` (ISO, from `Clock.now()`) may be deleted."""
    written = datetime.strptime(at[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return int(written.timestamp()) + ACCESS_RECORD_RETENTION_DAYS * 86400


class AccessLog:
    def __init__(self, store: DynamoStore, clock: Clock, ids: Ids):
        self.store = store
        self.clock = clock
        self.ids = ids

    def record(self, caller: Caller, tool: str, decision: str, reason: str | None = None,
               targets: dict[str, str] | None = None, layer: str = "mcp_server") -> str:
        access_id = self.ids.new("X")
        at = self.clock.now()
        rec = {"access_id": access_id, "tenant_id": caller.tenant_id, "at": at, "expires_at": expires_at(at),
               "principal_id": caller.principal.principal_id, "role": caller.principal.role.value, "tool": tool,
               "decision": decision, "layer": layer, "targets": targets or {}}
        if reason:
            rec["reason"] = reason
        self.store.put_access_record(rec)
        return access_id

    def check(self, caller: Caller, tool: str, targets: dict[str, str] | None = None) -> str:
        """Record the call, then allow it or raise AccessDenied. Returns the access record's id."""
        allowed = TOOL_ROLES.get(tool, frozenset())
        if caller.principal.role in allowed:
            return self.record(caller, tool, "allowed", targets=targets)
        need = " or ".join(sorted(r.value for r in allowed)) or "no role"
        reason = f"{tool} needs the {need} role; the caller has {caller.principal.role.value}"
        access_id = self.record(caller, tool, "denied", reason, targets)
        raise AccessDenied(f"access denied: {reason} (recorded as {access_id})", access_id)
