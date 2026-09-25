import pytest

from student_agent.coordinator import plan_tasks


@pytest.mark.parametrize(
    ("topic", "expected"),
    [
        (
            "canceled_order_paid",
            {"order-agent", "payment-agent", "policy-agent"},
        ),
        (
            "unavailable_order_paid",
            {"order-agent", "payment-agent", "policy-agent"},
        ),
        (
            "late_delivery_seller",
            {
                "order-agent",
                "payment-agent",
                "shipment-agent",
                "policy-agent",
            },
        ),
        (
            "late_delivery_logistics",
            {
                "order-agent",
                "payment-agent",
                "shipment-agent",
                "policy-agent",
            },
        ),
        (
            "valid_split_payment",
            {"order-agent", "payment-agent", "policy-agent"},
        ),
        (
            "payment_mismatch",
            {"order-agent", "payment-agent", "policy-agent"},
        ),
        (
            "duplicate_charge",
            {"order-agent", "payment-agent", "policy-agent"},
        ),
        (
            "refund_pending",
            {"order-agent", "payment-agent", "policy-agent"},
        ),
        (
            "refund_failed",
            {"order-agent", "payment-agent", "policy-agent"},
        ),
        (
            "unsupported_claim",
            {
                "order-agent",
                "payment-agent",
                "shipment-agent",
                "policy-agent",
            },
        ),
    ],
)
def test_routes_all_known_claim_topics(topic, expected):
    case = {
        "case_id": "CASE_TEST",
        "policy_version": "EC_POLICY_V1",
        "customer_request": {
            "claimed_order_id": "order-test",
            "claims": [
                {
                    "claim_id": "claim-main",
                    "topic": topic,
                },
                {
                    "claim_id": "claim-refund",
                    "topic": "requested_full_refund",
                },
            ],
        },
    }

    actual = {
        task.actor
        for task in plan_tasks(case)
    }

    assert actual == expected