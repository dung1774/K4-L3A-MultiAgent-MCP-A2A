# K4 L3A — Multi-Agent MCP + A2A · Chi tiết phân công công việc

> **Cập nhật lần cuối**: 2026-09-25
> **Trạng thái tổng**: TV2 DONE — TV1, TV3, TV4, TV5 PENDING

---

## Kiến trúc hệ thống

```
inputs/L3A_CASE_NNN.json
        |
        v
workflow.solve_case()           <- TV1 implement
        |
        |-> order_agent.run()   <- TV2 DONE
        |-> payment_agent.run() <- TV3 PENDING
        |-> shipment_agent.run()<- TV4 PENDING
        +-> policy_agent.run()  <- TV5 PENDING (+ verifier)
              |
              v
        Coordinator assembles final output dict
              |
              v
        outputs/<case_id>.json  (validates against l3a-output-v2.schema.json)
        traces/trace.jsonl
```

### Interface chung (KHONG thay doi)

```python
# Moi specialist agent (TV2-TV5) phai implement dung signature nay:
async def run(
    task: AgentTask | dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:   # AgentResult.to_dict()
    ...
```

**gateway.call() pattern** (bat buoc):
```python
evidence = await gateway.call("tool_name", case_id=case_id, **params)
evidence_ref = evidence["evidence_ref"]   # lay nguyen van, khong sua
data         = evidence["data"]
```

**trace.emit() sau moi evidence dung de ra ket luan** (bat buoc):
```python
trace.emit(
    case_id=case_id,
    event_type="tool_result_consumed",
    actor="<agent-name>",
    tool_name="<tool_name>",
    evidence_refs=[evidence_ref],
)
```

**Tool names that** (chay `day09 mcp-tools` de xac nhan lai neu can):
```
get_customer_history
get_order
get_order_items
get_order_payments
get_payment_timeline
get_policy
get_product_context
get_refund_timeline
get_sellers
get_shipment_summary
```

---

## TV1 — Coordinator + Integration (PENDING)

**File can tao:**
- `src/student_agent/workflow.py` — ham `solve_case()` (skeleton da co, can implement)
- `src/student_agent/coordinator.py` (tuy chon, co the gop vao workflow.py)

**Viec can lam:**
1. Implement `solve_case(case, gateway, trace) -> dict` trong workflow.py
2. Parse case input -> tao AgentTask cho tung specialist
3. Goi tuan tu: order_agent -> payment_agent -> shipment_agent -> policy_agent
4. Gom AgentResult tu cac agent, assemble output dict dung schema l3a-output-v2
5. Tra ve dict pass `Contracts.validate_output()`

**Output schema bat buoc** (l3a-output-v2.schema.json):
```json
{
  "schema_version": "day09-l3a-output-v2",
  "case_id": "...",
  "assessment": {
    "primary_issue": "<enum>",
    "case_status": "action_required|no_action|needs_investigation",
    "confidence": 0.0
  },
  "affected_entities": {
    "order_ids": [], "item_ids": [], "seller_ids": [],
    "payment_references": [], "shipment_ids": []
  },
  "claim_assessments": [],
  "root_cause_analysis": { "ranked_causes": [], "responsible_parties": [] },
  "evidence_refs": [],
  "data_conflicts": [],
  "financial_resolution": { "currency": "BRL", "recommended_refund_brl": 0, "refund_lines": [] },
  "resolution_actions": []
}
```

**primary_issue enum cho phep**:
canceled_order_paid, unavailable_order_paid, late_delivery_seller,
late_delivery_logistics, valid_split_payment, payment_mismatch,
duplicate_charge, refund_pending, refund_failed,
unsupported_claim, insufficient_evidence

**Goi y**: Dung primary_issue tu order_agent (TV2) lam uu tien dau,
override bang payment/shipment neu chung phat hien issue nang hon.

**Import TV2 tu coordinator**:
```python
from student_agent.agents.order_agent import AgentTask as OrderTask, run as order_run
result = await order_run(OrderTask.from_case(case), gateway, trace)
# result["primary_issue"], result["confidence"], result["evidence_refs"],
# result["affected_entities"], result["details"]["claim_verdicts"]
```

---

## TV2 — Order / Item / Seller Agent (DONE)

### Files da tao

| File | Mo ta |
|---|---|
| `src/student_agent/agents/__init__.py` | Package init |
| `src/student_agent/agents/order_agent.py` | Toan bo logic TV2 |
| `tests/test_order_agent.py` | 4 unit tests, tat ca pass |

### Noi dung order_agent.py

**Dinh nghia 2 dataclass noi bo** (khong phu thuoc vao TV1/TV5):

