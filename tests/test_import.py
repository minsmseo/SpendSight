"""Channel C: PayPal Transaction Search import (mock PayPal) and the transaction parser."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.extract.paypal_receipt import receipt_from_transaction
from app.models import utcnow
from app.paypal import get_paypal_client
from app.paypal.client import PayPalError

ALICE = 1


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def last_days(n):
    """An n-day range ending today (UTC, like the dates sent to PayPal)."""
    today = utcnow().date()
    return {"start_date": (today - timedelta(days=n - 1)).isoformat(), "end_date": today.isoformat()}


def row(client, expense_id):
    return next(r for r in client.get("/api/expenses").json() if r["id"] == expense_id)


def test_parser_reads_a_transaction_search_entry():
    # Shape from the PayPal Transaction Search reference.
    detail = {
        "transaction_info": {
            "paypal_account_id": "6STWC2LSUYYYE", "transaction_id": "5TY05013RG002845M", "transaction_event_code": "T0006",
            "transaction_initiation_date": "2026-07-11T04:03:52+0000", "transaction_updated_date": "2026-07-11T04:03:52+0000",
            "transaction_amount": {"currency_code": "USD", "value": "-465.00"}, "fee_amount": {"currency_code": "USD", "value": "-13.79"},
            "transaction_status": "S", "transaction_subject": "Bill for your purchase", "invoice_id": "INV-9", "custom_field": "abc",
        },
        "payer_info": {"account_id": "6STWC2LSUYYYE", "email_address": "consumer@example.com",
                       "payer_name": {"given_name": "test", "surname": "consumer", "alternate_full_name": "Acme Supplies"}},
        "cart_info": {"item_details": [
            {"item_code": "ItemCode", "item_name": "Item1", "item_quantity": "2",
             "item_unit_price": {"currency_code": "USD", "value": "50.00"}, "item_amount": {"currency_code": "USD", "value": "100.00"}},
            {"item_name": "Item2", "item_quantity": "1", "item_unit_price": {"currency_code": "USD", "value": "365.00"}},
        ]},
    }
    r = receipt_from_transaction(detail)
    assert (r["transaction_id"], r["kind"], r["completed"], r["outgoing"]) == ("5TY05013RG002845M", "payment", True, True)
    assert (r["vendor"], r["total"], r["currency"]) == ("Acme Supplies", 465.0, "USD")
    assert r["purchased_at"].isoformat() == "2026-07-11T04:03:52+00:00"
    assert [(i["name"], i["quantity"], i["amount"]) for i in r["items"]] == [("Item1", 2.0, 100.0), ("Item2", 1.0, 365.0)]

    bare = receipt_from_transaction({"transaction_info": {"transaction_id": "X1", "transaction_event_code": "T0001",
                                                          "transaction_amount": {"currency_code": "USD", "value": "-20.00"}, "transaction_status": "P"}})
    assert (bare["kind"], bare["completed"], bare["vendor"]) == ("payout", False, "PayPal transaction")
    assert bare["items"] == [{"name": "PayPal transaction purchase", "quantity": 1.0, "unit_amount": 20.0, "amount": 20.0}]
    assert receipt_from_transaction({})["transaction_id"] == ""


def test_import_creates_needs_info_expenses_once(client):
    page = client.get("/finance/import").text
    assert "Import PayPal history" in page and "3 hours or more" in page and "at most 31 days" in page

    first = client.post("/api/paypal/import", json={**last_days(31), "employee_id": ALICE}).json()
    assert first["imported"] == 3 and first["skipped"]["payouts"] == 1 and first["skipped"]["not_completed"] == 1
    assert first["skipped"]["already_recorded"] == 0 and first["mode"] == "mock"
    rows = [row(client, eid) for eid in first["expense_ids"]]
    assert {r["vendor"] for r in rows} == {"CloudStack SaaS", "SkyAir", "Coffee & Co"}
    assert {(r["source"], r["status"], r["employee"]) for r in rows} == {("import", "needs_info", "Alice Chen")}
    assert all("business purpose" in r["question"] for r in rows)
    assert {r["category"] for r in rows} == {"software", "travel_transport", "meal"}

    again = client.post("/api/paypal/import", json={**last_days(31), "employee_id": ALICE}).json()
    assert again["imported"] == 0 and again["skipped"]["already_recorded"] == 3

    # The employee supplies the purpose and the expense is reviewed like any other.
    flight = next(r for r in rows if r["vendor"] == "SkyAir")
    done = client.post(f"/api/expenses/{flight['id']}/answer", json={"answer": "Flight for customer onsite visit in Seattle"}).json()
    assert done["status"] == "approved" and row(client, flight["id"])["deductible_amount"] == 412.6


def test_store_purchases_are_not_imported_twice(client):
    cart = {"employee_id": 3, "vendor_id": "cloudstack", "items": [{"sku": "cs-seat", "quantity": 7}],
            "purpose": "Extra seats subscription for the marketing analysts"}
    order = client.post("/api/paypal/orders", json=cart).json()
    client.post(f"/api/paypal/orders/{order['order_id']}/complete")
    before = len(client.get("/api/expenses").json())
    res = client.post("/api/paypal/import", json={**last_days(3), "employee_id": ALICE, "include_incoming": True}).json()
    assert res["skipped"]["already_recorded"] >= 1 and res["skipped"]["incoming"] == 0
    assert len(client.get("/api/expenses").json()) == before + res["imported"]
    without = client.post("/api/paypal/import", json={**last_days(3), "employee_id": ALICE}).json()
    assert without["imported"] == 0


def test_range_and_error_handling(client, monkeypatch):
    bad = [({"start_date": "2026-10-01", "end_date": "2026-11-05"}, "at most 31 days"),
           ({"start_date": "2026-11-05", "end_date": "2026-11-01"}, "before start"),
           ({"start_date": "11/05/2026", "end_date": "2026-11-06"}, "YYYY-MM-DD")]
    for dates, message in bad:
        res = client.post("/api/paypal/import", json={**dates, "employee_id": ALICE})
        assert res.status_code == 400 and message in res.json()["detail"]
    assert client.post("/api/paypal/import", json={**last_days(5), "employee_id": 999}).status_code == 400
    ok = client.post("/api/paypal/import", json={"start_date": "2026-10-01", "end_date": "2026-10-31", "employee_id": ALICE})
    assert ok.status_code == 200                                                    # exactly 31 days is allowed

    def denied(start, end, page=1):
        raise PayPalError(403, {"name": "NOT_AUTHORIZED"})

    monkeypatch.setattr(get_paypal_client(), "search_transactions", denied)
    res = client.post("/api/paypal/import", json={**last_days(5), "employee_id": ALICE})
    assert res.status_code == 502 and "NOT_AUTHORIZED" in res.json()["detail"] and "Enable Transaction Search" in res.json()["detail"]
