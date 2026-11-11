"""CPA-editable tax rates and category keywords on top of the reviewed rule pack."""
import pytest
from fastapi.testclient import TestClient

from app.rules import engine

ALICE = 1


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c
        for rule_id in ("meal_client", "other_business"):               # leave the shared test database clean
            c.delete(f"/api/tax-rules/{rule_id}")
        c.delete("/api/tax-categories/meal")
        c.delete("/api/tax-categories/office_supplies")


def rule(client, rule_id):
    return next(r for r in client.get("/api/tax-rules").json()["rules"] if r["id"] == rule_id)


def buy(client, cart):
    order = client.post("/api/paypal/orders", json=cart).json()
    done = client.post(f"/api/paypal/orders/{order['order_id']}/complete").json()
    row = next(r for r in client.get("/api/expenses").json() if r["id"] == done["expense_id"])
    return done, row


def test_defaults_come_from_the_rule_pack(client):
    view = client.get("/api/tax-rules").json()
    assert view["pack"] == {"id": "us_2026", "jurisdiction": "US", "tax_year": 2026, "form": "1120"}
    assert view["customized_count"] == 0
    meal = rule(client, "meal_client")
    assert (meal["default_pct"], meal["deductible_pct"], meal["customized"]) == (50, 50, False)
    assert "274" in meal["citation"]
    page = client.get("/tax-rules").text
    assert "Reviewed default" in page and "Category keywords" in page and "Change a deductible rate" in page


def test_cpa_rate_overrides_the_default_and_can_be_restored(client):
    changed = client.put("/api/tax-rules/meal_client", json={"deductible_pct": 40, "note": "Conservative position agreed with CPA"}).json()
    assert (changed["default_pct"], changed["deductible_pct"], changed["customized"]) == (50, 40, True)
    assert changed["override_note"] == "Conservative position agreed with CPA"
    assert engine.get_tax_rule("meal_client")["deductible_pct"] == 40

    cart = {"employee_id": ALICE, "vendor_id": "steakhouse21", "items": [{"sku": "st-dinner-1", "quantity": 1}],
            "purpose": "Dinner with Hooli buyer to discuss the pilot renewal", "attendees": "Sam Roe (Hooli buyer, prospect)"}
    done, row = buy(client, cart)
    assert done["status"] == "approved" and row["deductible_amount"] == 24.8                      # 40% of $62.00
    detail = client.get(f"/expenses/{done['expense_id']}").text
    assert "rate set by your CPA; reviewed default 50%" in detail
    cpa = {i["expense_id"]: i for i in client.get("/api/dashboard").json()["cpa_items"]}
    assert "rate set by your CPA" in " ".join(cpa[done["expense_id"]]["notes"])

    # Setting the rate back to the default removes the override; so does Restore default.
    assert client.put("/api/tax-rules/meal_client", json={"deductible_pct": 50}).json()["customized"] is False
    client.put("/api/tax-rules/meal_client", json={"deductible_pct": 30})
    restored = client.delete("/api/tax-rules/meal_client").json()
    assert (restored["deductible_pct"], restored["customized"]) == (50, False)
    assert engine.get_tax_rule("meal_client")["deductible_pct"] == 50
    assert client.post(f"/api/expenses/{done['expense_id']}/review").json()["lines"][0]["deductible_pct"] == 50


def test_rate_validation(client):
    assert client.put("/api/tax-rules/meal_client", json={"deductible_pct": 120}).status_code == 422
    assert client.put("/api/tax-rules/meal_client", json={"deductible_pct": -1}).status_code == 422
    assert client.put("/api/tax-rules/not_a_rule", json={"deductible_pct": 10}).status_code == 404
    assert client.delete("/api/tax-rules/not_a_rule").status_code == 404
    assert rule(client, "meal_client")["customized"] is False


def test_category_keywords_can_be_edited_and_restored(client):
    assert engine.categorize("Kombucha keg refill", "Brew Supply")[0] == "other"
    before = next(c for c in client.get("/api/tax-rules").json()["categories"] if c["id"] == "meal")
    saved = client.put("/api/tax-categories/meal", json={"keywords": before["keywords"] + ", Kombucha ,kombucha", "default_rule": "meal_convenience"}).json()
    assert saved["customized"] and saved["keywords"].endswith("kombucha") and saved["keywords"].count("kombucha") == 1
    assert saved["default_rule"] == "meal_convenience" and saved["base_rule"] == "meal_client"
    assert engine.categorize("Kombucha keg refill", "Brew Supply")[0] == "meal"
    assert engine.load_taxonomy()["categories"]["meal"]["default_rule"] == "meal_convenience"

    assert client.put("/api/tax-categories/meal", json={"keywords": "x", "default_rule": "nope"}).status_code == 400
    assert client.put("/api/tax-categories/nope", json={"keywords": "x"}).status_code == 404
    restored = client.delete("/api/tax-categories/meal").json()
    assert restored["customized"] is False and restored["keywords"] == before["keywords"]
    assert engine.categorize("Kombucha keg refill", "Brew Supply")[0] == "other"
    # Saving the unchanged defaults does not create a customization.
    same = client.put("/api/tax-categories/office_supplies", json={"keywords": next(c for c in client.get("/api/tax-rules").json()["categories"] if c["id"] == "office_supplies")["keywords"]}).json()
    assert same["customized"] is False


def test_overrides_survive_a_reload_from_the_database(client):
    from sqlmodel import Session

    from app.db import engine as db_engine

    client.put("/api/tax-rules/other_business", json={"deductible_pct": 0, "note": "Hold until reviewed"})
    engine.set_overrides({}, {})
    assert engine.get_tax_rule("other_business")["deductible_pct"] == 100
    with Session(db_engine) as session:
        engine.reload_overrides(session)                                   # what startup does
    assert engine.get_tax_rule("other_business")["deductible_pct"] == 0
    client.delete("/api/tax-rules/other_business")
