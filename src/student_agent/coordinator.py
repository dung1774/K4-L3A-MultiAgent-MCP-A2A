from __future__ import annotations

from typing import Any

from .models import AgentTask
from .trace import TraceWriter

OBJECTIVES = {
    "order-agent": (
        "Verify order, item, seller and product facts using authoritative MCP evidence."
    ),
    "payment-agent": (
        "Verify payment, charge, payment timeline and refund facts "
        "using authoritative MCP evidence."
    ),
    "shipment-agent": (
        "Verify shipment and delivery facts and identify logistics-related delays."
    ),
    "policy-agent": (
        "Retrieve the applicable policy and determine relevant customer "
        "rights and resolution rules."
    ),
}


PAYMENT_HINTS = (
    "payment",
    "paid",
    "charge",
    "refund",
)

SHIPMENT_HINTS = (
    "shipment",
    "delivery",
    "late",
    "logistics",
)


def _customer_request(case: dict[str, Any]) -> dict[str, Any]:
    request = case.get("customer_request")
    return request if isinstance(request, dict) else {}


def _claim_topics(case: dict[str, Any]) -> list[str]:
    request = _customer_request(case)
    claims = request.get("claims")

    if not isinstance(claims, list):
        return []

    topics: list[str] = []

    for claim in claims:
        if not isinstance(claim, dict):
            continue

        topic = claim.get("topic")

        if isinstance(topic, str) and topic.strip():
            topics.append(topic.strip())

    return topics


def _claim_ids(case: dict[str, Any]) -> list[str]:
    request = _customer_request(case)
    claims = request.get("claims")

    if not isinstance(claims, list):
        return []

    claim_ids: list[str] = []

    for claim in claims:
        if not isinstance(claim, dict):
            continue

        claim_id = claim.get("claim_id")

        if isinstance(claim_id, str) and claim_id.strip():
            claim_ids.append(claim_id.strip())

    return claim_ids

def _select_specialists(case: dict[str, Any]) -> list[str]:
    """
    Claim topics are routing hints only, never ground truth.
    Factual conclusions must come from MCP evidence.
    """
    topics = set(_claim_topics(case))

    selected = {
        "order-agent",
        "policy-agent",
    }

    payment_topics = {
        "canceled_order_paid",
        "unavailable_order_paid",
        "valid_split_payment",
        "payment_mismatch",
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
        "requested_full_refund",
    }

    shipment_topics = {
        "late_delivery_seller",
        "late_delivery_logistics",
    }

    if topics & payment_topics:
        selected.add("payment-agent")

    if topics & shipment_topics:
        selected.add("shipment-agent")

    # Unknown/unsupported claims should be investigated broadly
    # rather than trusted at face value.
    if "unsupported_claim" in topics or not topics:
        selected.add("payment-agent")
        selected.add("shipment-agent")

    order = (
        "order-agent",
        "payment-agent",
        "shipment-agent",
        "policy-agent",
    )

    return [actor for actor in order if actor in selected]

def plan_tasks(case: dict[str, Any]) -> list[AgentTask]:
    case_id = case.get("case_id")

    if not isinstance(case_id, str) or not case_id:
        raise ValueError("case is missing a valid case_id")

    request = _customer_request(case)

    context = {
        "claimed_order_id": request.get("claimed_order_id"),
        "policy_version": case.get("policy_version"),
        "opened_at": case.get("opened_at"),
        "claim_ids": _claim_ids(case),
        "claim_topics": _claim_topics(case),
    }

    tasks: list[AgentTask] = []

    for actor in _select_specialists(case):
        tasks.append(
            AgentTask(
                case_id=case_id,
                task_id=f"{case_id}:{actor}",
                actor=actor,  # type: ignore[arg-type]
                objective=OBJECTIVES[actor],
                context=dict(context),
            )
        )

    return tasks


def emit_task_assignments(
    tasks: list[AgentTask],
    trace: TraceWriter,
) -> None:
    for task in tasks:
        trace.emit(
            case_id=task.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=task.actor,
            decision_code="SPECIALIST_ASSIGNED",
            attributes={
                "task_id": task.task_id,
            },
        )


def emit_verifier_handoff(
    *,
    case_id: str,
    result_count: int,
    evidence_count: int,
    trace: TraceWriter,
) -> None:
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="verifier",
        decision_code="READY_FOR_VERIFICATION",
        attributes={
            "specialist_results": result_count,
            "evidence_count": evidence_count,
        },
    )
