"""Order/item specialist using the shared coordinator contract."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..models import AgentResult, AgentTask
from ..trace import TraceWriter

ACTOR = "order-agent"


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _records(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("items", "orders", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [data]
    return []


def _money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def _expected_total(items: list[dict[str, Any]], order: dict[str, Any]) -> float | None:
    for key in ("order_total_brl", "total_brl", "total_amount", "payment_value"):
        direct = _money(order.get(key))
        if direct is not None:
            return float(direct.quantize(Decimal("0.01")))
    if not items:
        return None
    total = Decimal("0")
    saw_amount = False
    for item in items:
        for key in ("price", "freight_value"):
            amount = _money(item.get(key))
            if amount is not None:
                total += amount
                saw_amount = True
    return float(total.quantize(Decimal("0.01"))) if saw_amount else None


async def _consume(
    *,
    tool_name: str,
    task: AgentTask,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    **arguments: str,
) -> dict[str, Any]:
    evidence = await gateway.call(tool_name, case_id=task.case_id, **arguments)
    evidence_ref = evidence["evidence_ref"]
    trace.emit(
        case_id=task.case_id,
        event_type="tool_result_consumed",
        actor=ACTOR,
        tool_name=tool_name,
        evidence_refs=[evidence_ref],
        attributes={"task_id": task.task_id},
    )
    return evidence


async def run(
    task: AgentTask,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> AgentResult:
    """Collect authoritative order/item facts without trusting claim topics."""

    if task.actor != ACTOR:
        raise ValueError(f"order agent received task for actor {task.actor!r}")

    order_id = task.context.get("claimed_order_id")
    if not isinstance(order_id, str) or not order_id.strip():
        return AgentResult(
            case_id=task.case_id,
            task_id=task.task_id,
            actor=ACTOR,
            findings={"recommended_issue": "insufficient_evidence"},
            errors=["Order lookup requires a resolved order_id."],
        )
    order_id = order_id.strip()

    refs: list[str] = []
    errors: list[str] = []
    order: dict[str, Any] = {}
    items: list[dict[str, Any]] = []

    try:
        evidence = await _consume(
            tool_name="get_order",
            task=task,
            gateway=gateway,
            trace=trace,
            order_id=order_id,
        )
        refs.append(evidence["evidence_ref"])
        records = _records(evidence.get("data"))
        order = records[0] if records else {}
    except (RuntimeError, ValueError, KeyError, TypeError) as exc:
        errors.append(f"required get_order failed: {exc}")

    try:
        evidence = await _consume(
            tool_name="get_order_items",
            task=task,
            gateway=gateway,
            trace=trace,
            order_id=order_id,
        )
        refs.append(evidence["evidence_ref"])
        raw_items = _records(evidence.get("data"))
        seen_items: set[str] = set()
        for item in raw_items:
            item_key = str(item.get("order_item_id") or item.get("item_id") or "")
            if item_key and item_key in seen_items:
                continue
            if item_key:
                seen_items.add(item_key)
            items.append(item)
    except (RuntimeError, ValueError, KeyError, TypeError) as exc:
        errors.append(f"required get_order_items failed: {exc}")

    item_ids = _unique([
        str(item[key])
        for item in items
        for key in ("order_item_id", "item_id")
        if item.get(key) is not None
    ])
    seller_ids = _unique([
        str(item["seller_id"])
        for item in items
        if item.get("seller_id") is not None
    ])
    evidence_order_id = order.get("order_id")
    order_ids = [str(evidence_order_id)] if evidence_order_id else ([order_id] if order else [])

    status = str(order.get("order_status") or order.get("status") or "").lower()
    if status in {"canceled", "cancelled"}:
        issue = "canceled_order_paid"
        confidence = 0.88 if refs else 0.0
    elif status in {"unavailable", "processing_failed", "invoiced_failed"}:
        issue = "unavailable_order_paid"
        confidence = 0.85 if refs else 0.0
    elif order:
        issue = None
        confidence = 0.75
    else:
        issue = "insufficient_evidence"
        confidence = 0.0

    return AgentResult(
        case_id=task.case_id,
        task_id=task.task_id,
        actor=ACTOR,
        findings={
            "recommended_issue": issue,
            "order_status": status or None,
            "order_purchase_timestamp": order.get("order_purchase_timestamp"),
            "expected_total_brl": _expected_total(items, order),
            "order": order,
            "items": items,
        },
        evidence_refs=_unique(refs),
        order_ids=_unique(order_ids),
        item_ids=item_ids,
        seller_ids=seller_ids,
        confidence=confidence,
        errors=errors,
    )


__all__ = ["ACTOR", "run"]
