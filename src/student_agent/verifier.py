"""Cross-domain verifier adapted from TV5 for the shared result contract."""

from __future__ import annotations

from typing import Any

from .mcp_gateway import EvidenceGateway
from .models import AgentResult, VerificationResult
from .trace import TraceWriter

PAYMENT_ISSUES = {
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
}


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _by_actor(results: list[AgentResult], actor: str) -> AgentResult | None:
    return next((result for result in results if result.actor == actor), None)


def _topics(case: dict[str, Any]) -> list[tuple[str, str]]:
    request = case.get("customer_request")
    claims = request.get("claims") if isinstance(request, dict) else None
    if not isinstance(claims, list):
        return []
    return [
        (str(claim.get("claim_id", "")), str(claim.get("topic", "")))
        for claim in claims
        if isinstance(claim, dict) and claim.get("claim_id")
    ]


def _select_issue(results: list[AgentResult]) -> tuple[str, float]:
    order = _by_actor(results, "order-agent")
    payment = _by_actor(results, "payment-agent")
    shipment = _by_actor(results, "shipment-agent")

    order_issue = order.findings.get("recommended_issue") if order else None
    payment_issue = payment.findings.get("payment_verdict") if payment else None
    shipment_issue = shipment.findings.get("recommended_issue") if shipment else None
    captured = payment.findings.get("captured_total_brl") if payment else None

    if order_issue in {"canceled_order_paid", "unavailable_order_paid"}:
        if isinstance(captured, (int, float)) and captured > 0:
            return str(order_issue), min(order.confidence, payment.confidence)
        return "insufficient_evidence", 0.25
    if payment_issue in {"refund_pending", "refund_failed", "duplicate_charge"}:
        return str(payment_issue), payment.confidence
    if shipment_issue in {"late_delivery_seller", "late_delivery_logistics"}:
        return str(shipment_issue), shipment.confidence
    if payment_issue in PAYMENT_ISSUES:
        return str(payment_issue), payment.confidence

    required_failures = [
        error
        for result in results
        for error in result.errors
        if error.startswith("required ") or error.startswith("invalid specialist")
    ]
    if required_failures or not order or not payment:
        return "insufficient_evidence", 0.2
    if shipment and shipment.findings.get("recommended_issue") == "insufficient_evidence":
        return "insufficient_evidence", 0.3
    return "unsupported_claim", 0.82


def _policy_rule(policy: AgentResult | None, issue: str) -> dict[str, Any]:
    if policy is None:
        return {}
    raw = policy.findings.get("policy")
    rules = raw.get("rules") if isinstance(raw, dict) else None
    rule = rules.get(issue) if isinstance(rules, dict) else None
    return rule if isinstance(rule, dict) else {}


def _selected_refs(results: list[AgentResult], issue: str) -> list[str]:
    actors = {"order-agent", "payment-agent", "policy-agent"}
    if issue in {"late_delivery_seller", "late_delivery_logistics", "unsupported_claim"}:
        actors.add("shipment-agent")
    if issue == "insufficient_evidence":
        actors = {result.actor for result in results if result.evidence_refs}
    return _unique([
        ref
        for result in results
        if result.actor in actors
        for ref in result.evidence_refs
    ])


def _entities(results: list[AgentResult]) -> dict[str, list[str]]:
    return {
        name: _unique([
            value
            for result in results
            for value in getattr(result, name)
        ])
        for name in (
            "order_ids",
            "item_ids",
            "seller_ids",
            "payment_references",
            "shipment_ids",
        )
    }


def _financial(issue: str, rule: dict[str, Any], order_id: str | None) -> dict[str, Any]:
    raw_amount = rule.get("refund_brl", 0)
    try:
        amount = max(float(raw_amount), 0.0)
    except (TypeError, ValueError):
        amount = 0.0
    lines = []
    if amount > 0:
        lines.append(
            {
                "reason_code": issue.upper(),
                "amount_brl": amount,
                "entity_id": order_id,
            }
        )
    return {
        "currency": "BRL",
        "recommended_refund_brl": amount,
        "refund_lines": lines,
    }


def _claim_assessments(
    case: dict[str, Any], issue: str, confidence: float, refs: list[str]
) -> list[dict[str, Any]]:
    assessments = []
    for claim_id, topic in _topics(case):
        if topic == issue:
            verdict = "supported"
            claim_confidence = confidence
        elif topic == "requested_full_refund":
            if issue not in {
                "valid_split_payment",
                "unsupported_claim",
                "insufficient_evidence",
                "refund_pending",
            }:
                verdict = "supported"
            elif issue in {"valid_split_payment", "unsupported_claim"}:
                verdict = "unsupported"
            else:
                verdict = "insufficient_evidence"
            claim_confidence = confidence
        elif issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
            claim_confidence = confidence
        else:
            verdict = "unsupported"
            claim_confidence = confidence
        assessments.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": round(max(0.0, min(float(claim_confidence), 1.0)), 3),
                "evidence_refs": refs,
            }
        )
    return assessments


