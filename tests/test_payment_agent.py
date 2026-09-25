"""Unit tests cho TV3 — Payment / Refund Agent"""

import pytest
from decimal import Decimal

from student_agent.agents.payment_agent import (
    analyze_payment,
    PaymentTransaction,
    RefundTransaction,
    RefundLine,
)

# ====== HELPERS ======

def make_payment(txn_id="txn_001", ref="ref_001", amount="100.00",
                 status="captured", ev_ref="ev_" + "a" * 20, dup=False):
    return PaymentTransaction(
        transaction_id=txn_id,
        payment_reference=ref,
        amount_brl=Decimal(amount),
        status=status,
        evidence_ref=ev_ref,
        duplicate_charge_confirmed=dup,
    )

def make_refund(txn_id="ref_txn_001", ref="ref_001", amount="50.00",
                status="completed", ev_ref="ev_" + "b" * 20):
    return RefundTransaction(
        transaction_id=txn_id,
        payment_reference=ref,
        amount_brl=Decimal(amount),
        status=status,
        evidence_ref=ev_ref,
    )

# ====== TESTS ======

class TestPaymentReconciled:
    """Khi payment đúng, verdict = payment_reconciled"""
    def test_basic_happy_path(self):
        payment = make_payment(amount="150.00")
        result = analyze_payment(
            case_id="case_001",
            payments=[payment],
            refunds=[],
            expected_total_brl=Decimal("150.00"),
        )
        assert result["findings"]["payment_verdict"] == "payment_reconciled"
        assert result["findings"]["captured_total_brl"] == 150.0


class TestPaymentMismatch:
    """Khi số tiền captured != expected"""
    def test_amount_mismatch(self):
        payment = make_payment(amount="90.00")
        result = analyze_payment(
            case_id="case_002",
            payments=[payment],
            refunds=[],
            expected_total_brl=Decimal("100.00"),
        )
        assert result["findings"]["payment_verdict"] == "payment_mismatch"


class TestDuplicateCharge:
    """Khi có duplicate_charge_confirmed = True"""
    def test_duplicate_charge_flag(self):
        payment = make_payment(dup=True)
        result = analyze_payment(
            case_id="case_003",
            payments=[payment],
            refunds=[],
        )
        assert result["findings"]["payment_verdict"] == "duplicate_charge"


class TestSplitPayment:
    """Khi có nhiều transaction captured → valid_split_payment"""
    def test_split_payment(self):
        p1 = make_payment(txn_id="txn_001", amount="60.00",
                          ev_ref="ev_" + "a" * 20)
        p2 = make_payment(txn_id="txn_002", amount="40.00",
                          ev_ref="ev_" + "c" * 20)
        result = analyze_payment(
            case_id="case_004",
            payments=[p1, p2],
            refunds=[],
            expected_total_brl=Decimal("100.00"),
        )
        assert result["findings"]["payment_verdict"] == "valid_split_payment"
        assert result["findings"]["captured_total_brl"] == 100.0


class TestRefundPending:
    """Khi refund còn pending"""
    def test_refund_pending(self):
        payment = make_payment(amount="100.00")
        refund = make_refund(status="pending", amount="100.00")
        result = analyze_payment(
            case_id="case_005",
            payments=[payment],
            refunds=[refund],
            expected_total_brl=Decimal("100.00"),
        )
        assert result["findings"]["payment_verdict"] == "refund_pending"


class TestRefundFailed:
    """Khi refund bị failed"""
    def test_refund_failed(self):
        payment = make_payment(amount="100.00")
        refund = make_refund(status="failed", amount="100.00")
        result = analyze_payment(
            case_id="case_006",
            payments=[payment],
            refunds=[refund],
        )
        assert result["findings"]["payment_verdict"] == "refund_failed"


class TestRefundLines:
    """Kiểm tra refund_lines tính đúng không"""
    def test_refund_line_total_matches_recommended(self):
        payment = make_payment(amount="200.00")
        lines = [
            RefundLine(reason_code="canceled_item", amount_brl=Decimal("80.00")),
            RefundLine(reason_code="late_delivery", amount_brl=Decimal("20.00")),
        ]
        result = analyze_payment(
            case_id="case_007",
            payments=[payment],
            refunds=[],
            expected_total_brl=Decimal("200.00"),
            refund_lines=lines,
        )
        # sum(lines) == recommended_refund_brl
        total = sum(l["amount_brl"] for l in result["financial_resolution"]["refund_lines"])
        assert total == result["financial_resolution"]["recommended_refund_brl"]
        assert total == 100.0


# ====== ASYNC TESTS ======

import asyncio
from unittest.mock import AsyncMock, Mock


def test_collect_payment_evidence_calls_all_tools():
    """collect_payment_evidence phải gọi đủ 3 tools"""
    from student_agent.agents.payment_agent import collect_payment_evidence

    mock_gateway = AsyncMock()
    mock_gateway.list_tools.return_value = [
        "get_order_payments",
        "get_payment_timeline",
        "get_refund_timeline",
    ]
    mock_gateway.call.return_value = {
        "evidence_ref": "ev_" + "x" * 20,
        "data": {},
    }

    # TraceWriter.emit() là synchronous
    mock_trace = Mock()

    result = asyncio.run(
        collect_payment_evidence(
            case_id="case_test",
            order_id="order_123",
            gateway=mock_gateway,
            trace=mock_trace,
        )
    )

    assert result["case_id"] == "case_test"
    assert len(result["evidence_refs"]) >= 1
    assert mock_gateway.call.call_count == 3
    assert mock_trace.emit.call_count == 3

def test_payment_agent_run_matches_common_contract():
    from student_agent.agents.payment_agent import run
    from student_agent.models import AgentResult, AgentTask

    gateway = AsyncMock()

    gateway.list_tools.return_value = [
        "get_order_payments",
        "get_payment_timeline",
        "get_refund_timeline",
    ]

    gateway.call.return_value = {
        "evidence_ref": "ev_" + "z" * 20,
        "data": {},
    }

    trace = Mock()

    task = AgentTask(
        case_id="CASE_001",
        task_id="CASE_001:payment-agent",
        actor="payment-agent",
        objective="Verify payment.",
        context={
            "claimed_order_id": "order-123",
        },
    )

    result = asyncio.run(
        run(
            task,
            gateway,
            trace,
        )
    )

    assert isinstance(result, AgentResult)
    assert result.case_id == task.case_id
    assert result.task_id == task.task_id
    assert result.actor == "payment-agent"
    assert result.evidence_refs