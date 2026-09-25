"""Shipment/logistics specialist for the L3A workflow.

This module deliberately separates evidence collection from shipment analysis:
the coordinator supplies queries built from discovered MCP tools, while this
agent consumes only the returned shipment evidence.  It never invents an MCP
tool name or an evidence reference.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..models import AgentResult, AgentTask
from ..trace import TraceWriter

ACTOR = "shipment-agent"
SHIPMENT_TOOL_NAME = "get_shipment_summary"


class ShipmentEvidenceError(ValueError):
    """Raised when a shipment evidence payload cannot be analysed safely."""


@dataclass(frozen=True)
class ShipmentQuery:
    """A query selected from MCP discovery by the coordinator.

    ``arguments`` must not contain ``case_id``.  The case scope is injected by
    :class:`EvidenceGateway`, which prevents a query from overriding it.
    """

    tool_name: str
    arguments: Mapping[str, str]

    def __post_init__(self) -> None:
        if not self.tool_name or len(self.tool_name) > 80:
            raise ValueError("tool_name must contain between 1 and 80 characters")
        if "case_id" in self.arguments:
            raise ValueError("ShipmentQuery.arguments must not override case_id")
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.arguments.items()
        ):
            raise TypeError("ShipmentQuery arguments must be string pairs")


SHIPMENT_ID_FIELDS = ("shipment_id", "shipping_id", "tracking_id")
ORDER_ID_FIELDS = ("order_id",)
SELLER_ID_FIELDS = ("seller_id",)
PROVIDER_FIELDS = (
    "logistics_provider_id",
    "logistics_provider",
    "carrier_id",
    "carrier_name",
    "carrier",
)
STATUS_FIELDS = ("shipping_status", "shipment_status", "delivery_status", "status")
ESTIMATED_DELIVERY_FIELDS = (
    "estimated_delivery_at",
    "estimated_delivery_date",
    "order_estimated_delivery_date",
    "promised_delivery_at",
    "promised_delivery_date",
    "estimated_delivery",
)
ACTUAL_DELIVERY_FIELDS = (
    "actual_delivery_at",
    "actual_delivery_date",
    "order_delivered_customer_date",
    "delivered_customer_at",
    "delivered_at",
    "delivered_date",
)
HANDOFF_FIELDS = (
    "handed_to_carrier_at",
    "carrier_received_at",
    "carrier_pickup_at",
    "order_delivered_carrier_date",
    "delivered_carrier_at",
    "shipped_at",
    "actual_handoff_at",
)
HANDOFF_DEADLINE_FIELDS = (
    "handoff_deadline_at",
    "expected_handoff_at",
    "seller_dispatch_deadline",
    "shipping_limit_date",
    "promised_ship_at",
)
AS_OF_FIELDS = ("as_of", "observed_at", "evaluated_at", "current_time")
NESTED_RECORD_FIELDS = ("shipment", "delivery", "timeline", "timestamps", "logistics")


def _unique_strings(values: Iterable[str | None]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _candidate_maps(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    candidates = [record]
    for field in NESTED_RECORD_FIELDS:
        nested = record.get(field)
        if isinstance(nested, Mapping):
            candidates.append(nested)
    return candidates


def _first(record: Mapping[str, Any], fields: Sequence[str]) -> Any:
    for candidate in _candidate_maps(record):
        for field in fields:
            value = candidate.get(field)
            if value not in (None, ""):
                return value
    return None


def _as_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, Mapping):
        for field in ("provider_id", "carrier_id", "id", "name"):
            nested = value.get(field)
            if nested not in (None, ""):
                return str(nested)
        return None
    return str(value)


def _as_datetime(value: Any, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ShipmentEvidenceError(f"{field} is not a valid ISO-8601 timestamp") from exc
    else:
        raise ShipmentEvidenceError(f"{field} must be an ISO-8601 string or datetime")

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _shipment_records(data: Any) -> list[Mapping[str, Any]]:
    if data is None or data == {}:
        return []
    if isinstance(data, list):
        records = data
    elif isinstance(data, Mapping):
        for collection_field in ("shipments", "shipment_records", "results", "items"):
            collection = data.get(collection_field)
            if isinstance(collection, list):
                records = collection
                break
        else:
            nested = data.get("shipment")
            records = [nested] if isinstance(nested, Mapping) else [data]
    else:
        raise ShipmentEvidenceError("shipment evidence data must be an object or array")

    if any(not isinstance(record, Mapping) for record in records):
        raise ShipmentEvidenceError("every shipment record must be an object")
    return records


def _classify_record(
    record: Mapping[str, Any], evidence_ref: str, default_as_of: datetime | None
) -> dict[str, Any]:
    shipment_id = _as_text(_first(record, SHIPMENT_ID_FIELDS))
    order_id = _as_text(_first(record, ORDER_ID_FIELDS))
    seller_id = _as_text(_first(record, SELLER_ID_FIELDS))
    provider = _as_text(_first(record, PROVIDER_FIELDS))
    status = _as_text(_first(record, STATUS_FIELDS))

    estimated = _as_datetime(
        _first(record, ESTIMATED_DELIVERY_FIELDS), "estimated delivery"
    )
    delivered = _as_datetime(_first(record, ACTUAL_DELIVERY_FIELDS), "actual delivery")
    handed_over = _as_datetime(_first(record, HANDOFF_FIELDS), "carrier handoff")
    handoff_deadline = _as_datetime(
        _first(record, HANDOFF_DEADLINE_FIELDS), "carrier handoff deadline"
    )
    record_as_of = _as_datetime(_first(record, AS_OF_FIELDS), "shipment as_of")
    evaluation_time = delivered or record_as_of or default_as_of

    is_late: bool | None
    late_by_days: float | None = None
    attribution: str | None = None

    if estimated is None:
        is_late = None
        reason_code = "MISSING_ESTIMATED_DELIVERY"
    elif evaluation_time is None:
        is_late = None
        reason_code = "UNDELIVERED_WITHOUT_AS_OF"
    else:
        seconds_late = (evaluation_time - estimated).total_seconds()
        is_late = seconds_late > 0
        if is_late:
            late_by_days = round(seconds_late / 86_400, 3)
            if handoff_deadline is None:
                attribution = "unknown"
                reason_code = "LATE_WITHOUT_HANDOFF_DEADLINE"
            elif handed_over is None:
                attribution = "unknown"
                reason_code = "LATE_WITHOUT_HANDOFF_TIMESTAMP"
            elif handed_over > handoff_deadline:
                attribution = "seller"
                reason_code = "SELLER_HANDED_OVER_AFTER_DEADLINE"
            else:
                attribution = "logistics_provider"
                reason_code = "CARRIER_RECEIVED_ON_TIME_DELIVERED_LATE"
        elif delivered is not None:
            reason_code = "DELIVERED_ON_TIME"
        else:
            reason_code = "IN_TRANSIT_WITHIN_ESTIMATE"

    return {
        "shipment_id": shipment_id,
        "order_id": order_id,
        "seller_id": seller_id,
        "logistics_provider": provider,
        "shipping_status": status,
        "estimated_delivery_at": _iso(estimated),
        "actual_delivery_at": _iso(delivered),
        "handed_to_carrier_at": _iso(handed_over),
        "handoff_deadline_at": _iso(handoff_deadline),
        "is_late": is_late,
        "late_by_days": late_by_days,
        "delay_attribution": attribution,
        "reason_code": reason_code,
        "evidence_refs": [evidence_ref],
    }


def analyze_shipment_evidence(
    evidence: Sequence[Mapping[str, Any]], *, as_of: datetime | str | None = None
) -> dict[str, Any]:
    """Classify shipment delays using authoritative MCP evidence.

    A late delivery is attributed to the seller only when the carrier handoff
    happened after its deadline.  It is attributed to logistics only when the
    handoff was on time and the customer delivery was late.  A generic ``late``
    status is retained for context but is never enough by itself.
    """

    default_as_of = _as_datetime(as_of, "as_of")
    findings: list[dict[str, Any]] = []
    evidence_refs: list[str] = []

    for response in evidence:
        if not isinstance(response, Mapping):
            raise ShipmentEvidenceError("each evidence response must be an object")
        if response.get("domain") != "shipment":
            raise ShipmentEvidenceError("shipment agent only accepts shipment-domain evidence")
        evidence_ref = response.get("evidence_ref")
        if not isinstance(evidence_ref, str) or not evidence_ref:
            raise ShipmentEvidenceError("shipment evidence is missing evidence_ref")
        evidence_refs.append(evidence_ref)
        data = response.get("data")
        direct_events = data.get("events") if isinstance(data, Mapping) else None
        delivered_at = (
            _as_datetime(data.get("delivered_customer_at"), "delivered_customer_at")
            if isinstance(data, Mapping)
            else None
        )
        late_events = [
            event
            for event in direct_events or []
            if isinstance(event, Mapping)
            and str(event.get("event_type", "")).lower() == "delivered_late"
            and str(event.get("status", "")).lower() in {"confirmed", "completed"}
            and delivered_at is not None
            and _as_datetime(event.get("event_at"), "shipment event_at") == delivered_at
        ]
        if late_events and isinstance(data, Mapping):
            shipping_limits = data.get("shipping_limits")
            seller_id = None
            if isinstance(shipping_limits, list):
                seller_id = next(
                    (
                        _as_text(item.get("seller_id"))
                        for item in shipping_limits
                        if isinstance(item, Mapping) and item.get("seller_id")
                    ),
                    None,
                )
            for event in late_events:
                actor = str(event.get("actor", "")).lower()
                attribution = (
                    "seller"
                    if actor == "seller"
                    else "logistics_provider"
                    if actor in {"logistics", "logistics_provider", "carrier"}
                    else "unknown"
                )
                findings.append(
                    {
                        "shipment_id": _as_text(data.get("shipment_id")),
                        "order_id": _as_text(data.get("order_id")),
                        "seller_id": seller_id,
                        "logistics_provider": _as_text(event.get("provider_id")),
                        "shipping_status": _as_text(data.get("order_status")),
                        "estimated_delivery_at": _as_text(data.get("estimated_delivery_at")),
                        "actual_delivery_at": _as_text(data.get("delivered_customer_at")),
                        "handed_to_carrier_at": _as_text(data.get("delivered_carrier_at")),
                        "handoff_deadline_at": None,
                        "is_late": True,
                        "late_by_days": None,
                        "delay_attribution": attribution,
                        "reason_code": "CONFIRMED_DELIVERED_LATE_EVENT",
                        "evidence_refs": [evidence_ref],
                    }
                )
            continue
        for record in _shipment_records(data):
            findings.append(_classify_record(record, evidence_ref, default_as_of))

    seller_delays = [item for item in findings if item["delay_attribution"] == "seller"]
    logistics_delays = [
        item for item in findings if item["delay_attribution"] == "logistics_provider"
    ]
    unresolved_delays = [
        item
        for item in findings
        if item["is_late"] is True and item["delay_attribution"] == "unknown"
    ]
    undetermined = [item for item in findings if item["is_late"] is None]

    if seller_delays and logistics_delays:
        decision_code = "SHIPMENT_MIXED_DELAY"
        recommended_issue = None
    elif seller_delays:
        decision_code = "SHIPMENT_SELLER_DELAY"
        recommended_issue = "late_delivery_seller"
    elif logistics_delays:
        decision_code = "SHIPMENT_LOGISTICS_DELAY"
        recommended_issue = "late_delivery_logistics"
    elif unresolved_delays or undetermined or not findings:
        decision_code = "SHIPMENT_ATTRIBUTION_UNRESOLVED"
        recommended_issue = "insufficient_evidence"
    else:
        decision_code = "SHIPMENT_NO_LATE_DELIVERY"
        recommended_issue = None

    party_keys = [
        *(("seller", finding["seller_id"]) for finding in seller_delays),
        *(
            ("logistics_provider", finding["logistics_provider"])
            for finding in logistics_delays
        ),
    ]
    responsible_parties = [
        {"party_type": party_type, "party_id": party_id}
        for party_type, party_id in dict.fromkeys(party_keys)
    ]

    return {
        "findings": findings,
        "entities": {
            "shipment_ids": _unique_strings(item["shipment_id"] for item in findings),
            "order_ids": _unique_strings(item["order_id"] for item in findings),
            "seller_ids": _unique_strings(item["seller_id"] for item in findings),
            "logistics_providers": _unique_strings(
                item["logistics_provider"] for item in findings
            ),
        },
        "evidence_refs": _unique_strings(evidence_refs),
        "responsible_parties": responsible_parties,
        "recommended_primary_issue": recommended_issue,
        "decision_code": decision_code,
        "summary": {
            "shipments_checked": len(findings),
            "late_shipments": len(seller_delays) + len(logistics_delays) + len(unresolved_delays),
            "seller_delays": len(seller_delays),
            "logistics_delays": len(logistics_delays),
            "unresolved_delays": len(unresolved_delays) + len(undetermined),
        },
    }


async def run_shipment_agent(
    *,
    case_id: str,
    queries: Sequence[ShipmentQuery],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    as_of: datetime | str | None = None,
    handoff_target: str = "coordinator",
    task_id: str | None = None,
) -> dict[str, Any]:
    """Collect shipment evidence for explicit discovered-tool queries and analyse it."""

    if not case_id:
        raise ValueError("case_id must not be empty")

    responses: list[Mapping[str, Any]] = []
    seen_queries: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    for query in queries:
        fingerprint = (query.tool_name, tuple(sorted(query.arguments.items())))
        if fingerprint in seen_queries:
            continue
        seen_queries.add(fingerprint)

        response = await gateway.call(query.tool_name, case_id=case_id, **query.arguments)
        evidence_ref = response["evidence_ref"]
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=ACTOR,
            tool_name=query.tool_name,
            evidence_refs=[evidence_ref],
            attributes={"task_id": task_id} if task_id is not None else None,
        )
        responses.append(response)

    result = analyze_shipment_evidence(responses, as_of=as_of)
    handoff_attributes: dict[str, str | int] = dict(result["summary"])
    if task_id is not None:
        handoff_attributes["task_id"] = task_id
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=ACTOR,
        target=handoff_target,
        decision_code=result["decision_code"],
        evidence_refs=result["evidence_refs"],
        attributes=handoff_attributes,
    )
    return result


async def run(
    task: AgentTask,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> AgentResult:
    """Run the TV4 specialist through the team's common agent interface."""

    if task.actor != ACTOR:
        raise ValueError(f"shipment agent received task for actor {task.actor!r}")
    order_id = task.context.get("claimed_order_id")
    if not isinstance(order_id, str) or not order_id.strip():
        return AgentResult(
            task_id=task.task_id,
            case_id=task.case_id,
            actor=ACTOR,
            findings={"recommended_issue": "insufficient_evidence"},
            errors=["Shipment lookup requires a resolved order_id."],
        )
    order_id = order_id.strip()
    as_of = task.context.get("opened_at")
    if as_of is not None and not isinstance(as_of, (str, datetime)):
        raise TypeError("AgentTask.context['opened_at'] must be an ISO-8601 string or datetime")

    errors: list[str] = []
    try:
        response = await gateway.call(
            SHIPMENT_TOOL_NAME,
            case_id=task.case_id,
            order_id=order_id,
        )
        evidence_ref = response["evidence_ref"]
        trace.emit(
            case_id=task.case_id,
            event_type="tool_result_consumed",
            actor=ACTOR,
            tool_name=SHIPMENT_TOOL_NAME,
            evidence_refs=[evidence_ref],
            attributes={"task_id": task.task_id},
        )
        result = analyze_shipment_evidence([response], as_of=as_of)
    except (RuntimeError, ValueError, KeyError, TypeError) as exc:
        errors.append(f"required {SHIPMENT_TOOL_NAME} failed: {exc}")
        result = {
            "findings": [],
            "entities": {"shipment_ids": [], "order_ids": [], "seller_ids": []},
            "evidence_refs": [],
            "responsible_parties": [],
            "recommended_primary_issue": "insufficient_evidence",
            "decision_code": "SHIPMENT_EVIDENCE_UNAVAILABLE",
            "summary": {"shipments_checked": 0, "late_shipments": 0},
        }
    return AgentResult(
        task_id=task.task_id,
        case_id=task.case_id,
        actor=ACTOR,
        findings={
            "shipments": result["findings"],
            "responsible_parties": result["responsible_parties"],
            "recommended_issue": result["recommended_primary_issue"],
            "decision_code": result["decision_code"],
            "summary": result["summary"],
        },
        order_ids=result["entities"].get("order_ids", []),
        seller_ids=result["entities"].get("seller_ids", []),
        shipment_ids=result["entities"].get("shipment_ids", []),
        evidence_refs=result["evidence_refs"],
        confidence=(
            0.85
            if result["recommended_primary_issue"] not in {None, "insufficient_evidence"}
            else 0.4
        ),
        errors=errors,
    )


__all__ = [
    "ACTOR",
    "SHIPMENT_TOOL_NAME",
    "AgentResult",
    "AgentTask",
    "ShipmentEvidenceError",
    "ShipmentQuery",
    "analyze_shipment_evidence",
    "run",
    "run_shipment_agent",
]
