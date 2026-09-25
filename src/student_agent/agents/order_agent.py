"""TV2 — Order / Item / Seller Agent.

Responsibilities:
- Fetch order status, order items, seller info via MCP gateway.
- Detect issues: canceled_order_paid, unavailable_order_paid, late_delivery_seller.
- Emit trace events for every evidence used in conclusions.
- Return an AgentResult-compatible dict (plain dict, no custom dataclass needed
  — coordinator/verifier consume it as dict[str, Any]).

Tool names (discovered via `day09 mcp-tools`):
  get_order, get_order_items, get_sellers
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lightweight dataclasses that model the task / result contract.
# ---------------------------------------------------------------------------


@dataclass
class AgentTask:
    """Minimal AgentTask contract for TV2."""

    case_id: str
    order_id: str
    claims: list[dict[str, Any]] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_case(cls, case: dict[str, Any]) -> "AgentTask":
        cr = case.get("customer_request", {})
        return cls(
            case_id=case["case_id"],
            order_id=cr.get("claimed_order_id", ""),
            claims=cr.get("claims", []),
            extras={"policy_version": case.get("policy_version", "")},
        )


@dataclass
class AgentResult:
    """Minimal AgentResult contract for TV2."""

    agent: str
    case_id: str
    primary_issue: str | None
    confidence: float
    evidence_refs: list[str]
    affected_entities: dict[str, list[str]]
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "case_id": self.case_id,
            "primary_issue": self.primary_issue,
            "confidence": self.confidence,
            "evidence_refs": self.evidence_refs,
            "affected_entities": self.affected_entities,
            "details": self.details,
        }


# ---------------------------------------------------------------------------
# Order status categories
# ---------------------------------------------------------------------------
_CANCELED_STATUSES = {"canceled"}
_UNAVAILABLE_STATUSES = {"unavailable", "processing_failed", "invoiced_failed"}
# Seller-side late: order approved/processing but not yet dispatched to logistics
_SELLER_LATE_STATUSES = {"approved", "processing"}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def run(
    task: AgentTask | dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:
    """TV2 specialist run().

    Parameters
    ----------
    task:
        Either an ``AgentTask`` dataclass or a plain ``dict`` (case dict with
        at least ``case_id`` and ``customer_request.claimed_order_id``).
    gateway:
        MCP evidence gateway — calls must use the exact tool names from discovery.
    trace:
        TraceWriter — emit ``tool_result_consumed`` for every evidence used.

    Returns
    -------
    dict
        ``AgentResult.to_dict()`` — consumed by coordinator / TV5 verifier.
    """
    # ------------------------------------------------------------------
    # Normalise input
    # ------------------------------------------------------------------
    if isinstance(task, dict):
        task = AgentTask.from_case(task)

    case_id = task.case_id
    order_id = task.order_id

    collected_refs: list[str] = []
    entities: dict[str, list[str]] = {
        "order_ids": [],
        "item_ids": [],
        "seller_ids": [],
        "payment_references": [],
        "shipment_ids": [],
    }
    details: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Guard: no order_id
    # ------------------------------------------------------------------
    if not order_id:
        return AgentResult(
            agent="order-agent",
            case_id=case_id,
            primary_issue="insufficient_evidence",
            confidence=0.1,
            evidence_refs=[],
            affected_entities=entities,
            details={"error": "no order_id provided in task"},
        ).to_dict()

    # ------------------------------------------------------------------
    # 1. Fetch order
    # ------------------------------------------------------------------
    try:
        order_ev = await gateway.call("get_order", case_id=case_id, order_id=order_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_order failed for %s: %s", case_id, exc)
        return AgentResult(
            agent="order-agent",
            case_id=case_id,
            primary_issue="insufficient_evidence",
            confidence=0.1,
            evidence_refs=[],
            affected_entities=entities,
            details={"error": f"get_order failed: {exc}"},
        ).to_dict()

    order_ref: str = order_ev["evidence_ref"]
    order_data: dict[str, Any] = order_ev.get("data", {}) or {}
    entities["order_ids"].append(order_id)

    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor="order-agent",
        tool_name="get_order",
        evidence_refs=[order_ref],
    )
    collected_refs.append(order_ref)

    order_status: str = (order_data.get("order_status") or "").lower()
    details["order_status"] = order_status

    # ------------------------------------------------------------------
    # 2. Fetch order items (always — needed for seller_ids + item prices)
    # ------------------------------------------------------------------
    items_ref: str | None = None
    items_data: list[dict[str, Any]] = []
    try:
        items_ev = await gateway.call("get_order_items", case_id=case_id, order_id=order_id)
        items_ref = items_ev["evidence_ref"]
        items_data = items_ev.get("data", []) or []

        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="order-agent",
            tool_name="get_order_items",
            evidence_refs=[items_ref],
        )
        collected_refs.append(items_ref)

        for item in items_data:
            if item_id := item.get("order_item_id"):
                val = str(item_id)
                if val not in entities["item_ids"]:
                    entities["item_ids"].append(val)
            if seller_id := item.get("seller_id"):
                val = str(seller_id)
                if val not in entities["seller_ids"]:
                    entities["seller_ids"].append(val)

        details["item_count"] = len(items_data)

    except Exception as exc:  # noqa: BLE001
        logger.warning("get_order_items failed for %s: %s", case_id, exc)
        details["items_error"] = str(exc)

    # ------------------------------------------------------------------
    # 3. Fetch seller info (if we have seller IDs from items)
    # ------------------------------------------------------------------
    sellers_ref: str | None = None
    if entities["seller_ids"]:
        seller_id_param = entities["seller_ids"][0]
        try:
            sellers_ev = await gateway.call(
                "get_sellers", case_id=case_id, seller_id=seller_id_param
            )
            sellers_ref = sellers_ev["evidence_ref"]
            sellers_data = sellers_ev.get("data", {}) or {}

            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="order-agent",
                tool_name="get_sellers",
                evidence_refs=[sellers_ref],
            )
            collected_refs.append(sellers_ref)
            details["seller_info"] = sellers_data

        except Exception as exc:  # noqa: BLE001
            logger.warning("get_sellers failed for %s: %s", case_id, exc)
            details["sellers_error"] = str(exc)

    # ------------------------------------------------------------------
    # 4. Business logic — detect primary issue
    # ------------------------------------------------------------------
    primary_issue: str | None = None
    confidence: float = 0.0
    supporting_refs: list[str] = []

    if order_status in _CANCELED_STATUSES:
        primary_issue = "canceled_order_paid"
        confidence = 0.90
        # Support: order evidence + items evidence (shows items were paid for)
        supporting_refs = [r for r in [order_ref, items_ref] if r is not None]

    elif order_status in _UNAVAILABLE_STATUSES:
        primary_issue = "unavailable_order_paid"
        confidence = 0.85
        supporting_refs = [order_ref]

    elif order_status in _SELLER_LATE_STATUSES:
        # Order stuck at seller before dispatch — seller-side delay
        # Only flag if items confirm seller exists; full logistics blame is TV4's domain
        if entities["seller_ids"] and items_data:
            primary_issue = "late_delivery_seller"
            confidence = 0.70
            supporting_refs = [r for r in [order_ref, items_ref, sellers_ref] if r is not None]
        else:
            primary_issue = "insufficient_evidence"
            confidence = 0.40
            supporting_refs = [order_ref]

    else:
        # delivered, shipped, invoiced, etc. — no order-side issue from TV2's perspective
        primary_issue = None
        confidence = 0.80
        supporting_refs = [order_ref]

    # ------------------------------------------------------------------
    # 5. Claim-level alignment
    # ------------------------------------------------------------------
    claim_verdicts: list[dict[str, Any]] = []
    order_issues = {primary_issue} if primary_issue else set()
    for claim in task.claims:
        claim_id = claim.get("claim_id", "")
        topic = claim.get("topic", "")

        if topic in order_issues:
            verdict = "supported"
            cv_conf = confidence
            cv_refs = supporting_refs[:]
        elif topic in {"canceled_order_paid", "unavailable_order_paid", "late_delivery_seller"}:
            verdict = "unsupported"
            cv_conf = 0.70
            cv_refs = [order_ref]
        elif topic in {"requested_full_refund", "requested_partial_refund"}:
            if primary_issue in {"canceled_order_paid", "unavailable_order_paid"}:
                verdict = "supported"
                cv_conf = min(confidence, 0.85)
                cv_refs = supporting_refs[:]
            else:
                verdict = "partially_supported"
                cv_conf = 0.50
                cv_refs = [order_ref]
        else:
            verdict = "insufficient_evidence"
            cv_conf = 0.30
            cv_refs = [order_ref]

        claim_verdicts.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": cv_conf,
                "evidence_refs": cv_refs,
            }
        )

    details["claim_verdicts"] = claim_verdicts

    # ------------------------------------------------------------------
    # 6. Return
    # ------------------------------------------------------------------
    return AgentResult(
        agent="order-agent",
        case_id=case_id,
        primary_issue=primary_issue,
        confidence=confidence,
        evidence_refs=supporting_refs,
        affected_entities=entities,
        details=details,
    ).to_dict()
