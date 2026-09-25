from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


SpecialistActor = Literal[
    "order-agent",
    "payment-agent",
    "shipment-agent",
    "policy-agent",
]


@dataclass(slots=True)
class AgentTask:
    """Task envelope sent from the coordinator to a specialist."""

    case_id: str
    task_id: str
    actor: SpecialistActor
    objective: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentResult:
    """Common result envelope returned by every specialist."""

    case_id: str
    task_id: str
    actor: str

    findings: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)

    order_ids: list[str] = field(default_factory=list)
    item_ids: list[str] = field(default_factory=list)
    seller_ids: list[str] = field(default_factory=list)
    payment_references: list[str] = field(default_factory=list)
    shipment_ids: list[str] = field(default_factory=list)

    confidence: float = 0.0
    errors: list[str] = field(default_factory=list)

@dataclass(slots=True)
class VerificationResult:
    """Schema-ready semantic result returned by the verifier."""

    assessment: dict[str, Any]
    root_cause_analysis: dict[str, Any]
    data_conflicts: list[dict[str, Any]]
    financial_resolution: dict[str, Any]
    resolution_actions: list[str]

    claim_assessments: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
