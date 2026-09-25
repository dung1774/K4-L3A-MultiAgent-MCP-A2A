from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

from student_agent.agents.shipment_agent import run
from student_agent.models import AgentResult, AgentTask


def _task() -> AgentTask:
    return AgentTask(
        case_id="CASE_SHIPMENT",
        task_id="CASE_SHIPMENT:shipment-agent",
        actor="shipment-agent",
        objective="Verify shipment.",
        context={
            "claimed_order_id": "order-1",
            "opened_at": "2018-02-10T00:00:00Z",
        },
    )


def test_run_uses_shared_contract_and_attributes_logistics_delay() -> None:
    gateway = AsyncMock()
    gateway.call.return_value = {
        "domain": "shipment",
        "evidence_ref": "ev_" + "s" * 20,
        "data": {
            "order_id": "order-1",
            "shipment_id": "shipment-1",
            "logistics_provider": "carrier-1",
            "order_estimated_delivery_date": "2018-02-01T00:00:00Z",
            "order_delivered_customer_date": "2018-02-05T00:00:00Z",
            "shipping_limit_date": "2018-01-20T00:00:00Z",
            "order_delivered_carrier_date": "2018-01-19T00:00:00Z",
        },
    }
    trace = Mock()
    result = asyncio.run(run(_task(), gateway, trace))
    assert isinstance(result, AgentResult)
    assert result.actor == "shipment-agent"
    assert result.findings["recommended_issue"] == "late_delivery_logistics"
    assert result.shipment_ids == ["shipment-1"]
    assert result.evidence_refs == ["ev_" + "s" * 20]
    trace.emit.assert_called_once()


def test_required_shipment_failure_returns_observable_error() -> None:
    gateway = AsyncMock()
    gateway.call.side_effect = RuntimeError("timeout")
    result = asyncio.run(run(_task(), gateway, Mock()))
    assert result.findings["recommended_issue"] == "insufficient_evidence"
    assert result.evidence_refs == []
    assert result.errors == ["required get_shipment_summary failed: timeout"]
