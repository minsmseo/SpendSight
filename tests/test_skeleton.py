import time

import pytest
from fastapi.testclient import TestClient

from app.extract.paypal_receipt import receipt_from_order
from app.paypal import mock as paypal_mock
from app.paypal.client import PayPalError
from app.rules import engine

SPEC_RULE_IDS = {
    "meal_client", "meal_travel", "meal_convenience", "event_all_staff", "entertainment", "gift",
    "travel_transport", "lodging", "software", "office_supplies", "advertising", "fines_penalties",
    "political", "personal",
}


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_rule_pack_complete():
    pack = engine.load_rule_pack()
    ids = {r["id"] for r in pack["rules"]}
    assert SPEC_RULE_IDS <= ids
    assert all(r["verify_with_cpa"] for r in pack["rules"])
    assert engine.get_tax_rule("meal_client")["deductible_pct"] == 50
    assert engine.get_tax_rule("entertainment")["deductible_pct"] == 0


def test_industry_override_office_snacks():
    assert engine.get_tax_rule("meal_convenience", "software_saas")["deductible_pct"] == 0
    assert engine.get_tax_rule("meal_convenience", "restaurant")["deductible_pct"] == 100
    assert len(engine.load_industries()) == 5


def test_taxonomy_rules_exist():
    ids = {r["id"] for r in engine.load_rule_pack()["rules"]}
    for cat in engine.load_taxonomy()["categories"].values():
        assert cat["default_rule"] in ids


def test_pages_and_seed(client):
    assert client.get("/healthz").json()["ok"] is True
    rows = client.get("/api/expenses").json()
    assert len(rows) == 150
    assert {r["employee"] for r in rows} == {"Alice Chen", "Bob Martinez", "Carol Singh"}
    for path in ("/finance/queue", "/employee", "/store", "/onboarding", "/reimburse", "/reports/tax",
                 "/settings", f"/expenses/{rows[0]['id']}"):
        assert client.get(path).status_code == 200, path
    assert client.get("/config").json()["paypal_mode"] == "mock"
    assert client.get("/api/status").json()["ai"]["state"] == "not_set"


def test_webhook_idempotent(client):
    event = {"id": "WH-TEST-1", "event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {}}
    assert client.post("/webhooks/paypal", json=event).json() == {"ok": True, "verified": True}
    assert client.post("/webhooks/paypal", json=event).json() == {"ok": True, "duplicate": True}


ITEMS = [{"name": "Dinner for two", "unit_amount": 148.0, "quantity": 1, "sku": "st-dinner-2"},
         {"name": "Wine pairing", "unit_amount": 38.0, "quantity": 1, "sku": "st-wine"}]


def test_mock_order_capture_to_receipt():
    pp = paypal_mock.MockPayPalClient()
    order = pp.create_order("CAPTURE", ITEMS, "42", "INV-42", "Steakhouse 21")
    captured = pp.capture_order(order["id"])
    receipt = receipt_from_order(captured)
    assert receipt["vendor"] == "Steakhouse 21"
    assert receipt["total"] == 186.0
    assert receipt["custom_id"] == "42"
    assert receipt["paypal_capture_id"]
    assert len(receipt["items"]) == 2
    with pytest.raises(PayPalError):
        pp.capture_order(order["id"])


def test_mock_authorize_capture_and_void():
    pp = paypal_mock.MockPayPalClient()
    hotel = [{"name": "Hotel room, 3 nights", "unit_amount": 1150.0, "quantity": 1}]
    a1 = pp.authorize_order(pp.create_order("AUTHORIZE", hotel, "1", "INV-1", "Hotel Lumen")["id"])
    auth_id = receipt_from_order(a1)["paypal_authorization_id"]
    assert pp.capture_authorization(auth_id)["status"] == "COMPLETED"
    with pytest.raises(PayPalError):
        pp.void_authorization(auth_id)
    a2 = pp.authorize_order(pp.create_order("AUTHORIZE", hotel, "2", "INV-2", "Hotel Lumen")["id"])
    assert pp.void_authorization(receipt_from_order(a2)["paypal_authorization_id"])["status"] == "VOIDED"


def test_mock_payout_lifecycle(monkeypatch):
    monkeypatch.setattr(paypal_mock, "PAYOUT_DELAY_SECONDS", 0.2)
    pp = paypal_mock.MockPayPalClient()
    items = [{"receiver_email": "a@example.com", "amount": 93.5, "sender_item_id": "1,2"}]
    batch_id = pp.create_payout("batch-1", items)["batch_header"]["payout_batch_id"]
    assert pp.get_payout(batch_id)["batch_header"]["batch_status"] == "PROCESSING"
    time.sleep(0.3)
    done = pp.get_payout(batch_id)
    assert done["batch_header"]["batch_status"] == "SUCCESS"
    assert done["items"][0]["transaction_status"] == "SUCCESS"
    with pytest.raises(PayPalError):
        pp.create_payout("batch-1", items)
