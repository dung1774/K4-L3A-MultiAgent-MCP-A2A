from __future__ import annotations

import importlib
from typing import Any

from .mcp_gateway import EvidenceGateway
from .models import AgentResult, VerificationResult
from .trace import TraceWriter


async def verify_case(
    case: dict[str, Any],
    specialist_results: list[AgentResult],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> VerificationResult:
    """
    Dynamically load the verifier implemented by TV5.

    Expected module:
        student_agent.verifier

    Expected function:
        async def verify_case(...)
    """

    module_name = "student_agent.verifier"

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Verifier module is not available yet: "
            "src/student_agent/verifier.py"
        ) from exc

    runner = getattr(module, "verify_case", None)

    if runner is None:
        raise RuntimeError(
            "student_agent.verifier must expose async function verify_case()"
        )

    result = await runner(
        case,
        specialist_results,
        gateway,
        trace,
    )

    if not isinstance(result, VerificationResult):
        raise TypeError(
            f"Verifier returned {type(result).__name__}, "
            "expected VerificationResult"
        )

    return result