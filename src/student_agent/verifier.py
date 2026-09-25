from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


@dataclass(slots=True)
class VerificationResult:
    output: dict[str, Any]
    valid: bool
    checks: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.valid


def _result(item: Any) -> dict[str, Any]:
    value = item.get("result", item) if isinstance(item, dict) else getattr(item, "result", {})
    return value if isinstance(value, dict) else {}


def _refs(item: Any, result: dict[str, Any]) -> list[str]:
    refs = (
        getattr(item, "evidence_refs", None)
        if not isinstance(item, dict)
        else item.get("evidence_refs")
    )
    refs = refs if isinstance(refs, list) else result.get("evidence_refs", [])
    return [ref for ref in refs if isinstance(ref, str)]


def _first(results: list[dict[str, Any]], *names: str) -> Any:
    for result in results:
        for name in names:
            if result.get(name) is not None:
                return result[name]
    return None


def _money_consistent(financial: Any) -> bool:
    if not isinstance(financial, dict):
        return False
    total = financial.get("recommended_refund_brl")
    lines = financial.get("refund_lines", [])
    if not isinstance(total, (int, float)) or total < 0 or not isinstance(lines, list):
        return False
    amounts = [line.get("amount_brl") for line in lines if isinstance(line, dict)]
    amounts_valid = all(
        isinstance(amount, (int, float)) and amount >= 0 for amount in amounts
    )
    return len(amounts) == len(lines) and amounts_valid and abs(total - sum(amounts)) <= 0.01


def _optional_check(value: Any, predicate) -> bool:
    """Do not reject a partial handoff before all specialists have contributed."""
    return True if value is None else predicate(value)


async def verify_case(
    case: dict[str, Any],
    specialist_results: list[Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> VerificationResult:
    """Verify handoffs and assemble the specialist result into the public output."""
    del gateway
    case_id = case.get("case_id")
    results = [_result(item) for item in specialist_results]
    refs: list[str] = []
    for item, result in zip(specialist_results, results, strict=True):
        refs.extend(_refs(item, result))
    refs = list(dict.fromkeys(refs))

    output = _first(results, "output", "final_output")
    if not isinstance(output, dict):
        output = {
            "schema_version": "day09-l3a-output-v2",
            "case_id": case_id,
            "assessment": {
                "primary_issue": (
                    _first(results, "primary_issue", "issue") or "insufficient_evidence"
                ),
                "case_status": _first(results, "case_status", "status") or "needs_investigation",
                "confidence": (
                    _first(results, "confidence")
                    if isinstance(_first(results, "confidence"), (int, float))
                    else 0.0
                ),
            },
            "affected_entities": _first(results, "affected_entities", "entities")
            or {
                key: []
                for key in (
                    "order_ids",
                    "item_ids",
                    "seller_ids",
                    "payment_references",
                    "shipment_ids",
                )
            },
            "root_cause_analysis": _first(results, "root_cause_analysis", "root_cause")
            or {"ranked_causes": [], "responsible_parties": []},
            "evidence_refs": refs,
            "data_conflicts": _first(results, "data_conflicts") or [],
            "financial_resolution": _first(results, "financial_resolution", "financial")
            or {"currency": "BRL", "recommended_refund_brl": 0, "refund_lines": []},
            "resolution_actions": _first(results, "resolution_actions", "actions") or [],
        }
    output.setdefault("case_id", case_id)
    output.setdefault("evidence_refs", refs)

    checks = {
        "case_scope": output.get("case_id") == case_id,
        "evidence_coverage": _optional_check(
            output.get("evidence_refs"), lambda value: isinstance(value, list)
        ),
        "money_consistency": _optional_check(
            output.get("financial_resolution"), _money_consistent
        ),
        "schema_consistency": _optional_check(
            output.get("schema_version"),
            lambda value: value == "day09-l3a-output-v2",
        ),
        "responsible_party": (
            _optional_check(
                output.get("root_cause_analysis"),
                lambda value: isinstance(value, dict)
                and isinstance(value.get("responsible_parties"), list),
            )
        ),
        "primary_issue": (
            _optional_check(
                output.get("assessment"),
                lambda value: isinstance(value, dict)
                and isinstance(value.get("primary_issue"), str),
            )
        ),
        "resolution_actions": _optional_check(
            output.get("resolution_actions"), lambda value: isinstance(value, list)
        ),
        "confidence": (
            _optional_check(
                output.get("assessment"),
                lambda value: (
                    "confidence" not in value
                    or (
                        isinstance(value["confidence"], (int, float))
                        and 0 <= value["confidence"] <= 1
                    )
                )
                if isinstance(value, dict)
                else False,
            )
        ),
    }
    errors = [name for name, passed in checks.items() if not passed]
    valid = not errors
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="passed" if valid else "failed",
        evidence_refs=refs,
    )
    return VerificationResult(output=output, valid=valid, checks=checks, errors=errors)