def _money_consistent(financial: dict[str, Any]) -> bool:
    lines = financial.get("refund_lines")
    total = financial.get("recommended_refund_brl")
    return (
        isinstance(lines, list)
        and isinstance(total, (int, float))
        and abs(total - sum(float(line.get("amount_brl", -1)) for line in lines)) <= 0.01
    )


async def verify_case(
    case: dict[str, Any],
    specialist_results: list[AgentResult],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> VerificationResult:
    """Validate scope/ownership/consistency and assemble schema-ready semantics."""

    del trace
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("case is missing a valid case_id")
    mismatched = [result.actor for result in specialist_results if result.case_id != case_id]
    if mismatched:
        raise ValueError(f"specialist result case_id mismatch: {mismatched}")

    issue, confidence = _select_issue(specialist_results)
    confidence = round(max(0.0, min(float(confidence), 1.0)), 3)
    policy = _by_actor(specialist_results, "policy-agent")
    rule = _policy_rule(policy, issue)
    refs = _selected_refs(specialist_results, issue)
    available_refs = {
        ref for result in specialist_results for ref in result.evidence_refs
    }

    case_status = rule.get("case_status")
    if case_status not in {"action_required", "no_action", "needs_investigation"}:
        if issue == "insufficient_evidence":
            case_status = "needs_investigation"
        elif issue in {"valid_split_payment", "unsupported_claim"}:
            case_status = "no_action"
        else:
            case_status = "action_required"
    actions = []
    action = rule.get("recommended_action")
    if isinstance(action, str) and action:
        actions.append(action)
    elif issue == "insufficient_evidence":
        actions.append("collect_additional_evidence")

    parties = rule.get("responsible_parties")
    if not isinstance(parties, list):
        parties = (
            [{"party_type": "unknown", "party_id": None}]
            if issue == "insufficient_evidence"
            else []
        )
    parties = [
        {
            "party_type": party.get("party_type", "unknown"),
            "party_id": party.get("party_id"),
        }
        for party in parties
        if isinstance(party, dict)
    ]

    entities = _entities(specialist_results)
    order_id = entities["order_ids"][0] if entities["order_ids"] else None
    financial = _financial(issue, rule, order_id)
    conflicts = []
    if issue == "payment_mismatch":
        conflicts.append(
            {
                "field": "payment_total_brl",
                "sources": ["order_items", "payment_timeline"],
                "selected_source": None,
                "resolution_code": "RECONCILIATION_REQUIRED",
            }
        )

    assessment = {
        "primary_issue": issue,
        "case_status": case_status,
        "confidence": confidence,
    }
    root_cause = {
        "ranked_causes": [
            {"cause_code": issue.upper(), "rank": 1}
        ],
        "responsible_parties": parties,
    }
    claims = _claim_assessments(case, issue, confidence, refs)
    candidate = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": assessment,
        "affected_entities": entities,
        "claim_assessments": claims,
        "root_cause_analysis": root_cause,
        "evidence_refs": refs,
        "data_conflicts": conflicts,
        "financial_resolution": financial,
        "resolution_actions": _unique(actions),
    }

    checks = {
        "case_scope": not mismatched,
        "evidence_ownership": set(refs) <= available_refs,
        "claim_linkage": all(set(item["evidence_refs"]) <= set(refs) for item in claims),
        "evidence_relevance": bool(refs) or issue == "insufficient_evidence",
        "money_consistency": _money_consistent(financial),
        "responsible_party": isinstance(parties, list),
        "primary_issue": isinstance(issue, str),
        "resolution_actions": len(actions) == len(set(actions)),
        "confidence": 0 <= confidence <= 1,
        "data_conflicts": isinstance(conflicts, list),
        "schema_compliance": True,
    }
    validator = getattr(gateway, "validate_output", None)
    if callable(validator):
        try:
            validator(candidate, f"verifier/{case_id}")
        except ValueError:
            checks["schema_compliance"] = False
    errors = [name for name, passed in checks.items() if not passed]
    if errors:
        raise ValueError(f"verification failed: {errors}")

    return VerificationResult(
        assessment=assessment,
        root_cause_analysis=root_cause,
        data_conflicts=conflicts,
        financial_resolution=financial,
        resolution_actions=_unique(actions),
        claim_assessments=claims,
        evidence_refs=refs,
        checks=checks,
        errors=[],
    )


__all__ = ["verify_case"]
