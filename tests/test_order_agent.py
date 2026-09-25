from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

from student_agent.agents.order_agent import run
from student_agent.models import AgentResult, AgentTask

ORDER_REF = "ev_order_canceledAAAAAAAAAAAA"
ITEMS_REF = "ev_items_canceledAAAAAAAAAAAA"


def _task(case_id: str = "L3A_CASE_TEST") -> AgentTask:
    return AgentTask(
        case_id=case_id,
        task_id=f"{case_id}:order-agent",
        actor="order-agent",
        objective="Verify order and items.",
        context={"claimed_order_id": "order-xyz"},
    )


def _gateway(status: str, *, fail_items: bool = False) -> MagicMock:
    async def call(tool_name: str, *, case_id: str, **kwargs: Any) -> dict[str, Any]:
        assert case_id.startswith("L3A_CASE")
        if tool_name == "get_order":
            return {
                "evidence_ref": ORDER_REF,
                "data": {"order_id": "order-xyz", "order_status": status},
            }
        if tool_name == "get_order_items":
            if fail_items:
                raise RuntimeError("temporary item lookup failure")
            return {
                "evidence_ref": ITEMS_REF,
                "data": [
                    {
                        "order_item_id": "1",
                        "seller_id": "seller-abc",
                        "price": "90.00",
                        "freight_value": "10.00",
                    }
                ],
            }
        raise AssertionError(tool_name)

    gateway = MagicMock()
    gateway.call = call
    return gateway


def test_run_uses_shared_contract_and_evidence_refs() -> None:
    trace = MagicMock()
    result = asyncio.run(run(_task(), _gateway("canceled"), trace))
    assert isinstance(result, AgentResult)
    assert result.actor == "order-agent"
    assert result.findings["recommended_issue"] == "canceled_order_paid"
    assert result.findings["expected_total_brl"] == 100.0
    assert result.evidence_refs == [ORDER_REF, ITEMS_REF]
    assert result.order_ids == ["order-xyz"]
    assert result.item_ids == ["1"]
    assert result.seller_ids == ["seller-abc"]
    assert trace.emit.call_count == 2


def test_optional_partial_failure_is_observable() -> None:
    result = asyncio.run(run(_task(), _gateway("delivered", fail_items=True), MagicMock()))
    assert isinstance(result, AgentResult)
    assert result.evidence_refs == [ORDER_REF]
    assert result.errors
    assert "get_order_items failed" in result.errors[0]


def test_missing_order_id_returns_insufficient_evidence() -> None:
    task = _task()
    task.context = {}
    result = asyncio.run(run(task, MagicMock(), MagicMock()))
    assert result.findings["recommended_issue"] == "insufficient_evidence"
    assert not result.evidence_refs
    assert result.errors
