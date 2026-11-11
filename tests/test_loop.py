"""Full review loop through the HTTP API: follow-up answers, Finance actions, upload, grid edits."""
import pytest
from fastapi.testclient import TestClient

BOB, CAROL = 2, 3


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def row(client, expense_id):
    return next(r for r in client.get("/api/expenses").json() if r["id"] == expense_id)


def buy(client, cart):
    order = client.post("/api/paypal/orders", json=cart).json()
    return client.post(f"/api/paypal/orders/{order['order_id']}/complete").json()


def upload(client, employee_id, receipt, **form):
    with open(f"demo/receipts/{receipt}", "rb") as fh:
        page = client.post("/expenses/upload", data={"employee_id": employee_id}, files={"receipt": (receipt, fh, "image/png")})
    assert page.status_code == 200 and "Confirm receipt details" in page.text
    name = page.text.split('name="receipt_name" value="')[1].split('"')[0]
    digest = page.text.split('name="receipt_hash" value="')[1].split('"')[0]
    done = client.post("/expenses/submit", data={"employee_id": employee_id, "receipt_name": name, "receipt_hash": digest, **form},
                       follow_redirects=False)
    assert done.status_code == 303, done.text
    eid = int(done.headers["location"].rsplit("/", 1)[1])
    assert client.get(f"/uploads/{name}").status_code == 200
    return eid


def test_employee_answers_agent_question_and_gets_approved(client):
    done = buy(client, {"employee_id": CAROL, "vendor_id": "steakhouse21", "items": [{"sku": "st-dinner-1", "quantity": 1}],
                        "purpose": "Dinner with Umbrella buyer to discuss renewal"})
    eid = done["expense_id"]
    assert done["status"] == "needs_info" and "attended" in done["question"]
    assert "attended" in row(client, eid)["question"]
    assert "Who attended" in client.get(f"/employee?employee_id={CAROL}").text
    assert client.post(f"/api/expenses/{eid}/answer", json={"answer": " "}).status_code == 400
    answered = client.post(f"/api/expenses/{eid}/answer", json={"answer": "Pat Kim, buyer at Umbrella, our customer"}).json()
    assert answered["status"] == "approved"
    assert row(client, eid)["deductible_amount"] == 31.0 and row(client, eid)["question"] is None
    assert client.post(f"/api/expenses/{eid}/answer", json={"answer": "again"}).status_code == 409
    assert client.get(f"/expenses/{eid}").text.count("Pat Kim") >= 2   # thread and attendees field


def test_upload_then_finance_asks_approves_and_rejects(client):
    eid = upload(client, BOB, "client_dinner.png", vendor="Stadium Tickets", purchased_on="2026-11-10", total="420",
                 items="Game tickets (2 seats), 420.00", purpose="Game tickets to host Initech client and discuss expansion",
                 attendees="Sam Lee (Initech VP)")
    r = row(client, eid)
    assert (r["status"], r["source"], r["deductible_amount"], r["engine"]) == ("flagged", "upload", 0.0, "rules")

    asked = client.post(f"/api/expenses/{eid}/ask", json={"question": "Was this approved by your manager?"}).json()
    assert asked["status"] == "needs_info"
    assert row(client, eid)["question"] == "Was this approved by your manager?"
    assert client.post(f"/api/expenses/{eid}/answer", json={"answer": "Yes, approved by the VP of Sales"}).json()["status"] == "flagged"

    assert client.post(f"/api/expenses/{eid}/approve", json={"note": "Client event, VP approved"}).json()["status"] == "approved"
    r = row(client, eid)
    assert (r["finance"], r["reimbursable"], r["engine"]) == ("approved", True, "rules")
    page = client.get(f"/expenses/{eid}").text
    assert "Client event, VP approved" in page and "/uploads/" in page
    assert client.post(f"/api/expenses/{eid}/approve").status_code == 409
    assert client.post(f"/api/expenses/{eid}/reject").json()["status"] == "rejected"
    assert row(client, eid)["reimbursable"] is False


def test_finance_corrects_category_in_grid(client):
    eid = upload(client, CAROL, "office_snacks.png", vendor="Coffee & Co", purchased_on="2026-11-10", total="34",
                 items="Office coffee box (12 cups), 34.00", purpose="Snacks and coffee for the office")
    assert (row(client, eid)["category"], row(client, eid)["deductible_amount"]) == ("meal", 0.0)
    assert client.patch(f"/api/expenses/{eid}/category", json={"category": "nope"}).status_code == 400
    assert client.patch(f"/api/expenses/{eid}/category", json={"category": "office_supplies"}).status_code == 200
    assert (row(client, eid)["category"], row(client, eid)["deductible_amount"]) == ("office_supplies", 34.0)
    client.post(f"/api/expenses/{eid}/review")            # a later re-review keeps the correction
    assert row(client, eid)["category"] == "office_supplies"


def test_approve_and_reject_handle_held_authorizations(client):
    cart = {"employee_id": BOB, "vendor_id": "hotellumen", "items": [{"sku": "hl-1n", "quantity": 4}],
            "purpose": "Hotel for customer onsite visit in Austin, Dec 8 to Dec 12"}
    first = buy(client, cart)
    assert first["status"] == "held"
    assert client.post(f"/api/expenses/{first['expense_id']}/approve").json()["status"] == "approved"
    assert "Capture" in client.get(f"/expenses/{first['expense_id']}").text
    second = buy(client, cart)
    assert client.post(f"/api/expenses/{second['expense_id']}/reject").json()["status"] == "rejected"
    assert row(client, second["expense_id"])["finance"] == "rejected"


def test_chart_and_bad_uploads(client):
    seeded = client.get("/api/expenses?status=paid").json()[0]
    series = client.get(f"/api/expenses/{seeded['id']}/chart").json()
    assert len(series["points"]) >= 8 and series["ucl3"] > series["mean"] > 0
    assert series["current"]["amount"] == seeded["total"]
    bad = client.post("/expenses/upload", data={"employee_id": 1}, files={"receipt": ("x.exe", b"MZ", "application/octet-stream")})
    assert bad.status_code == 400
    escape = client.post("/expenses/submit", data={"employee_id": 1, "vendor": "X", "purchased_on": "2026-11-10", "total": "5",
                                                   "receipt_name": "../spendsight.db"}, follow_redirects=False)
    assert escape.status_code == 400
    assert client.get("/reimburse?ids=1,2,abc").status_code == 200
