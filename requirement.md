#Nhiệm vụ của tôi: Policy + Verifier

Hai phần này mình ghép cùng nhau vì chúng đều là cross-domain validation, thay vì ghép Policy với Shipment.

Làm:

src/student_agent/agents/policy_agent.py
src/student_agent/verifier.py

Policy:

tra policy qua MCP
xác định rule áp dụng
resolution action
refund eligibility

Verifier:

evidence ownership
evidence coverage
case scope
money consistency
responsible party
primary issue
resolution actions
confidence
schema consistency

#Interface yêu cầu:
async def verify_case(
    case: dict[str, Any],
    specialist_results: list[AgentResult],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> VerificationResult:
    ...