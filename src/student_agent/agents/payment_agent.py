"""Payment and refund reconciliation for normalized MCP evidence.

The MCP gateway deliberately exposes tool names and evidence envelopes without
defining payment-domain field names. The coordinator or a tool adapter must map
authoritative MCP data to the records below before calling :func:`analyze_payment`.
This module never invents evidence references or decides refund eligibility.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from ..mcp_gateway import EvidenceGateway
from ..models import AgentResult, AgentTask
from ..trace import TraceWriter
import re
from typing import Any, Literal

PaymentStatus = Literal["captured", "pending", "failed"]
RefundStatus = Literal["completed", "pending", "failed"]
EVIDENCE_REF_PATTERN = re.compile(r"^ev_[A-Za-z0-9_-]{20,96}$")
PAYMENT_TOOL_NAMES = (
    "get_order_payments",
    "get_payment_timeline",
    "get_refund_timeline",
)


async def collect_payment_evidence(
    *,
    case_id: str,
    order_id: str,
    gateway: Any,
    trace: Any,
) -> dict[str, Any]:
    """Fetch this case's payment/refund evidence using the discovered MCP tools.

    This function returns the authoritative evidence envelopes unchanged. The
    response ``data`` mapping must be normalized only after its MCP shape has
    been inspected; evidence refs are copied verbatim into the trace/result.
    """
    if not case_id.strip():
        raise ValueError("case_id must not be empty")
    if not order_id.strip():
        raise ValueError("order_id must be resolved before payment lookup")

    available_tools = set(await gateway.list_tools())
    missing_tools = [name for name in PAYMENT_TOOL_NAMES if name not in available_tools]
    if missing_tools:
        raise RuntimeError(f"MCP gateway is missing payment tools: {missing_tools}")

    evidence_by_tool: dict[str, dict[str, Any]] = {}
    evidence_refs: list[str] = []
    for tool_name in PAYMENT_TOOL_NAMES:
        evidence = await gateway.call(
            tool_name,
            case_id=case_id,
            order_id=order_id,
        )
        evidence_by_tool[tool_name] = evidence
        evidence_ref = evidence["evidence_ref"]
        if evidence_ref not in evidence_refs:
            evidence_refs.append(evidence_ref)
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="payment-agent",
            tool_name=tool_name,
            evidence_refs=[evidence_ref],
        )

    return {
        "case_id": case_id,
        "order_id": order_id,
        "evidence_by_tool": evidence_by_tool,
        "evidence_refs": evidence_refs,
    }


@dataclass(frozen=True)
class PaymentTransaction:
    """A payment transaction normalized from one MCP evidence response."""

    transaction_id: str
    payment_reference: str
    amount_brl: Decimal | str | int | float
    status: PaymentStatus
    evidence_ref: str
    duplicate_charge_confirmed: bool = False


@dataclass(frozen=True)
class RefundTransaction:
    """A refund transaction normalized from one MCP evidence response."""

    transaction_id: str
    payment_reference: str
    amount_brl: Decimal | str | int | float
    status: RefundStatus
    evidence_ref: str


@dataclass(frozen=True)
class RefundLine:
    """A proposed refund line whose eligibility was decided by the caller/policy."""

    reason_code: str
    amount_brl: Decimal | str | int | float
    entity_id: str | None = None


def _money(value: Decimal | str | int | float, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a non-negative BRL amount")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} is not a valid amount") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{label} must be a finite, non-negative amount")
    return amount.quantize(Decimal("0.01"))


def _validate_record(record: PaymentTransaction | RefundTransaction, kind: str) -> None:
    if not record.transaction_id.strip():
        raise ValueError(f"{kind} transaction_id must not be empty")
    if not record.payment_reference.strip():
        raise ValueError(f"{kind} payment_reference must not be empty")
    if not EVIDENCE_REF_PATTERN.fullmatch(record.evidence_ref):
        raise ValueError(f"{kind} evidence_ref is not a valid MCP evidence reference")
    _money(record.amount_brl, f"{kind} amount_brl")


def analyze_payment(
    *,
    case_id: str,
    payments: list[PaymentTransaction],
    refunds: list[RefundTransaction],
    expected_total_brl: Decimal | str | int | float | None = None,
    refund_lines: list[RefundLine] | None = None,
) -> dict[str, Any]:
    """Reconcile normalized payment/refund records for one case.

    ``expected_total_brl`` must come from authoritative order/item evidence.
    ``refund_lines`` must already have passed policy eligibility checks. When
    either input is missing, the function reports insufficient evidence and
    does not infer an expected charge or recommend a refund.
    """
    if not case_id:
        raise ValueError("case_id must not be empty")
    refund_lines = refund_lines or []

    for record in payments:
        _validate_record(record, "payment")
        if record.status not in {"captured", "pending", "failed"}:
            raise ValueError(f"unsupported payment status: {record.status!r}")
    for record in refunds:
        _validate_record(record, "refund")
        if record.status not in {"completed", "pending", "failed"}:
            raise ValueError(f"unsupported refund status: {record.status!r}")

    payment_ids = Counter(
        record.transaction_id for record in payments if record.status == "captured"
    )
    duplicate_capture_record = any(count > 1 for count in payment_ids.values())
    duplicate_charge_confirmed = any(
        record.duplicate_charge_confirmed for record in payments
    )
    unique_captures: dict[str, PaymentTransaction] = {}
    for record in payments:
        if record.status == "captured":
            unique_captures.setdefault(record.transaction_id, record)

    captured_total = sum(
        (_money(record.amount_brl, "payment amount_brl") for record in unique_captures.values()),
        Decimal("0.00"),
    )
    completed_refunds: dict[str, RefundTransaction] = {}
    duplicate_refund_ids = Counter(
        record.transaction_id for record in refunds if record.status == "completed"
    )
    duplicate_refund = any(count > 1 for count in duplicate_refund_ids.values())
    for record in refunds:
        if record.status == "completed":
            completed_refunds.setdefault(record.transaction_id, record)
    refunded_total = sum(
        (_money(record.amount_brl, "refund amount_brl") for record in completed_refunds.values()),
        Decimal("0.00"),
    )
    pending_refund_total = sum(
        (
            _money(record.amount_brl, "refund amount_brl")
            for record in refunds
            if record.status == "pending"
        ),
        Decimal("0.00"),
    )

    expected = (
        _money(expected_total_brl, "expected_total_brl")
        if expected_total_brl is not None
        else None
    )
    if duplicate_charge_confirmed:
        verdict = "duplicate_charge"
    elif any(record.status == "failed" for record in refunds):
        verdict = "refund_failed"
    elif any(record.status == "pending" for record in refunds):
        verdict = "refund_pending"
    elif expected is None or not unique_captures:
        verdict = "insufficient_evidence"
    elif captured_total != expected:
        verdict = "payment_mismatch"
    elif len(unique_captures) > 1:
        verdict = "valid_split_payment"
    else:
        verdict = "payment_reconciled"

    line_total = sum(
        (_money(line.amount_brl, "refund line amount_brl") for line in refund_lines),
        Decimal("0.00"),
    )
    for line in refund_lines:
        if not line.reason_code.strip():
            raise ValueError("refund line reason_code must not be empty")
    if line_total > captured_total - refunded_total:
        raise ValueError("proposed refund exceeds the unreimbursed captured total")

    refs = list(dict.fromkeys(record.evidence_ref for record in [*payments, *refunds]))
    payment_references = list(
        dict.fromkeys(record.payment_reference for record in [*payments, *refunds])
    )

    return {
        "case_id": case_id,
        "findings": {
            "payment_verdict": verdict,
            "captured_total_brl": float(captured_total),
            "refunded_total_brl": float(refunded_total),
            "pending_refund_total_brl": float(pending_refund_total),
            "refundable_total_brl": float(
                max(captured_total - refunded_total, Decimal("0.00"))
            ),
            "duplicate_evidence_records": duplicate_capture_record or duplicate_refund,
        },
        "entities": {"payment_references": payment_references},
        "evidence_refs": refs,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": float(line_total),
            "refund_lines": [
                {
                    "reason_code": line.reason_code,
                    "amount_brl": float(_money(line.amount_brl, "refund line amount_brl")),
                    "entity_id": line.entity_id,
                }
                for line in refund_lines
            ],
        },
        "refund_evidence_refs": list(
            dict.fromkeys(record.evidence_ref for record in refunds)
        ),
    }

async def run(
    task: AgentTask,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> AgentResult:
    """Payment-agent entry point used by the coordinator dispatcher.

    Customer claims are routing hints only. This function retrieves
    authoritative MCP evidence and returns it through the common AgentResult
    contract. Raw MCP field names are not guessed here; domain normalization
    is performed only after the actual MCP response shape is known.
    """

    if task.actor != "payment-agent":
        raise ValueError(
            f"payment agent received task for actor {task.actor!r}"
        )

    order_id = task.context.get("claimed_order_id")

    if not isinstance(order_id, str) or not order_id.strip():
        return AgentResult(
            case_id=task.case_id,
            task_id=task.task_id,
            actor=task.actor,
            findings={
                "payment_verdict": "insufficient_evidence",
                "reason": "missing_order_id",
            },
            confidence=0.0,
            errors=["Payment lookup requires a resolved order_id."],
        )

    collected = await collect_payment_evidence(
        case_id=task.case_id,
        order_id=order_id,
        gateway=gateway,
        trace=trace,
    )

    evidence_by_tool = collected["evidence_by_tool"]
    evidence_refs = collected["evidence_refs"]

    # Keep authoritative MCP payload available to the verifier/integration
    # layer without guessing undocumented MCP domain field names.
    findings = {
        "payment_verdict": "insufficient_evidence",
        "evidence_collected": True,
        "evidence_by_tool": evidence_by_tool,
    }

    return AgentResult(
        case_id=task.case_id,
        task_id=task.task_id,
        actor=task.actor,
        findings=findings,
        evidence_refs=evidence_refs,
        confidence=0.25,
        errors=[
            "Payment MCP evidence collected but requires domain normalization."
        ],
    )