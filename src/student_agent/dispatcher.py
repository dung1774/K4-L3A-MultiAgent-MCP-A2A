from __future__ import annotations

import importlib

from .mcp_gateway import EvidenceGateway
from .models import AgentResult, AgentTask
from .trace import TraceWriter

AGENT_MODULES = {
    "order-agent": "student_agent.agents.order_agent",
    "payment-agent": "student_agent.agents.payment_agent",
    "shipment-agent": "student_agent.agents.shipment_agent",
    "policy-agent": "student_agent.agents.policy_agent",
}


async def dispatch_task(
    task: AgentTask,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> AgentResult:
    module_name = AGENT_MODULES.get(task.actor)

    if module_name is None:
        raise ValueError(f"Unknown specialist actor: {task.actor}")

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            f"Specialist module is not available yet: {module_name}"
        ) from exc

    runner = getattr(module, "run", None)

    if runner is None:
        raise RuntimeError(
            f"{module_name} must expose async function run(task, gateway, trace)"
        )

    result = await runner(task, gateway, trace)

    if not isinstance(result, AgentResult):
        raise TypeError(
            f"{task.actor} returned {type(result).__name__}, "
            "expected AgentResult"
        )

    if result.case_id != task.case_id:
        raise ValueError(
            f"{task.actor} returned mismatched case_id"
        )

    if result.task_id != task.task_id:
        raise ValueError(
            f"{task.actor} returned mismatched task_id"
        )

    if result.actor != task.actor:
        raise ValueError(
            f"{task.actor} returned mismatched actor"
        )

    return result