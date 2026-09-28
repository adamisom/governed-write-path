"""Typed records shared by the write path, the agents and the evals.

Money is integer cents everywhere. Dates are ISO strings (YYYY-MM-DD) in stored
records and `datetime.date` in the typed model outputs.

Nothing in this module imports Strands. The agent layer imports these types,
never the other way round.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class DocumentKind(StrEnum):
    invoice = "invoice"
    credit_memo = "credit_memo"
    letter = "letter"
    other = "other"


class VendorRequest(StrEnum):
    """What a vendor letter or note asks for, as a closed set.

    The reader reports requests it sees as one of these values instead of passing
    the text on, so the proposer learns that a letter asks for a bank change
    without ever reading the letter.
    """

    bank_details_change = "bank_details_change"
    cancel_invoice = "cancel_invoice"
    expedite_payment = "expedite_payment"
    other = "other"


class Action(StrEnum):
    post_payable = "post_payable"
    hold_invoice = "hold_invoice"
    recode_line = "recode_line"
    apply_credit_memo = "apply_credit_memo"
    send_vendor_query = "send_vendor_query"
    request_human_review = "request_human_review"
    # Forbidden to the agent. They are in the enum on purpose, so an attempt is a
    # countable, audited proposal instead of a schema error.
    schedule_payment = "schedule_payment"
    update_vendor_bank_details = "update_vendor_bank_details"
    create_vendor = "create_vendor"
    delete_payable = "delete_payable"


FORBIDDEN_ACTIONS = frozenset(
    {
        Action.schedule_payment,
        Action.update_vendor_bank_details,
        Action.create_vendor,
        Action.delete_payable,
    }
)
WRITE_ACTIONS = frozenset(
    {
        Action.post_payable,
        Action.hold_invoice,
        Action.recode_line,
        Action.apply_credit_memo,
        Action.send_vendor_query,
    }
)


class Tier(StrEnum):
    auto = "auto"
    approval = "approval"
    forbidden = "forbidden"
    human = "human"  # request_human_review: not a write


class AuditStatus(StrEnum):
    proposed = "proposed"
    pending_approval = "pending_approval"
    approved = "approved"
    applying = "applying"
    applied = "applied"
    declined = "declined"
    rejected = "rejected"
    routed = "routed"
    failed = "failed"
    reverted = "reverted"


NON_TERMINAL_AUDIT = frozenset(
    {AuditStatus.proposed, AuditStatus.pending_approval, AuditStatus.approved, AuditStatus.applying}
)


class RunOutcome(StrEnum):
    APPLIED = "APPLIED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    DECLINED = "DECLINED"
    ROUTED_TO_HUMAN = "ROUTED_TO_HUMAN"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    DUPLICATE_UPLOAD = "DUPLICATE_UPLOAD"
    # A run in external-proposal mode, read and parked after step 5 until an agent proposes over MCP.
    AWAITING_PROPOSAL = "AWAITING_PROPOSAL"
    # Returned by `process` for a run another worker is processing. Never stored as a run's outcome.
    IN_PROGRESS = "IN_PROGRESS"


class RevertOutcome(StrEnum):
    REVERTED = "REVERTED"
    REVERT_REFUSED = "REVERT_REFUSED"


# ---------------------------------------------------------------------------
# The reader's output
# ---------------------------------------------------------------------------

_Str120 = Annotated[str, Field(max_length=120)]
_Id = Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9#._/-]+$")]
_Account = Annotated[str, Field(pattern=r"^\d{4}$")]
_Cents = Annotated[int, Field(ge=-10**11, le=10**11)]
_ReasonCode = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")]


class ExtractedLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: _Str120
    qty: int = Field(ge=0, le=1_000_000)
    unit_price_cents: _Cents
    amount_cents: _Cents


class Extraction(BaseModel):
    """Typed invoice fields. The only thing the reader model can return."""

    model_config = ConfigDict(extra="forbid")

    document_kind: DocumentKind
    vendor_name: _Str120
    vendor_tax_id: str | None = Field(default=None, max_length=20)
    remit_to_bank_last4: str | None = Field(default=None, pattern=r"^\d{4}$")
    invoice_number: str | None = Field(default=None, max_length=40)
    invoice_date: date | None = None
    po_number: str | None = Field(default=None, max_length=20)
    referenced_invoice_numbers: list[Annotated[str, Field(max_length=40)]] = Field(default_factory=list, max_length=5)
    lines: list[ExtractedLine] = Field(default_factory=list, max_length=50)
    freight_cents: _Cents = 0
    tax_cents: _Cents = 0
    total_cents: _Cents | None = None
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    notes_present: bool = False
    vendor_requests: list[VendorRequest] = Field(default_factory=list, max_length=4)
    conflicts: list[Annotated[str, Field(max_length=40)]] = Field(default_factory=list, max_length=10)


# ---------------------------------------------------------------------------
# The proposer's output
# ---------------------------------------------------------------------------


class PayableLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["item", "freight", "tax"] = "item"
    account: _Account
    amount_cents: _Cents
    source_line: int | None = Field(default=None, ge=1, le=50, description="1-based index into the extracted lines")
    po_line_no: int | None = Field(default=None, ge=1, le=50)


class PostPayableParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vendor_id: _Id
    invoice_number: Annotated[str, Field(min_length=1, max_length=40)]
    invoice_date: date
    po_id: _Id | None = None
    contract_id: _Id | None = None
    lines: list[PayableLine] = Field(min_length=1, max_length=50)
    total_cents: _Cents


class HoldInvoiceParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: _Id
    reason_code: _ReasonCode


class RecodeLineParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payable_id: _Id
    line_no: int = Field(ge=1, le=50)
    account: _Account


class ApplyCreditMemoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credit_number: Annotated[str, Field(min_length=1, max_length=40)]
    payable_id: _Id
    amount_cents: int = Field(gt=0, le=10**11)


class SendVendorQueryParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vendor_id: _Id
    template_id: _ReasonCode
    fields: dict[Annotated[str, Field(max_length=40)], Union[int, Annotated[str, Field(max_length=60)]]] = Field(
        default_factory=dict, max_length=8
    )


class RequestHumanReviewParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason_code: _ReasonCode


class _ForbiddenParams(BaseModel):
    """Forbidden actions accept loose parameters so an attempt is recorded, not lost."""

    model_config = ConfigDict(extra="allow")


class _ProposalBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rationale: str = Field(default="", max_length=1000, description="Model text. Stored as data, never executed.")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    requires_approval_reason: str | None = Field(
        default=None, max_length=200, description="Set this to raise the tier to approval. It can never lower a tier."
    )
    evidence: list[Annotated[str, Field(max_length=80)]] = Field(default_factory=list, max_length=20)


class PostPayable(_ProposalBase):
    action: Literal["post_payable"]
    params: PostPayableParams


class HoldInvoice(_ProposalBase):
    action: Literal["hold_invoice"]
    params: HoldInvoiceParams


class RecodeLine(_ProposalBase):
    action: Literal["recode_line"]
    params: RecodeLineParams


class ApplyCreditMemo(_ProposalBase):
    action: Literal["apply_credit_memo"]
    params: ApplyCreditMemoParams


class SendVendorQuery(_ProposalBase):
    action: Literal["send_vendor_query"]
    params: SendVendorQueryParams


class RequestHumanReview(_ProposalBase):
    action: Literal["request_human_review"]
    params: RequestHumanReviewParams


class SchedulePayment(_ProposalBase):
    action: Literal["schedule_payment"]
    params: _ForbiddenParams = Field(default_factory=_ForbiddenParams)


class UpdateVendorBankDetails(_ProposalBase):
    action: Literal["update_vendor_bank_details"]
    params: _ForbiddenParams = Field(default_factory=_ForbiddenParams)


class CreateVendor(_ProposalBase):
    action: Literal["create_vendor"]
    params: _ForbiddenParams = Field(default_factory=_ForbiddenParams)


class DeletePayable(_ProposalBase):
    action: Literal["delete_payable"]
    params: _ForbiddenParams = Field(default_factory=_ForbiddenParams)


ProposedWrite = Annotated[
    Union[
        PostPayable,
        HoldInvoice,
        RecodeLine,
        ApplyCreditMemo,
        SendVendorQuery,
        RequestHumanReview,
        SchedulePayment,
        UpdateVendorBankDetails,
        CreateVendor,
        DeletePayable,
    ],
    Field(discriminator="action"),
]


class ProposalSet(BaseModel):
    """At most three proposed writes for one document."""

    model_config = ConfigDict(extra="forbid")

    proposals: list[ProposedWrite] = Field(min_length=1, max_length=3)


def params_dict(proposal: BaseModel) -> dict:
    """The proposal's parameters as plain JSON-ready data."""
    return proposal.params.model_dump(mode="json", exclude_none=True)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Principals
# ---------------------------------------------------------------------------


class Role(StrEnum):
    uploader = "uploader"
    approver = "approver"
    admin = "admin"
    service = "service"
    # An outside agent, e.g. one connected over MCP. It can read a parked run's context and propose, nothing else.
    agent = "agent"


class Principal(BaseModel):
    principal_id: str
    role: Role


SERVICE_PRINCIPAL = Principal(principal_id="svc:gwp", role=Role.service)
