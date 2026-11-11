"""Payouts batch lifecycle (mock PayPal) and webhook dispatch."""
import itertools
import time

import pytest
from fastapi.testclient import TestClient

from app.paypal import get_paypal_client
from app.paypal import mock as paypal_mock

ALICE, BOB, CAROL = 1, 2, 3
_event_ids = itertools.count(1)
_latte_qty = itertools.count(1)


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def fast_payouts(monkeypatch):
    monkeypatch.setattr(paypal_mock, "PAYOUT_DELAY_SECONDS", 0.25)


def approved_expense(client, employee_id):
    """A small approved PayPal purchase; a new amount each time so duplicate detection stays quiet."""
    cart = {"employee_id": employee_id, "vendor_id": "coffeeco", "items": [{"sku": "cf-latte", "quantity": next(_latte_qty)}],
            "purpose": "Coffee with prospect to discuss pilot project", "attendees": "Prospect contact (Initech)"}
    order = client.post("/api/paypal/orders", json=cart).json()
    done = client.post(f"/api/paypal/orders/{order['order_id']}/complete").json()
    assert done["status"] == "approved", done
    return done["expense_id"], order


def status_of(client, expense_id):
    return next(r["status"] for r in client.get("/api/expenses").json() if r["id"] == expense_id)


def webhook(client, event_type, resource):
    return client.post("/webhooks/paypal", json={"id": f"WH-PAYOUT-{next(_event_ids)}", "event_type": event_type, "resource": resource}).json()


def test_batch_groups_by_employee_and_marks_expenses_paid(client):
    a1, _ = approved_expense(client, ALICE)
    a2, _ = approved_expense(client, ALICE)
    b1, _ = approved_expense(client, BOB)
    page = client.get(f"/reimburse?ids={a1},{a2},{b1}").text
    assert "Send PayPal Payouts" in page and "3 expenses · 2 employees" in page

    batch = client.post("/api/payouts", json={"expense_ids": [a1, a2, b1]}).json()
    assert batch["status"] == "PENDING" and batch["paypal_batch_id"] and not batch["done"]
    items = {i["employee"]: i for i in batch["items"]}
    assert items["Alice Chen"]["expense_ids"] == sorted([a1, a2]) and items["Bob Martinez"]["expense_ids"] == [b1]
    assert round(sum(i["amount"] for i in batch["items"]), 2) == batch["total"]
    sent = get_paypal_client().payouts[batch["paypal_batch_id"]]["items"]
    assert {i["sender_item_id"] for i in sent} == {f"{min(a1, a2)},{max(a1, a2)}", str(b1)}      # sender_item_id = expense ids

    assert client.post("/api/payouts", json={"expense_ids": [a1, b1]}).status_code == 409        # never paid twice
    assert client.get(f"/api/payouts/{batch['id']}").json()["status"] == "PROCESSING"
    assert status_of(client, a1) == "approved"
    time.sleep(0.35)
    final = client.get(f"/api/payouts/{batch['id']}").json()
    assert final["status"] == "SUCCESS" and final["done"] and {i["status"] for i in final["items"]} == {"SUCCESS"}
    assert all(i["paypal_item_id"] for i in final["items"])
    assert {status_of(client, e) for e in (a1, a2, b1)} == {"paid"}
    assert client.get("/api/payouts/999999").status_code == 404
    assert any(b["id"] == batch["id"] for b in client.get("/api/payouts").json())
    assert "succeeded" in client.get("/reimburse").text or "SUCCESS" in client.get("/reimburse").text


def test_item_webhooks_fail_and_succeed(client):
    a, _ = approved_expense(client, ALICE)
    c, _ = approved_expense(client, CAROL)
    batch = client.post("/api/payouts", json={"expense_ids": [a, c]}).json()
    pid = batch["paypal_batch_id"]

    got = webhook(client, "PAYMENT.PAYOUTS-ITEM.FAILED", {
        "payout_batch_id": pid, "payout_item_id": "ITEMFAIL1", "transaction_status": "FAILED", "payout_item": {"sender_item_id": str(a)}})
    assert got["verified"] and "FAILED" in got["action"]
    got = webhook(client, "PAYMENT.PAYOUTS-ITEM.SUCCEEDED", {
        "payout_batch_id": pid, "payout_item_id": "ITEMOK1", "transaction_status": "SUCCESS", "payout_item": {"sender_item_id": str(c)}})
    assert "SUCCESS" in got["action"]
    assert (status_of(client, a), status_of(client, c)) == ("approved", "paid")

    # A late UNCLAIMED event does not undo a success; the failed expense can be paid again.
    webhook(client, "PAYMENT.PAYOUTS-ITEM.UNCLAIMED", {
        "payout_batch_id": pid, "payout_item_id": "ITEMOK1", "transaction_status": "UNCLAIMED", "payout_item": {"sender_item_id": str(c)}})
    assert status_of(client, c) == "paid"
    retry = client.post("/api/payouts", json={"expense_ids": [a, c]}).json()
    assert [i["expense_ids"] for i in retry["items"]] == [[a]]
    assert webhook(client, "PAYMENT.PAYOUTS-ITEM.SUCCEEDED", {"payout_batch_id": "NOPE", "transaction_status": "SUCCESS"})["action"] == "unknown payout batch"


