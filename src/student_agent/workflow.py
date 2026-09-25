from __future__ import annotations

from typing import Any

from .coordinator import (
    emit_task_assignments,
    emit_verifier_handoff,
    plan_tasks,
)
from .dispatcher import dispatch_task
from .mcp_gateway import EvidenceGateway
from .models import AgentResult, VerificationResult
from .trace import TraceWriter
from .verification import verify_case


def _unique(values: list[str]) -> list[str]:
    """Preserve order while removing duplicates."""

    return list(dict.fromkeys(values))

def _collect_evidence_refs(
    specialist_results: list[AgentResult],
    verification: VerificationResult,
) -> list[str]:
    available_refs = {
        ref
        for result in specialist_results
        for ref in result.evidence_refs
    }

    selected_refs = _unique(verification.evidence_refs)

    unknown_refs = [
        ref
        for ref in selected_refs
        if ref not in available_refs
    ]

    if unknown_refs:
        raise ValueError(
            f"Verifier selected evidence not produced by specialists: "
            f"{unknown_refs}"
        )

    if len(selected_refs) > 30:
        raise ValueError(
            "Verifier selected more than 30 evidence refs"
        )

    return selected_refs


def _collect_entities(
    specialist_results: list[AgentResult],
) -> dict[str, list[str]]:
    order_ids: list[str] = []
    item_ids: list[str] = []
    seller_ids: list[str] = []
    payment_references: list[str] = []
    shipment_ids: list[str] = []

    for result in specialist_results:
        order_ids.extend(result.order_ids)
        item_ids.extend(result.item_ids)
        seller_ids.extend(result.seller_ids)
        payment_references.extend(result.payment_references)
        shipment_ids.extend(result.shipment_ids)

    return {
        "order_ids": _unique(order_ids),
        "item_ids": _unique(item_ids),
        "seller_ids": _unique(seller_ids),
        "payment_references": _unique(payment_references),
        "shipment_ids": _unique(shipment_ids),
    }


def build_output(
    *,
    case: dict[str, Any],
    specialist_results: list[AgentResult],
    verification: VerificationResult,
) -> dict[str, Any]:
    """
    Convert verified multi-agent results into the public L3A output contract.
    """

    case_id = case.get("case_id")

    if not isinstance(case_id, str) or not case_id:
        raise ValueError("case is missing a valid case_id")

    output: dict[str, Any] = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": verification.assessment,
        "affected_entities": _collect_entities(specialist_results),
        "root_cause_analysis": verification.root_cause_analysis,
        "evidence_refs": _collect_evidence_refs(
            specialist_results,
            verification,
        ),
        "data_conflicts": verification.data_conflicts,
        "financial_resolution": verification.financial_resolution,
        "resolution_actions": verification.resolution_actions,
    }

    if verification.claim_assessments:
        output["claim_assessments"] = verification.claim_assessments

    return output


async def solve_case(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:
    """
    L3A multi-agent orchestration.

    Flow:
        Coordinator
            -> specialist tasks
            -> collect specialist results
            -> verifier
            -> schema-ready output
    """

    case_id = case.get("case_id")

    if not isinstance(case_id, str) or not case_id:
        raise ValueError("case is missing a valid case_id")

    # ---------------------------------------------------------
    # 1. Coordinator decides which specialists are required.
    # ---------------------------------------------------------

    tasks = plan_tasks(case)

    if not tasks:
        raise RuntimeError(
            f"No specialist tasks were planned for case {case_id}"
        )

    emit_task_assignments(tasks, trace)

    # ---------------------------------------------------------
    # 2. Run specialists.
    #
    # Sequential execution is intentional for the first version.
    # Correctness and observable trace are more important than
    # concurrency for L3A.
    # ---------------------------------------------------------

    specialist_results: list[AgentResult] = []

    for task in tasks:
        result = await dispatch_task(
            task,
            gateway,
            trace,
        )

        specialist_results.append(result)

        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=result.actor,
            target="coordinator",
            decision_code="SPECIALIST_RESULT_READY",
            evidence_refs=(
                result.evidence_refs
                if result.evidence_refs
                else None
            ),
            attributes={
                "task_id": result.task_id,
                "confidence": result.confidence,
                "error_count": len(result.errors),
            },
        )

    # ---------------------------------------------------------
    # 3. Coordinator sends consolidated results to verifier.
    # ---------------------------------------------------------

    specialist_evidence = _unique(
        [
            evidence_ref
            for result in specialist_results
            for evidence_ref in result.evidence_refs
        ]
    )

    emit_verifier_handoff(
        case_id=case_id,
        result_count=len(specialist_results),
        evidence_count=len(specialist_evidence),
        trace=trace,
    )

    # ---------------------------------------------------------
    # 4. Independent verification.
    # ---------------------------------------------------------

    verification = await verify_case(
        case,
        specialist_results,
        gateway,
        trace,
    )

    verification_evidence = _collect_evidence_refs(
        specialist_results,
        verification,
    )

    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        target="coordinator",
        decision_code="VERIFICATION_COMPLETED",
        evidence_refs=(
            verification_evidence
            if verification_evidence
            else None
        ),
        attributes={
            "specialist_results": len(specialist_results),
        },
    )

    # ---------------------------------------------------------
    # 5. Build public output.
    # Validation is performed by cli.py after solve_case().
    # ---------------------------------------------------------

    return build_output(
        case=case,
        specialist_results=specialist_results,
        verification=verification,
    )