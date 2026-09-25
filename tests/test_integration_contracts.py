from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from student_agent import dispatcher, workflow
from student_agent.agents.policy_agent import run as run_policy
from student_agent.models import AgentResult, AgentTask, VerificationResult
from student_agent.verifier import verify_case


def _result(actor: str, refs: list[str], **findings) -> AgentResult:
    return AgentResult(
        case_id="CASE_001",
        task_id=f"CASE_001:{actor}",
        actor=actor,
        findings=findings,
        evidence_refs=refs,
        order_ids=["order-1"] if actor == "order-agent" else [],
        confidence=0.9,
    )


def test_dispatcher_rejects_mismatched_case_id(monkeypatch) -> None:
    async def bad_run(task, gateway, trace):
        return AgentResult(
            case_id="CASE_OTHER",
            task_id=task.task_id,
            actor=task.actor,
        )

    monkeypatch.setattr(
        dispatcher.importlib,
        "import_module",
        lambda name: SimpleNamespace(run=bad_run),
    )
    task = AgentTask("CASE_001", "task-1", "order-agent", "verify")
    with pytest.raises(ValueError, match="mismatched case_id"):
        asyncio.run(dispatcher.dispatch_task(task, Mock(), Mock()))


def test_verifier_evidence_selection_deduplicates_and_rejects_unknown() -> None:
    ref = "ev_" + "a" * 20
    results = [_result("order-agent", [ref, ref])]
    verification = VerificationResult(
        assessment={},
        root_cause_analysis={},
        data_conflicts=[],
        financial_resolution={},
        resolution_actions=[],
        evidence_refs=[ref, ref],
    )
    assert workflow._collect_evidence_refs(results, verification) == [ref]
    verification.evidence_refs = ["ev_" + "z" * 20]
    with pytest.raises(ValueError, match="not produced by specialists"):
        workflow._collect_evidence_refs(results, verification)


def test_policy_agent_uses_shared_contract_and_emits_policy_decision() -> None:
    gateway = AsyncMock()
    gateway.call.return_value = {
        "evidence_ref": "ev_" + "p" * 20,
        "data": {"policy_version": "EC_POLICY_V1", "rules": {}},
    }
    task = AgentTask(
        "CASE_001",
        "CASE_001:policy-agent",
        "policy-agent",
        "policy",
        {"policy_version": "EC_POLICY_V1"},
    )
    trace = Mock()
    result = asyncio.run(run_policy(task, gateway, trace))
    assert isinstance(result, AgentResult)
    assert result.actor == "policy-agent"
    assert result.evidence_refs == ["ev_" + "p" * 20]
    assert [call.kwargs["event_type"] for call in trace.emit.call_args_list] == [
        "tool_result_consumed",
        "policy_decided",
    ]


def test_verifier_financial_consistency_and_policy_mapping() -> None:
    order_ref = "ev_" + "o" * 20
    payment_ref = "ev_" + "m" * 20
    policy_ref = "ev_" + "p" * 20
    results = [
        _result(
            "order-agent",
            [order_ref],
            recommended_issue="canceled_order_paid",
        ),
        _result(
            "payment-agent",
            [payment_ref],
            payment_verdict="payment_reconciled",
            captured_total_brl=79.0,
        ),
        _result(
            "policy-agent",
            [policy_ref],
            policy={
                "rules": {
                    "canceled_order_paid": {
                        "case_status": "action_required",
                        "recommended_action": "issue_refund",
                        "refund_brl": 79.0,
                        "responsible_parties": [
                            {"party_type": "platform", "party_id": None}
                        ],
                    }
                }
            },
        ),
    ]
    case = {
        "case_id": "CASE_001",
        "customer_request": {
            "claims": [
                {"claim_id": "claim-a", "topic": "canceled_order_paid"},
                {"claim_id": "claim-b", "topic": "requested_full_refund"},
            ]
        },
    }
    verification = asyncio.run(verify_case(case, results, Mock(), Mock()))
    assert verification.assessment["primary_issue"] == "canceled_order_paid"
    assert verification.resolution_actions == ["issue_refund"]
    assert verification.financial_resolution["recommended_refund_brl"] == 79.0
    assert sum(
        line["amount_brl"]
        for line in verification.financial_resolution["refund_lines"]
    ) == 79.0
    assert verification.evidence_refs == [order_ref, payment_ref, policy_ref]