```python
@dataclass
class AgentTask:
    case_id: str
    order_id: str
    claims: list[dict]
    extras: dict

    @classmethod
    def from_case(cls, case: dict) -> AgentTask: ...
    # Tu dong parse tu raw case dict (customer_request.claimed_order_id, v.v.)

@dataclass
class AgentResult:
    agent: str
    case_id: str
    primary_issue: str | None
    confidence: float
    evidence_refs: list[str]
    affected_entities: dict[str, list[str]]
    details: dict

    def to_dict(self) -> dict: ...
```

**Ham chinh `run()`** — dung signature chung:

```python
async def run(
    task: AgentTask | dict,   # nhan ca dict (raw case) lan AgentTask
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict:                    # AgentResult.to_dict()
```

**Thu tu goi tool** (theo thu tu, dung ten that tu `day09 mcp-tools`):

1. `get_order(case_id, order_id)` — luon goi, lay order_status
2. `get_order_items(case_id, order_id)` — luon goi, lay item_id + seller_id
3. `get_sellers(case_id, seller_id)` — chi goi neu step 2 tra ve seller_id

**Sau moi gateway.call() thanh cong**:
```python
trace.emit(
    case_id=case_id,
    event_type="tool_result_consumed",
    actor="order-agent",
    tool_name="get_order",        # ten tool tuong ung
    evidence_refs=[evidence_ref], # lay nguyen van tu response
)
```

### Logic phat hien issue

| `order_status` (lowercase) | `primary_issue` | `confidence` | `evidence_refs` |
|---|---|---|---|
| `canceled` | `canceled_order_paid` | 0.90 | [order_ref, items_ref] |
| `unavailable`, `processing_failed`, `invoiced_failed` | `unavailable_order_paid` | 0.85 | [order_ref] |
| `approved` / `processing` + co items + co seller | `late_delivery_seller` | 0.70 | [order_ref, items_ref, sellers_ref] |
| `approved` / `processing` nhung khong co items | `insufficient_evidence` | 0.40 | [order_ref] |
| `delivered`, `shipped`, `invoiced`, v.v. | `None` | 0.80 | [order_ref] |

### affected_entities TV2 da populate

```python
{
    "order_ids":          [claimed_order_id],
    "item_ids":           [order_item_id, ...]  # lay tu items response
    "seller_ids":         [seller_id, ...]      # lay tu items response
    "payment_references": [],                   # de trong, TV3 fill
    "shipment_ids":       [],                   # de trong, TV4 fill
}
```

### claim_verdicts (trong result["details"]["claim_verdicts"])

TV2 xu ly claim_verdicts cho cac claim trong task.claims:

| `claim.topic` | Dieu kien | `verdict` |
|---|---|---|
| Khop voi primary_issue phat hien | - | `supported` |
| `canceled_order_paid` / `unavailable_order_paid` / `late_delivery_seller` | Khong khop issue | `unsupported` |
| `requested_full_refund` | primary_issue la canceled/unavailable | `supported` |
| `requested_full_refund` | primary_issue khac | `partially_supported` |
| Topic khac | - | `insufficient_evidence` |

### Cach su dung tu coordinator (TV1)

```python
from student_agent.agents.order_agent import AgentTask, run as order_run

# Option 1: truyen raw case dict
result = await order_run(case, gateway, trace)

# Option 2: tao AgentTask truoc
task = AgentTask.from_case(case)
result = await order_run(task, gateway, trace)

# result la dict co cac key:
# {
#   "agent": "order-agent",
#   "case_id": "L3A_CASE_001",
#   "primary_issue": "canceled_order_paid",   # hoac None
#   "confidence": 0.90,
#   "evidence_refs": ["ev_xxx...", "ev_yyy..."],
#   "affected_entities": { "order_ids": [...], "item_ids": [...], ... },
#   "details": {
#     "order_status": "canceled",
#     "item_count": 2,
#     "seller_info": {...},
#     "claim_verdicts": [{"claim_id": "...", "verdict": "supported", ...}]
#   }
# }
```

### Ket qua test

```
pytest tests/test_order_agent.py -q
....
4 passed in 1.07s
```

4 test cases:
- `test_canceled_order_paid` — order_status=canceled -> primary_issue=canceled_order_paid
- `test_unavailable_order_paid` — order_status=unavailable -> primary_issue=unavailable_order_paid
- `test_late_delivery_seller` — order_status=approved + co items/seller -> late_delivery_seller
- `test_no_issue_delivered_order` — order_status=delivered -> primary_issue=None

### Ghi chu / Rui ro

- `late_delivery_seller` tu TV2 chi dua vao order_status (approved/processing). TV4 can dung
  `get_shipment_summary` de xac nhan hoac override bang du lieu thuc te (shipping_limit_date, v.v.).
