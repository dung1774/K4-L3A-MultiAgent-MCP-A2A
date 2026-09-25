# L3A Architecture Record

## 1. System overview

Hệ thống xử lý mỗi case theo luồng:

```text
Input
  ↓
Coordinator
  ↓
Specialist Agents
  ├── Order/Item Agent
  ├── Payment Agent
  ├── Shipment Agent
  └── Policy Agent
  ↓
Verifier
  ↓
Output JSON

Specialist Agents ── MCP Evidence Gateway
All stages ────────── Trace
```

Coordinator chỉ điều phối và handoff. Customer claim chỉ được dùng làm tín hiệu routing, không được coi là ground truth. Mọi kết luận nghiệp vụ phải dựa trên evidence từ MCP.

---

## 2. Agent ownership

| Actor            | Input                     | Trách nhiệm                                                                    | MCP tools / Output                                                                  |
| ---------------- | ------------------------- | ------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------- |
| Coordinator      | Case input                | Chọn specialist, tạo task, tổng hợp kết quả và handoff                         | Không gọi domain MCP                                                                |
| Order/Item Agent | `AgentTask`               | Xác minh order, item, seller và tổng giá trị đơn                               | `get_order`, `get_order_items` → `AgentResult`                                      |
| Payment Agent    | `AgentTask`               | Xác minh payment, payment timeline, refund                                     | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` → `AgentResult` |
| Shipment Agent   | `AgentTask`               | Xác minh shipment và nguyên nhân giao hàng trễ                                 | `get_shipment_summary` → `AgentResult`                                              |
| Policy Agent     | `AgentTask`               | Lấy policy áp dụng cho case                                                    | `get_policy` → `AgentResult`                                                        |
| Verifier         | Case + specialist results | Chọn evidence liên quan, kiểm tra consistency, confidence và tạo kết luận cuối | `VerificationResult`                                                                |

Mỗi agent chỉ sử dụng các MCP tools thuộc phạm vi trách nhiệm của mình.

---

## 3. A2A protocol

Các agent trao đổi qua hai envelope chung:

* `AgentTask`: task do Coordinator gửi cho specialist.
* `AgentResult`: kết quả specialist trả về Coordinator.

`case_id` được dùng để đảm bảo mọi task và evidence thuộc đúng case. `task_id` dùng để liên kết task với kết quả tương ứng.

Luồng handoff:

```text
Coordinator → Specialist → Coordinator → Verifier → Coordinator
```

Dispatcher kiểm tra `case_id`, `task_id`, actor và kiểu kết quả trước khi chấp nhận.

Mỗi specialist chỉ chạy một lần cho mỗi task nên không tạo vòng lặp A2A.

Trace chỉ lưu các sự kiện quan sát được, không lưu prompt hoặc chain-of-thought.

---

## 4. Evidence lifecycle

Mọi evidence được lấy trực tiếp từ MCP Evidence Gateway với đúng `case_id`.

```text
MCP call
  ↓
MCP response validation
  ↓
evidence_ref
  ↓
tool_result_consumed
  ↓
AgentResult
  ↓
Verifier chọn evidence liên quan
  ↓
output.evidence_refs
```

Quy tắc:

* Không tự tạo hoặc sửa `evidence_ref`.
* Không dùng evidence giữa các case khác nhau.
* Mỗi evidence được sử dụng phải có event `tool_result_consumed`.
* Verifier chỉ chọn evidence thực sự hỗ trợ kết luận cuối.
* Customer message không được coi là evidence.

---

## 5. Failure policy

| Failure                              | Retry?             | Fallback                                        | Trace / result                  |
| ------------------------------------ | ------------------ | ----------------------------------------------- | ------------------------------- |
| MCP timeout / error                  | Không retry vô hạn | Tiếp tục với evidence còn lại                   | Ghi vào `AgentResult.errors`    |
| Evidence / record not found          | Không              | Không suy đoán dữ liệu                          | `insufficient_evidence` khi cần |
| Source conflict                      | Không              | Giữ các nguồn và để Verifier xử lý              | `data_conflicts`                |
| Invalid specialist result            | Không              | Coordinator chuyển thành kết quả thiếu evidence | Handoff có error                |
| Optional refund evidence unavailable | Không              | Tiếp tục phân tích payment nếu vẫn đủ evidence  | Ghi lỗi optional                |

Missing evidence không được thay thế bằng dữ liệu tự suy đoán.

---

## 6. Verification invariants

Trước khi finalize, Verifier kiểm tra:

* `case_id` của mọi specialist phải khớp input.
* Evidence phải thuộc đúng case hiện tại.
* Evidence cuối phải xuất phát từ evidence đã được specialist thu thập.
* Claim assessment phải được evidence phù hợp hỗ trợ.
* Không có duplicate entity hoặc evidence ref.
* `recommended_refund_brl >= 0`.
* Tổng refund lines phải nhất quán với số tiền refund đề xuất.
* `primary_issue`, responsible party, resolution action và financial resolution phải nhất quán.
* `confidence` nằm trong `[0, 1]`.
* Output cuối phải pass `l3a-output-v2.schema.json`.

---

## 7. Reproducibility

* Python: 3.11+
* Dependencies: quản lý trong `pyproject.toml`.
* Specialist agents chạy tuần tự để giữ workflow dễ kiểm chứng.
* Không sử dụng random cho quyết định nghiệp vụ.
* Không ghi API key vào source, trace hoặc submission.

Các lệnh chính:

```bash
python -m pip install -e ".[dev]"

day09 validate-inputs
day09 mcp-tools
day09 run
day09 validate
day09 package --output dist/submission.zip
```

Submission cuối chỉ gồm:

```text
manifest.json
trace.jsonl
outputs/
```
