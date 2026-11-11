"""Finance dashboard: outcome split, action counts and the CPA review list."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_dashboard_numbers_add_up(client):
    d = client.get("/api/dashboard").json()
    assert [s["key"] for s in d["split"]] == ["deductible", "nondeductible", "review", "rejected"]
    assert round(sum(s["amount"] for s in d["split"]), 2) == d["submitted"]
    assert abs(sum(s["pct"] for s in d["split"]) - 100) < 0.3
    assert d["deductible_pct"] == d["split"][0]["pct"] and 50 < d["deductible_pct"] <= 100
    assert d["review_count"] == sum(q["count"] for q in d["queue"].values())
    assert sum(d["sources"].values()) == d["submitted_count"] == sum(d["engines"].values())
    assert round(sum(m["deductible"] + m["nondeductible"] for m in d["months"]), 2) == round(d["deductible"] + d["nondeductible"], 2)
    assert [m["month"] for m in d["months"]] == sorted(m["month"] for m in d["months"])
    report = client.get("/api/reports/tax").json()
    assert (d["deductible"], d["nondeductible"], d["missing_count"]) == (report["deductible"], report["nondeductible"], report["missing_count"])
    assert client.get("/api/dashboard?period=01").json()["submitted"] == 0


def test_cpa_review_list_explains_each_item(client):
    items = {i["vendor"]: i for i in client.get("/api/dashboard").json()["cpa_items"]}
    assert "$25 per recipient" in items["Gift Box Co"]["notes"][0] and "Only $25.00 of $60.00" in items["Gift Box Co"]["notes"][0]
    assert "Entertainment is 0% deductible" in items["Stadium Tickets"]["notes"][0]
    assert "No specific tax rule matched" in items["Parcel Post"]["notes"][0]
    assert "open to all staff" in items["Lakeside Events"]["notes"][0]
    assert all(i["engine"] in ("rules", "llm") and i["notes"] for i in items.values())
    # Ordinary, certain treatments (software, client meals at 50%) are not on the list.
    assert "CloudStack SaaS" not in items and "SkyAir" not in items


def test_dashboard_reflects_the_review_pipeline(client):
    before = client.get("/api/dashboard").json()
    cart = {"employee_id": 3, "vendor_id": "steakhouse21", "items": [{"sku": "st-dinner-1", "quantity": 2}],
            "purpose": "Dinner with Vandelay partners to discuss the reseller deal"}
    order = client.post("/api/paypal/orders", json=cart).json()
    assert client.post(f"/api/paypal/orders/{order['order_id']}/complete").json()["status"] == "needs_info"
    after = client.get("/api/dashboard").json()
    assert after["queue"]["needs_info"]["count"] == before["queue"]["needs_info"]["count"] + 1
    assert round(after["split"][2]["amount"] - before["split"][2]["amount"], 2) == 124.0
    assert after["deductible"] == before["deductible"]                 # nothing is deductible until it is accepted


def test_dashboard_page_and_home_redirect(client):
    page = client.get("/dashboard").text
    for text in ("Deductible share of submitted spend", "Confirm with your CPA", "Needs a decision", "Not tax advice", 'id="splitChart"'):
        assert text in page
    assert client.get("/", follow_redirects=False).headers["location"] == "/dashboard"
    assert client.get("/role/finance", follow_redirects=False).headers["location"] == "/dashboard"
    assert client.get("/role/employee", follow_redirects=False).headers["location"] == "/employee"
    for path in ("/dashboard", "/finance/queue", "/employee", "/store", "/onboarding", "/reimburse", "/reports/tax", "/settings", "/finance/import"):
        html = client.get(path).text
        assert not any(ch in html for ch in "⚠\U0001F464✅❌"), path     # no emoji in the UI
