# L3A Architecture Record

## 1. System overview

```text
inputs/<case_id>.json
  -> CLI emits case_received
  -> Coordinator plans AgentTask envelopes
  -> Dispatcher invokes shared specialist run(task, gateway, trace)
  -> Specialists consume case-scoped MCP evidence
  -> Coordinator hands AgentResult envelopes to Verifier
  -> Verifier selects relevant evidence and checks consistency
  -> build_output creates the public L3A V2 object
  -> public schema validation
  -> CLI emits case_finalized and writes outputs/<case_id>.json
```

The coordinator owns orchestration only. Domain interpretation stays in the
specialists and cross-domain checks stay in the verifier.

## 2. Agent ownership

| Actor | Input | Responsibility | Output / handoff |
| --- | --- | --- | --- |
| Coordinator | Public case | Route claim hints, enrich later tasks with authoritative earlier findings, and sequence handoffs | `AgentTask` and observable lifecycle events |
| Order/item | `AgentTask` | `get_order`, `get_order_items`; normalize order status, entities, and item total | Shared `AgentResult` |
| Payment | `AgentTask` | `get_order_payments`, `get_payment_timeline`, optional `get_refund_timeline`; reconcile events within the case timeline | Shared `AgentResult` |
| Shipment | `AgentTask` | `get_shipment_summary`; verify confirmed late-delivery attribution | Shared `AgentResult` |
| Policy | `AgentTask` | `get_policy` for the requested `policy_version`; expose machine-readable business rules | Shared `AgentResult` plus `policy_decided` |
| Verifier | Case and specialist results | Check scope, ownership, linkage, relevance, money, responsibility/action consistency, duplicates, confidence, conflicts, and schema | `VerificationResult` with a selected evidence subset |

The coordinator never calls domain MCP tools. Every MCP call receives exactly
the current task's `case_id`.

## 3. A2A protocol

`AgentTask` and `AgentResult` in `src/student_agent/models.py` are the only
specialist envelopes. `task_id` correlates a specialist result to its task and
`case_id` prevents cross-case reuse. The dispatcher rejects mismatched actor,
task, case, or result type. Each task executes once, so there is no handoff
loop. MCP timeouts and domain errors are recorded in `AgentResult.errors`;
optional refund absence does not abort the case.

Trace contains only observable events and decision codes: `case_received`,
`task_assigned`, `tool_result_consumed`, `handoff`, `policy_decided`,
`verification_completed`, and `case_finalized`. It never contains prompts or
private reasoning.

## 4. Evidence lifecycle

The gateway validates every MCP envelope against the public evidence schema and
keeps SDK v2 snake-case fields (`is_error`, `structured_content`). A specialist
copies the returned `evidence_ref` unchanged, emits `tool_result_consumed` only
when it consumes the response, and returns collected refs in its `AgentResult`.

The verifier chooses only evidence relevant to the final issue. `build_output`
enforces:

```text
final evidence refs ⊆ specialist evidence refs
```

It also removes duplicates and rejects unknown refs. No customer statement is
treated as evidence and no evidence ref is generated locally.

## 5. Failure policy

| Failure | Retry | Fallback | Observable result |
| --- | --- | --- | --- |
| Required order/payment/shipment/policy failure | No implicit retry | Continue with available specialists; prefer `insufficient_evidence` | `AgentResult.errors`, handoff error count |
| Optional refund timeline unavailable/not found | No | Continue payment analysis without refund facts | `AgentResult.errors` marked optional |
| Timeout/transient MCP error | No unbounded retry | Same as required/optional classification | Error text without guessed facts |
| Conflicting totals | No | Keep both sources and request reconciliation | `data_conflicts` with `RECONCILIATION_REQUIRED` |
| Invalid specialist result | No | Coordinator creates an observable insufficient-evidence result | Handoff with nonzero error count |

## 6. Verification invariants

- Every specialist `case_id` matches the input case.
- Final evidence belongs to a specialist result for the same case.
- Claim evidence is a subset of final evidence.
- Evidence selected for the final issue comes only from routed domain owners.
- Refund line totals equal `recommended_refund_brl`.
- Case status, responsible parties, actions, and refund amount come from the
  applicable MCP business-policy rule.
- Resolution actions and entity/evidence sets contain no duplicates.
- Confidence stays in `[0, 1]`.
- The verifier candidate and final output pass the public L3A V2 schema.

## 7. Reproducibility

- Python: 3.11 or newer.
- Dependencies: ranges pinned in `pyproject.toml`, including `mcp>=2,<3`.
- Specialist execution is sequential and deterministic.
- No random business decisions; random trace event IDs do not affect output.
- Commands: `python -m compileall src/student_agent`, `python -m pytest -q`,
  `day09 validate-inputs`, `day09 run`, `day09 validate`, and
  `day09 package --output dist/submission.zip`.
- Secrets remain in ignored environment configuration and are never included
  in traces or the submission archive.
