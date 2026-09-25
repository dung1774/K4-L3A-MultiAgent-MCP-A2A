from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter


@dataclass(slots=True)
class AgentResult:
    """A small, serialisable handoff between specialist agents."""

    agent: str
    result: dict[str, Any]
    evidence_refs: list[str] = field(default_factory=list)


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


def _policy_tool(tools: list[str]) -> str | None:
    preferred = ("get_policy", "lookup_policy", "policy_lookup", "get_refund_policy")
    for candidate in preferred:
        if candidate in tools:
            return candidate
    return next((name for name in tools if "polic" in name.lower()), None)


def _topic(case: dict[str, Any], specialist_results: list[AgentResult]) -> str | None:
    claims = _value(case.get("customer_request"), "claims") or []
    topics = [claim.get("topic") for claim in claims if isinstance(claim, dict)]
    for item in specialist_results:
        result = item.result if isinstance(item, AgentResult) else item.get("result", item)
        candidate = _value(result, "primary_issue", "issue", "topic")
        if candidate:
            return str(candidate)
    return next((str(topic) for topic in topics if topic), None)


async def resolve_policy(
    case: dict[str, Any],
    specialist_results: list[AgentResult],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> PolicyDecision:
    """Retrieve the authoritative policy and derive the permitted resolution."""
    case_id = str(case["case_id"])
    tools = await gateway.list_tools()
    tool_name = _policy_tool(tools)
    if tool_name is None:
        raise RuntimeError("MCP tool discovery returned no policy tool")

    policy_version = case.get("policy_version")
    arguments: dict[str, str] = {}
    if policy_version:
        arguments["policy_version"] = str(policy_version)
    topic = _topic(case, specialist_results)
    if topic:
        arguments["issue"] = topic
    evidence = await gateway.call(tool_name, case_id=case_id, **arguments)
    data = evidence.get("data") if isinstance(evidence, dict) else {}
    data = data if isinstance(data, dict) else {"value": data}
    evidence_ref = evidence.get("evidence_ref")
    refs = [evidence_ref] if isinstance(evidence_ref, str) else []
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor="policy-agent",
        tool_name=tool_name,
        evidence_refs=refs,
    )

    eligible = _value(data, "refund_eligible", "eligible_for_refund")
    amount = _value(data, "recommended_refund_brl", "refund_amount_brl", "max_refund_brl")
    try:
        amount = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        amount = None
    decision = PolicyDecision(
        policy_version=_value(data, "policy_version") or policy_version,
        rule=_value(data, "rule", "rule_code", "applied_rule"),
        resolution_action=_value(data, "resolution_action", "action", "recommended_action"),
        refund_eligible=eligible if isinstance(eligible, bool) else None,
        refund_amount_brl=amount,
        evidence_refs=refs,
        raw=data,
    )
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=decision.rule,
        evidence_refs=refs,
    )
    return decision


class PolicyAgent:
    """Compatibility wrapper for callers that prefer an agent object."""

    async def run(self, case, specialist_results, gateway, trace) -> PolicyDecision:
        return await resolve_policy(case, specialist_results, gateway, trace)