def test_batch_denied_webhook_releases_expenses(client):
    a, _ = approved_expense(client, BOB)
    batch = client.post("/api/payouts", json={"expense_ids": [a]}).json()
    got = webhook(client, "PAYMENT.PAYOUTSBATCH.DENIED", {"batch_header": {"payout_batch_id": batch["paypal_batch_id"], "batch_status": "DENIED"}})
    assert "DENIED" in got["action"]
    view = next(b for b in client.get("/api/payouts").json() if b["id"] == batch["id"])
    assert view["done"] and view["items"][0]["status"] == "FAILED" and status_of(client, a) == "approved"
    assert client.post("/api/payouts", json={"expense_ids": [a]}).status_code == 200


def test_payment_webhooks_finish_abandoned_checkout_and_voided_hold(client):
    # Buyer approved and PayPal captured, but the browser never called /complete.
    cart = {"employee_id": CAROL, "vendor_id": "cloudstack", "items": [{"sku": "cs-seat", "quantity": 3}],
            "purpose": "Extra seats subscription for new analysts"}
    order = client.post("/api/paypal/orders", json=cart).json()
    get_paypal_client().capture_order(order["order_id"])
    got = webhook(client, "PAYMENT.CAPTURE.COMPLETED", {
        "id": "CAPTUREX", "custom_id": str(order["expense_id"]), "supplementary_data": {"related_ids": {"order_id": order["order_id"]}}})
    assert "created from webhook" in got["action"] and status_of(client, order["expense_id"]) == "approved"
    again = webhook(client, "PAYMENT.CAPTURE.COMPLETED", {"id": "CAPTUREX", "custom_id": str(order["expense_id"])})
    assert "already recorded" in again["action"]

    # A held authorization that PayPal voided (or that expired) can no longer be approved.
    hotel = {"employee_id": BOB, "vendor_id": "hotellumen", "items": [{"sku": "hl-1n", "quantity": 5}],
             "purpose": "Hotel for customer onsite visit in Denver, Jan 5 to Jan 10"}
    held = client.post("/api/paypal/orders", json=hotel).json()
    assert client.post(f"/api/paypal/orders/{held['order_id']}/complete").json()["status"] == "held"
    auth_id = get_paypal_client().orders[held["order_id"]]["purchase_units"][0]["payments"]["authorizations"][0]["id"]
    got = webhook(client, "PAYMENT.AUTHORIZATION.VOIDED", {"id": auth_id})
    assert "authorization voided" in got["action"] and status_of(client, held["expense_id"]) == "rejected"
    assert webhook(client, "CHECKOUT.ORDER.APPROVED", {"id": held["order_id"]})["action"] == "stored"


def test_unverified_and_malformed_events_change_nothing(client, monkeypatch):
    a, _ = approved_expense(client, ALICE)
    batch = client.post("/api/payouts", json={"expense_ids": [a]}).json()
    monkeypatch.setattr(get_paypal_client(), "verify_webhook_signature", lambda headers, event, webhook_id: False)
    got = webhook(client, "PAYMENT.PAYOUTS-ITEM.SUCCEEDED", {
        "payout_batch_id": batch["paypal_batch_id"], "transaction_status": "SUCCESS", "payout_item": {"sender_item_id": str(a)}})
    assert got == {"ok": True, "verified": False} and status_of(client, a) == "approved"
    assert client.post("/webhooks/paypal", content=b"not json").json()["ok"] is False
    assert client.post("/webhooks/paypal", json=["list"]).json()["ok"] is False


def test_nothing_to_pay(client):
    assert client.post("/api/payouts", json={"expense_ids": [999999]}).status_code == 409
