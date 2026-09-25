"""Business-policy specialist adapted from TV5 to the shared agent contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..models import AgentResult, AgentTask
from ..trace import TraceWriter

ACTOR = "policy-agent"


@dataclass(slots=True)
class PolicyDecision:
    policy_version: str | None
    rule: str | None
    resolution_action: str | None
    refund_eligible: bool | None
    refund_amount_brl: float | None
    evidence_refs: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def _value(data: Any, *names: str) -> Any:
    if not isinstance(data, dict):
        return None
    for name in names:
        if name in data:
            return data[name]
    return None


def _normalize_decision(data: Any, policy_version: str | None, ref: str) -> PolicyDecision:
    raw = data if isinstance(data, dict) else {"value": data}
    eligible = _value(raw, "refund_eligible", "eligible_for_refund")
    amount = _value(raw, "recommended_refund_brl", "refund_amount_brl", "max_refund_brl")
    try:
        normalized_amount = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        normalized_amount = None
    return PolicyDecision(
        policy_version=_value(raw, "policy_version") or policy_version,
        rule=_value(raw, "rule", "rule_code", "applied_rule", "decision_code"),
        resolution_action=_value(
            raw,
            "resolution_action",
            "action",
            "recommended_action",
        ),
        refund_eligible=eligible if isinstance(eligible, bool) else None,
        refund_amount_brl=normalized_amount,
        evidence_refs=[ref],
        raw=raw,
    )


async def run(
    task: AgentTask,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> AgentResult:
    if task.actor != ACTOR:
        raise ValueError(f"policy agent received task for actor {task.actor!r}")
    policy_version = task.context.get("policy_version")
    if not isinstance(policy_version, str) or not policy_version.strip():
        return AgentResult(
            case_id=task.case_id,
            task_id=task.task_id,
            actor=ACTOR,
            findings={"policy_decision": None},
            errors=["Policy lookup requires policy_version."],
        )

    try:
        evidence = await gateway.call(
            "get_policy",
            case_id=task.case_id,
            policy_version=policy_version,
        )
        evidence_ref = evidence["evidence_ref"]
        trace.emit(
            case_id=task.case_id,
            event_type="tool_result_consumed",
            actor=ACTOR,
            tool_name="get_policy",
            evidence_refs=[evidence_ref],
            attributes={"task_id": task.task_id},
        )
        decision = _normalize_decision(evidence.get("data"), policy_version, evidence_ref)
        trace.emit(
            case_id=task.case_id,
            event_type="policy_decided",
            actor=ACTOR,
            decision_code=decision.rule or "POLICY_RETRIEVED",
            evidence_refs=[evidence_ref],
            attributes={"task_id": task.task_id},
        )
        return AgentResult(
            case_id=task.case_id,
            task_id=task.task_id,
            actor=ACTOR,
            findings={
                "policy_version": decision.policy_version,
                "rule": decision.rule,
                "resolution_action": decision.resolution_action,
                "refund_eligible": decision.refund_eligible,
                "refund_amount_brl": decision.refund_amount_brl,
                "policy": decision.raw,
            },
            evidence_refs=[evidence_ref],
            confidence=0.9,
        )
    except (RuntimeError, ValueError, KeyError, TypeError) as exc:
        trace.emit(
            case_id=task.case_id,
            event_type="policy_decided",
            actor=ACTOR,
            decision_code="POLICY_UNAVAILABLE",
            attributes={"task_id": task.task_id},
        )
        return AgentResult(
            case_id=task.case_id,
            task_id=task.task_id,
            actor=ACTOR,
            findings={"policy_decision": None},
            confidence=0.0,
            errors=[f"required get_policy failed: {exc}"],
        )


async def resolve_policy(
    case: dict[str, Any],
    specialist_results: list[AgentResult],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> PolicyDecision:
    """Compatibility adapter for the original TV5 entry point."""

    del specialist_results
    request = case.get("customer_request")
    order_id = request.get("claimed_order_id") if isinstance(request, dict) else None
    task = AgentTask(
        case_id=str(case["case_id"]),
        task_id=f"{case['case_id']}:policy-agent",
        actor=ACTOR,
        objective="Retrieve applicable business policy.",
        context={
            "policy_version": case.get("policy_version"),
            "claimed_order_id": order_id,
        },
    )
    result = await run(task, gateway, trace)
    finding = result.findings
    return PolicyDecision(
        policy_version=finding.get("policy_version"),
        rule=finding.get("rule"),
        resolution_action=finding.get("resolution_action"),
        refund_eligible=finding.get("refund_eligible"),
        refund_amount_brl=finding.get("refund_amount_brl"),
        evidence_refs=result.evidence_refs,
        raw=finding.get("policy") if isinstance(finding.get("policy"), dict) else {},
    )


class PolicyAgent:
    async def run(
        self,
        task: AgentTask,
        gateway: EvidenceGateway,
        trace: TraceWriter,
    ) -> AgentResult:
        return await run(task, gateway, trace)


__all__ = ["ACTOR", "PolicyAgent", "PolicyDecision", "resolve_policy", "run"]
