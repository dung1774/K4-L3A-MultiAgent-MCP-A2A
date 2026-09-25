from student_agent.coordinator import plan_tasks


def actors(case):
    return [task.actor for task in plan_tasks(case)]


def test_payment_case_routes_to_payment() -> None:
    case = {
        "case_id": "CASE_001",
        "policy_version": "EC_POLICY_V1",
        "customer_request": {
            "claimed_order_id": "order-1",
            "claims": [
                {
                    "claim_id": "claim-1",
                    "topic": "canceled_order_paid",
                },
                {
                    "claim_id": "claim-2",
                    "topic": "requested_full_refund",
                },
            ],
        },
    }

    assert actors(case) == [
        "order-agent",
        "payment-agent",
        "policy-agent",
    ]


def test_delivery_case_routes_to_shipment() -> None:
    case = {
        "case_id": "CASE_002",
        "policy_version": "EC_POLICY_V1",
        "customer_request": {
            "claimed_order_id": "order-2",
            "claims": [
                {
                    "claim_id": "claim-1",
                    "topic": "late_delivery",
                }
            ],
        },
    }

    assert actors(case) == [
        "order-agent",
        "shipment-agent",
        "policy-agent",
    ]


def test_unknown_case_routes_broadly() -> None:
    case = {
        "case_id": "CASE_003",
        "policy_version": "EC_POLICY_V1",
        "customer_request": {
            "claimed_order_id": "order-3",
            "claims": [],
        },
    }

    assert actors(case) == [
        "order-agent",
        "payment-agent",
        "shipment-agent",
        "policy-agent",
    ]