"""Tests for TV2 order_agent.

All tests use mocks — no real MCP server or TraceWriter needed.
Evidence refs follow the pattern ev_<20+chars> as per the schema.
No pytest-asyncio required: sync wrappers use asyncio.run().
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

from student_agent.agents.order_agent import AgentTask, run


_ORDER_EV_REF = "ev_order_canceledAAAAAAAAAAAA"
_ITEMS_EV_REF = "ev_items_canceledAAAAAAAAAAAA"
_SELLERS_EV_REF = "ev_sellers_lateAAAAAAAAAAAAAA"

assert all(len(r) >= 23 for r in [_ORDER_EV_REF, _ITEMS_EV_REF, _SELLERS_EV_REF])


def _make_gateway(order_status: str, has_items: bool = True) -> MagicMock:
    order_response: dict[str, Any] = {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": _ORDER_EV_REF,
        "result_hash": "sha256:" + "a" * 64,
        "domain": "order",
        "data": {"order_id": "order-xyz", "order_status": order_status},
    }
    items_response: dict[str, Any] = {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": _ITEMS_EV_REF,
        "result_hash": "sha256:" + "b" * 64,
        "domain": "item",
        "data": [{"order_item_id": "1", "seller_id": "seller-abc", "price": 100.0}] if has_items else [],
    }
    sellers_response: dict[str, Any] = {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": _SELLERS_EV_REF,
        "result_hash": "sha256:" + "c" * 64,
        "domain": "seller",
        "data": {"seller_id": "seller-abc", "seller_city": "Sao Paulo"},
    }

    async def _call(tool_name: str, *, case_id: str, **kwargs: Any) -> dict[str, Any]:
        if tool_name == "get_order":
            return order_response
        if tool_name == "get_order_items":
            return items_response
        if tool_name == "get_sellers":
            return sellers_response
        raise RuntimeError(f"unexpected tool: {tool_name}")

    gateway = MagicMock()
    gateway.call = _call
    return gateway


def _make_trace() -> MagicMock:
    trace = MagicMock()
    trace.emit = MagicMock(return_value={})
    return trace


def test_canceled_order_paid() -> None:
    task = AgentTask(
        case_id="L3A_CASE_TEST",
        order_id="order-xyz",
        claims=[
            {"claim_id": "c-001", "topic": "canceled_order_paid"},
            {"claim_id": "c-002", "topic": "requested_full_refund"},
        ],
    )
    result = asyncio.run(run(task, _make_gateway("canceled"), _make_trace()))
    assert result["primary_issue"] == "canceled_order_paid"
    assert result["confidence"] >= 0.85
    assert _ORDER_EV_REF in result["evidence_refs"]
    assert _ITEMS_EV_REF in result["evidence_refs"]
    trace = _make_trace()
    asyncio.run(run(task, _make_gateway("canceled"), trace))
    calls = [c.kwargs for c in trace.emit.call_args_list]
    tool_names = [c["tool_name"] for c in calls if "tool_name" in c]
    assert "get_order" in tool_names
    assert "get_order_items" in tool_names
    verdicts = result["details"]["claim_verdicts"]
    assert verdicts[0]["verdict"] == "supported"
    assert verdicts[1]["verdict"] == "supported"


def test_unavailable_order_paid() -> None:
    task = AgentTask(
        case_id="L3A_CASE_TEST2",
        order_id="order-xyz",
        claims=[{"claim_id": "c-003", "topic": "unavailable_order_paid"}],
    )
    result = asyncio.run(run(task, _make_gateway("unavailable", has_items=False), _make_trace()))
    assert result["primary_issue"] == "unavailable_order_paid"
    assert result["confidence"] >= 0.80
    assert _ORDER_EV_REF in result["evidence_refs"]
    assert result["details"]["claim_verdicts"][0]["verdict"] == "supported"


def test_late_delivery_seller() -> None:
    task = AgentTask(
        case_id="L3A_CASE_TEST3",
        order_id="order-xyz",
        claims=[{"claim_id": "c-004", "topic": "late_delivery_seller"}],
    )
    trace = _make_trace()
    result = asyncio.run(run(task, _make_gateway("approved", has_items=True), trace))
    assert result["primary_issue"] == "late_delivery_seller"
    assert result["confidence"] >= 0.65
    assert _ORDER_EV_REF in result["evidence_refs"]
    assert _ITEMS_EV_REF in result["evidence_refs"]
    assert "seller-abc" in result["affected_entities"]["seller_ids"]
    calls = [c.kwargs for c in trace.emit.call_args_list]
    tool_names = [c["tool_name"] for c in calls if "tool_name" in c]
    assert "get_sellers" in tool_names
    assert result["details"]["claim_verdicts"][0]["verdict"] == "supported"


def test_no_issue_delivered_order() -> None:
    task = AgentTask(case_id="L3A_CASE_TEST4", order_id="order-xyz", claims=[])
    result = asyncio.run(run(task, _make_gateway("delivered"), _make_trace()))
    assert result["primary_issue"] is None
    assert result["confidence"] >= 0.75
    assert _ORDER_EV_REF in result["evidence_refs"]
    assert result["details"]["claim_verdicts"] == []
