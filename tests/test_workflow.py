from __future__ import annotations

import asyncio
from pathlib import Path

from student_agent.contracts import Contracts
from student_agent.models import AgentResult, VerificationResult
from student_agent import workflow


class FakeTrace:
    def __init__(self) -> None:
        self.events = []

    def emit(self, **kwargs):
        self.events.append(kwargs)
        return kwargs


async def fake_dispatch(task, gateway, trace):
    evidence_map = {
        "order-agent": "ev_11111111111111111111",
        "payment-agent": "ev_22222222222222222222",
        "shipment-agent": "ev_33333333333333333333",
        "policy-agent": "ev_44444444444444444444",
    }

    result = AgentResult(
        case_id=task.case_id,
        task_id=task.task_id,
        actor=task.actor,
        evidence_refs=[evidence_map[task.actor]],
        confidence=0.9,
    )

    if task.actor == "order-agent":
        result.order_ids = ["order-123"]
        result.seller_ids = ["seller-1"]

    if task.actor == "payment-agent":
        result.payment_references = ["payment-1"]

    return result


async def fake_verify(
    case,
    specialist_results,
    gateway,
    trace,
):
    return VerificationResult(
        assessment={
            "primary_issue": "canceled_order_paid",
            "case_status": "action_required",
            "confidence": 0.95,
        },
        root_cause_analysis={
            "ranked_causes": [
                {
                    "cause_code": "ORDER_CANCELED_AFTER_PAYMENT",
                    "rank": 1,
                }
            ],
            "responsible_parties": [
                {
                    "party_type": "seller",
                    "party_id": "seller-1",
                }
            ],
        },
        data_conflicts=[],
        financial_resolution={
            "currency": "BRL",
            "recommended_refund_brl": 100.0,
            "refund_lines": [
                {
                    "reason_code": "CANCELED_ORDER",
                    "amount_brl": 100.0,
                    "entity_id": "order-123",
                }
            ],
        },
        resolution_actions=[
            "issue_full_refund",
        ],
    )


def test_workflow_builds_valid_l3a_output(monkeypatch) -> None:
    monkeypatch.setattr(
        workflow,
        "dispatch_task",
        fake_dispatch,
    )

    monkeypatch.setattr(
        workflow,
        "verify_case",
        fake_verify,
    )

    case = {
        "case_id": "CASE_001",
        "policy_version": "EC_POLICY_V1",
        "customer_request": {
            "claimed_order_id": "order-123",
            "claims": [
                {
                    "claim_id": "claim-1",
                    "topic": "canceled_order_paid",
                },
                {
                    "claim_id": "claim-2",
                    "topic": "requested_full_refund",
                },
            ],
        },
    }

    trace = FakeTrace()

    output = asyncio.run(
        workflow.solve_case(
            case,
            object(),
            trace,
        )
    )

    root = Path(__file__).resolve().parents[1]

    contracts = Contracts(
        root / "contracts" / "schemas"
    )

    contracts.validate_output(
        output,
        "workflow-test",
    )

    assert output["case_id"] == "CASE_001"

    assert output["assessment"]["primary_issue"] == (
        "canceled_order_paid"
    )

    assert output["affected_entities"]["order_ids"] == [
        "order-123"
    ]

    event_types = [
        event["event_type"]
        for event in trace.events
    ]

    assert "task_assigned" in event_types
    assert "handoff" in event_types
    assert "verification_completed" in event_types