- Neu `get_order` that bai -> tra ve `insufficient_evidence` ngay, khong tiep tuc.
- Neu `get_order_items` hoac `get_sellers` that bai -> ghi log warning, tiep tuc xu ly voi du lieu con lai.



---

## TV3 — Payment / Refund Agent (PENDING)

**File can tao**: `src/student_agent/agents/payment_agent.py`

**Tools can dung**:
- get_order_payments(case_id, order_id) — danh sach payment transactions
- get_payment_timeline(case_id, order_id) — timeline su kien payment
- get_refund_timeline(case_id, order_id) — trang thai refund

**Viec can lam**:
1. Fetch payment data, tinh tong payment_value tu cac transactions
2. Phat hien issue: valid_split_payment, payment_mismatch, duplicate_charge,
   refund_pending, refund_failed
3. QUAN TRONG: tinh recommended_refund_brl va populate refund_lines:
   { "reason_code": "canceled_order_refund", "amount_brl": <total>, "entity_id": order_id }
4. Populate affected_entities["payment_references"] tu payment_sequential_id hoac payment ID
5. Tra ve AgentResult.to_dict() cung pattern nhu TV2

**Luu y**: sum(refund_lines.amount_brl) ~ recommended_refund_brl — verifier se check.

---

## TV4 — Shipment / Logistics Agent (PENDING)

**File can tao**: `src/student_agent/agents/shipment_agent.py`

**Tool can dung**:
- get_shipment_summary(case_id, order_id)

**Viec can lam**:
1. Fetch shipment data
2. So sanh cac moc thoi gian:
   - shipping_limit_date (deadline seller phai giao carrier)
   - order_delivered_carrier_date (ngay carrier nhan)
   - order_estimated_delivery_date (ngay du kien giao khach)
   - order_delivered_customer_date (ngay thuc te giao khach)
3. Phan xet delay attribution:
   - order_delivered_carrier_date > shipping_limit_date -> seller delay -> late_delivery_seller
   - order_delivered_customer_date > order_estimated_delivery_date
     nhung carrier nhan dung han -> logistics delay -> late_delivery_logistics
4. Populate affected_entities["shipment_ids"]
5. KHONG tu ket luan neu thieu du lieu -> tra insufficient_evidence

**Phoi hop voi TV2**: TV2 co the dat late_delivery_seller tam thoi khi order
stuck o approved/processing. TV4 se xac nhan hoac override bang du lieu shipment that.
Coordinator (TV1) chiu trach nhiem merge va chon ket luan cuoi.

---

## TV5 — Policy + Verifier (PENDING)

**Files can tao**:
- `src/student_agent/agents/policy_agent.py`
- `src/student_agent/verifier.py`

### Policy Agent

**Tool can dung**: get_policy(case_id, policy_version)

**Viec can lam**:
1. Fetch policy bang policy_version tu case input (e.g. "EC_POLICY_V1")
2. Dua vao primary_issue -> tra policy rule
3. Xac dinh resolution_actions (list string, max 8, moi cai max 80 chars)
4. Xac dinh refund eligibility de confirm/adjust recommended_refund_brl cua TV3

**resolution_actions mau**:
["issue_full_refund", "notify_seller", "flag_order_for_review"]

### Verifier

**Implement verify_case()** — check consistency truoc khi output:
1. evidence_refs tat ca phai co dang ^ev_[A-Za-z0-9_-]{20,96}$
2. sum(refund_lines.amount_brl) ~ recommended_refund_brl (tolerance +-0.01)
3. primary_issue phai thuoc enum cho phep
4. confidence trong [0, 1]
5. affected_entities khong co string rong
6. claim_assessments[].evidence_refs khong dung ref cua case khac

---

## Quy tac chung (moi TV doc)

1. KHONG tu tao evidence_ref — chi lay tu gateway.call(...)["evidence_ref"]
2. KHONG dung evidence cheo case — case_id phai dung voi case dang xu ly
3. trace.emit() bat buoc cho moi evidence dung trong ket luan
4. Test voi mock — khong can MCP server that, dung asyncio.run() thay cho pytest-asyncio
5. Schema validation: chay `day09 validate` sau khi co output de kiem tra

## Chay nhanh de kiem tra

```bash
# Xem tool names that:
day09 mcp-tools

# Chay test TV2 (da pass):
pytest tests/test_order_agent.py -q

# Chay toan bo test (1 pre-existing failure khong lien quan den code):
pytest -q

# Sau khi TV1 implement solve_case():
day09 run        # chay tren 100 case
day09 validate   # check output schema
```
