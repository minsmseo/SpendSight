"""Vendor Store checkout in mock mode: order -> receipt -> review, and held authorizations."""
import pytest
from fastapi.testclient import TestClient

ALICE, CAROL = 1, 3
HOTEL = {"employee_id": CAROL, "vendor_id": "hotellumen", "items": [{"sku": "hl-3n", "quantity": 1}],
         "purpose": "Hotel for DataCon conference in Las Vegas, Dec 1 to Dec 4"}


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def buy(client, cart):
    order = client.post("/api/paypal/orders", json=cart)
    assert order.status_code == 200, order.text
    done = client.post(f"/api/paypal/orders/{order.json()['order_id']}/complete")
    assert done.status_code == 200, done.text
    return order.json(), done.json()


def row(client, expense_id):
    return next(r for r in client.get("/api/expenses").json() if r["id"] == expense_id)


def test_purchase_becomes_reviewed_expense_without_manual_entry(client):
    cart = {"employee_id": ALICE, "vendor_id": "steakhouse21",
            "items": [{"sku": "st-dinner-1", "quantity": 1}, {"sku": "st-wine", "quantity": 1}],
            "purpose": "Dinner with Initech VP to discuss pilot renewal", "attendees": "Sam Lee (Initech VP, prospect)"}
    assert client.post("/api/paypal/quote", json=cart).json() == {"total": 100.0, "tax": 0.0, "intent": "CAPTURE"}
    order, done = buy(client, cart)
    assert order["intent"] == "CAPTURE"
    assert (done["status"], done["engine"]) == ("approved", "rules")
    r = row(client, done["expense_id"])
    assert (r["vendor"], r["source"], r["total"], r["deductible_amount"]) == ("Steakhouse 21", "paypal", 100.0, 50.0)
    # completing twice does not capture twice
    again = client.post(f"/api/paypal/orders/{order['order_id']}/complete").json()
    assert again["already_completed"] is True and again["status"] == "approved"


def test_cart_is_validated_and_drafts_stay_out_of_the_queue(client):
    base = {"employee_id": ALICE, "vendor_id": "coffeeco", "items": [{"sku": "cf-latte", "quantity": 1}]}
    assert client.post("/api/paypal/orders", json={**base, "purpose": "  "}).status_code == 400
    assert client.post("/api/paypal/orders", json={**base, "purpose": "x", "vendor_id": "nope"}).status_code == 400
    bad_item = {**base, "purpose": "x", "items": [{"sku": "hl-3n", "quantity": 1}]}
    assert client.post("/api/paypal/orders", json=bad_item).status_code == 400
    unpaid = client.post("/api/paypal/orders", json={**base, "purpose": "Coffee with prospect about pilot"}).json()
    assert all(r["id"] != unpaid["expense_id"] for r in client.get("/api/expenses").json())
    assert client.post("/api/paypal/orders/NOPE/complete").status_code == 404


def test_high_value_hotel_is_held_then_captured_on_approval(client):
    assert client.post("/api/paypal/quote", json=HOTEL).json()["intent"] == "AUTHORIZE"
    order, done = buy(client, HOTEL)
    assert order["intent"] == "AUTHORIZE" and done["status"] == "held"
    eid = done["expense_id"]
    assert "Held by" in client.get(f"/expenses/{eid}").text
    captured = client.post(f"/api/expenses/{eid}/capture-auth").json()
    assert captured["status"] == "approved" and captured["paypal_capture_id"]
    assert row(client, eid)["status"] == "approved"
    assert client.post(f"/api/expenses/{eid}/capture-auth").status_code == 409
    assert client.post(f"/api/expenses/{eid}/void-auth").status_code == 409


def test_held_purchase_rejected_is_voided(client):
    _, done = buy(client, HOTEL)
    eid = done["expense_id"]
    assert done["status"] == "held"
    assert client.post(f"/api/expenses/{eid}/void-auth").json()["status"] == "rejected"
    assert client.post(f"/api/expenses/{eid}/capture-auth").status_code == 409


def test_gym_membership_bought_through_paypal_is_rejected(client):
    _, done = buy(client, {"employee_id": ALICE, "vendor_id": "fitnessclub",
                           "items": [{"sku": "fc-month", "quantity": 1}], "purpose": "team wellness"})
    assert done["status"] == "rejected"
