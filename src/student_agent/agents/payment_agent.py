"""Payment and refund reconciliation for normalized MCP evidence.

The MCP gateway deliberately exposes tool names and evidence envelopes without
defining payment-domain field names. The coordinator or a tool adapter must map
authoritative MCP data to the records below before calling :func:`analyze_payment`.
This module never invents evidence references or decides refund eligibility.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from ..mcp_gateway import EvidenceGateway
from ..models import AgentResult, AgentTask
from ..trace import TraceWriter

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

    evidence_by_tool: dict[str, dict[str, Any]] = {}
    evidence_refs: list[str] = []
    errors: list[str] = []
    required_tools = PAYMENT_TOOL_NAMES[:2]
    for tool_name in PAYMENT_TOOL_NAMES:
        try:
            evidence = await gateway.call(
                tool_name,
                case_id=case_id,
                order_id=order_id,
            )
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            kind = "required" if tool_name in required_tools else "optional"
            errors.append(f"{kind} {tool_name} failed: {exc}")
            continue
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
        "errors": errors,
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

def _dict_records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _payment_rows(evidence_by_tool: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    envelope = evidence_by_tool.get("get_order_payments", {})
    rows = _dict_records(envelope.get("data"))
    if rows:
        return rows
    timeline = evidence_by_tool.get("get_payment_timeline", {}).get("data")
    return _dict_records(timeline.get("payments")) if isinstance(timeline, dict) else []


def _events(
    evidence_by_tool: dict[str, dict[str, Any]], tool_name: str
) -> list[dict[str, Any]]:
    data = evidence_by_tool.get(tool_name, {}).get("data")
    if isinstance(data, dict):
        return _dict_records(data.get("events"))
    return _dict_records(data)


def _amount(value: Any) -> Decimal | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return amount.quantize(Decimal("0.01"))


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _within_case_window(
    events: list[dict[str, Any]],
    purchase_at: Any,
    opened_at: Any,
) -> list[dict[str, Any]]:
    start = _timestamp(purchase_at)
    end = _timestamp(opened_at)
    selected: list[dict[str, Any]] = []
    for event in events:
        occurred = _timestamp(event.get("event_at"))
        in_window = (start is None or occurred is None or occurred >= start) and (
            end is None or occurred is None or occurred <= end
        )
        if in_window:
            selected.append(event)
    return selected


def normalize_payment_evidence(
    evidence_by_tool: dict[str, dict[str, Any]],
    expected_total_brl: Any = None,
    purchase_at: Any = None,
    opened_at: Any = None,
) -> dict[str, Any]:
    """Normalize only fields observed in the public MCP payment shapes."""

    payments = _payment_rows(evidence_by_tool)
    timeline_events = _within_case_window(
        _events(evidence_by_tool, "get_payment_timeline"),
        purchase_at,
        opened_at,
    )
    refund_events = _within_case_window(
        _events(evidence_by_tool, "get_refund_timeline"),
        purchase_at,
        opened_at,
    )

    expected = _amount(expected_total_brl) if expected_total_brl is not None else None

    fingerprints = [
        (
            str(row.get("payment_sequential", "")),
            str(row.get("payment_type", "")),
            str(row.get("payment_installments", "")),
            str(row.get("payment_value", "")),
        )
        for row in payments
    ]
    duplicate_rows = len(fingerprints) != len(set(fingerprints))

    capture_events = [
        event
        for event in timeline_events
        if str(event.get("event_type", "")).lower() in {"captured", "charged"}
        and str(event.get("status", "")).lower()
        in {"confirmed", "completed", "captured", "success"}
    ]
    captured_values = [
        amount
        for event in capture_events
        if (amount := _amount(event.get("amount_brl"))) is not None
    ]
    if not captured_values:
        captured_values = [
            amount
            for row in payments
            if (amount := _amount(row.get("payment_value"))) is not None
        ]
    captured_total = sum(captured_values, Decimal("0.00"))
    capture_count = len(capture_events) if capture_events else len(payments)
    amount_counts = Counter(captured_values)
    duplicate_events = any(count > 1 for count in amount_counts.values()) and (
        expected is None or captured_total > expected
    )

    refund_statuses = {
        str(event.get("status") or event.get("event_type") or "").lower()
        for event in refund_events
    }
    has_mismatch_event = any(
        str(event.get("event_type", "")).lower() == "reconciliation_mismatch"
        for event in timeline_events
    )
    if refund_statuses & {"failed", "rejected", "error"}:
        verdict = "refund_failed"
        confidence = 0.94
    elif refund_statuses & {"pending", "processing", "requested", "initiated"}:
        verdict = "refund_pending"
        confidence = 0.91
    elif duplicate_events or (duplicate_rows and expected is None):
        verdict = "duplicate_charge"
        confidence = 0.90
    elif has_mismatch_event or (
        expected is not None and capture_events and captured_total != expected
    ):
        verdict = "payment_mismatch"
        confidence = 0.90
    elif expected is not None and capture_count > 1 and captured_total == expected:
        verdict = "valid_split_payment"
        confidence = 0.92
    elif expected is not None and payments and captured_total == expected:
        verdict = "payment_reconciled"
        confidence = 0.88
    else:
        verdict = "insufficient_evidence"
        confidence = 0.25 if payments or timeline_events else 0.0

    actual_references: list[str] = []
    for record in [*payments, *timeline_events, *refund_events]:
        for key in ("payment_reference", "transaction_id"):
            value = record.get(key)
            if isinstance(value, str) and value:
                actual_references.append(value)

    return {
        "payment_verdict": verdict,
        "captured_total_brl": float(captured_total),
        "expected_total_brl": float(expected) if expected is not None else None,
        "payment_count": capture_count,
        "payments": payments,
        "payment_events": timeline_events,
        "refund_events": refund_events,
        "payment_references": list(dict.fromkeys(actual_references)),
        "confidence": confidence,
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
    normalized = normalize_payment_evidence(
        evidence_by_tool,
        task.context.get("expected_total_brl"),
        task.context.get("order_purchase_timestamp"),
        task.context.get("opened_at"),
    )
    findings = {
        **normalized,
        "evidence_by_tool": evidence_by_tool,
    }

    return AgentResult(
        case_id=task.case_id,
        task_id=task.task_id,
        actor=task.actor,
        findings=findings,
        evidence_refs=evidence_refs,
        payment_references=normalized["payment_references"],
        confidence=normalized["confidence"],
        errors=collected["errors"],
    )
