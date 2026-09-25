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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..mcp_gateway import EvidenceGateway
    from ..trace import TraceWriter

ACTOR = "shipment-agent"
SHIPMENT_TOOL_NAME = "get_shipment_summary"


class ShipmentEvidenceError(ValueError):
    """Raised when a shipment evidence payload cannot be analysed safely."""


@dataclass(frozen=True)
class AgentTask:
    """Temporary shared-task envelope until TV1 centralizes team contracts.

    Shipment tasks require ``payload["order_id"]``.  They may also provide
    ``payload["as_of"]`` and ``payload["handoff_target"]``.
    """

    task_id: str
    case_id: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("task_id must not be empty")
        if not self.case_id:
            raise ValueError("case_id must not be empty")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping")


@dataclass(frozen=True)
class AgentResult:
    """JSON-friendly specialist result returned to the coordinator/verifier."""

    task_id: str
    case_id: str
    agent: str
    status: str
    findings: list[dict[str, Any]]
    entities: dict[str, list[str]]
    evidence_refs: list[str]
    responsible_parties: list[dict[str, Any]]
    recommended_primary_issue: str | None
    decision_code: str
    summary: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        """Return a plain mapping for assembly or A2A serialization."""

        return {
            "task_id": self.task_id,
            "case_id": self.case_id,
            "agent": self.agent,
            "status": self.status,
            "findings": self.findings,
            "entities": self.entities,
            "evidence_refs": self.evidence_refs,
            "responsible_parties": self.responsible_parties,
            "recommended_primary_issue": self.recommended_primary_issue,
            "decision_code": self.decision_code,
            "summary": self.summary,
        }


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
    "delivered_at",
    "delivered_date",
)
HANDOFF_FIELDS = (
    "handed_to_carrier_at",
    "carrier_received_at",
    "carrier_pickup_at",
    "order_delivered_carrier_date",
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
        for record in _shipment_records(response.get("data")):
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


def _required_task_text(task: AgentTask, field: str) -> str:
    value = task.payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"AgentTask.payload[{field!r}] must be a non-empty string")
    return value.strip()


async def run(
    task: AgentTask,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> AgentResult:
    """Run the TV4 specialist through the team's common agent interface."""

    order_id = _required_task_text(task, "order_id")
    as_of = task.payload.get("as_of")
    if as_of is not None and not isinstance(as_of, (str, datetime)):
        raise TypeError("AgentTask.payload['as_of'] must be an ISO-8601 string or datetime")
    handoff_target = task.payload.get("handoff_target", "coordinator")
    if not isinstance(handoff_target, str) or not handoff_target:
        raise ValueError("AgentTask.payload['handoff_target'] must be a non-empty string")

    result = await run_shipment_agent(
        case_id=task.case_id,
        queries=[ShipmentQuery(SHIPMENT_TOOL_NAME, {"order_id": order_id})],
        gateway=gateway,
        trace=trace,
        as_of=as_of,
        handoff_target=handoff_target,
        task_id=task.task_id,
    )
    status = (
        "needs_investigation"
        if result["decision_code"] == "SHIPMENT_ATTRIBUTION_UNRESOLVED"
        else "completed"
    )
    return AgentResult(
        task_id=task.task_id,
        case_id=task.case_id,
        agent=ACTOR,
        status=status,
        findings=result["findings"],
        entities=result["entities"],
        evidence_refs=result["evidence_refs"],
        responsible_parties=result["responsible_parties"],
        recommended_primary_issue=result["recommended_primary_issue"],
        decision_code=result["decision_code"],
        summary=result["summary"],
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
