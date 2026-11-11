"""Rule engine on the demo cases from CLAUDE.md section 12 (no AI key)."""
from datetime import timedelta

import pytest
from sqlmodel import Session, select

from app.ai import agent
from app.ai import provider as ai_provider
from app.db import engine as db_engine
from app.db import init_db
from app.models import Company, Expense, FollowUp, LineItem, utcnow
from app.routers.reports import tax_summary
from app.rules import engine
from seed import seed_if_empty

ALICE, BOB, CAROL = 1, 2, 3
DINNER = [("Dinner for two (prix fixe)", 148.0), ("Wine pairing", 38.0)]
DINNER_PURPOSE = "Dinner with Globex CTO to discuss renewal"


@pytest.fixture(scope="module")
def session():
    init_db()
    seed_if_empty()
    with Session(db_engine) as s:
        yield s


def weekday_noon(days_ago: int = 1):
    """A recent weekday at 20:00 UTC (noon in the company time zone)."""
    day = utcnow() - timedelta(days=days_ago)
    while engine.to_local(day.replace(hour=20)).weekday() >= 5:
        day -= timedelta(days=1)
    return day.replace(hour=20, minute=0, second=0, microsecond=0)


def make(session, employee_id, vendor, items, purpose, attendees="", source="paypal", when=None, **extra):
    exp = Expense(
        employee_id=employee_id, source=source, status="draft", vendor=vendor,
        purchased_at=when or weekday_noon(), total=sum(a for _, a in items),
        purpose=purpose, attendees=attendees, **extra,
    )
    session.add(exp)
    session.flush()
    for name, amount in items:
        session.add(LineItem(expense_id=exp.id, name=name, unit_amount=amount, amount=amount))
    session.commit()
    return exp


def deductible(result):
    return round(sum(ln["deductible_amount"] for ln in result["lines"]), 2)


def test_case1_client_dinner_approved_50pct(session):
    exp = make(session, ALICE, "Steakhouse 21", DINNER, DINNER_PURPOSE, "Jane Doe (Globex CTO, customer), Alice Chen")
    r = engine.review_expense(session, exp.id)
    assert (r["decision"], r["reimbursable"], r["engine"]) == ("approved", True, "rules")
    assert {ln["rule_id"] for ln in r["lines"]} == {"meal_client"}
    assert deductible(r) == 93.0
    assert exp.status == "approved"


def test_case2_dinner_without_attendees_asks_then_approves(session):
    exp = make(session, CAROL, "Steakhouse 21", DINNER, DINNER_PURPOSE)
    r = engine.review_expense(session, exp.id)
    assert r["decision"] == "needs_info"
    assert "attended" in r["question"] and "relationship" in r["question"]
    assert "attendees" in r["missing"]
    followup = session.exec(select(FollowUp).where(FollowUp.expense_id == exp.id)).one()
    followup.answer = "Jane Doe, CTO of Globex, our customer"
    session.add(followup)
    session.commit()
    r = engine.review_expense(session, exp.id)
    assert r["decision"] == "approved" and deductible(r) == 93.0


def test_case3_office_snacks_0pct_but_deductible_for_restaurant(session):
    snacks = [("Office coffee box (12 cups)", 34.0), ("Assorted office snacks", 30.0)]
    exp = make(session, BOB, "Coffee & Co", snacks, "Snacks and coffee for the office", source="upload")
    r = engine.review_expense(session, exp.id)
    assert (r["decision"], r["reimbursable"]) == ("approved", True)
    assert {ln["rule_id"] for ln in r["lines"]} == {"meal_convenience"}
    assert deductible(r) == 0.0
    company = session.exec(select(Company)).first()
    company.industry_id = "restaurant"
    session.add(company)
    session.commit()
    try:
        assert deductible(engine.review_expense(session, exp.id)) == 64.0
    finally:
        company.industry_id = "software_saas"
        session.add(company)
        session.commit()


def test_case4_stadium_tickets_with_client_0pct(session):
    exp = make(session, ALICE, "Stadium Tickets", [("Game tickets (2 seats)", 420.0)],
               "Game tickets to host Globex client and discuss expansion", "Jane Doe (Globex CTO)")
    r = engine.review_expense(session, exp.id)
    assert {ln["rule_id"] for ln in r["lines"]} == {"entertainment"}
    assert deductible(r) == 0.0
    assert r["decision"] == "flagged" and "no_preapproval" in r["flags"]   # above the $300 pre-approval line
    assert r["reimbursable"] is True


def test_case5_split_purchase_flagged(session):
    item = [("Laptop accessory bundle (dock + cables)", 240.0)]
    purpose = "Laptop accessories for new engineer workstation"
    when = weekday_noon(2)
    make_first = make(session, BOB, "Office Depotish", item, purpose, when=when)
    engine.review_expense(session, make_first.id)
    second = make(session, BOB, "Office Depotish", item, purpose, when=when + timedelta(hours=1))
    r = engine.review_expense(session, second.id)
    assert r["decision"] == "flagged"
    assert "split_purchase" in {a["rule"] for a in r["anomalies"]}


def test_case6_high_value_hotel_is_held(session):
    exp = make(session, ALICE, "Hotel Lumen", [("Hotel room, 3 nights (conference rate)", 1150.0)],
               "Hotel for DataCon conference in Las Vegas, Dec 1 to Dec 4",
               intent="AUTHORIZE", paypal_authorization_id="AUTH-TEST-1")
    r = engine.review_expense(session, exp.id)
    assert r["status"] == "held" and exp.status == "held"
    assert "no_preapproval" not in r["flags"]
    assert deductible(r) == 1150.0


def test_case7_gym_membership_rejected(session):
    exp = make(session, BOB, "Fitness Club", [("Gym membership (monthly)", 79.0)], "team wellness")
    r = engine.review_expense(session, exp.id)
    assert (r["decision"], r["reimbursable"]) == ("rejected", False)
    assert deductible(r) == 0.0 and exp.status == "rejected"


def test_case9_tax_report_totals(session):
    s = tax_summary(session)
    assert round(s["deductible"] + s["nondeductible"], 2) == s["spent"]
    meal = next(r for r in s["rows"] if r["category"] == "meal")
    assert meal["nondeductible"] > 0


def test_imported_row_without_purpose_asks(session):
    exp = make(session, CAROL, "CloudStack SaaS", [("Team plan subscription (monthly)", 199.0)], "", source="import")
    r = engine.review_expense(session, exp.id)
    assert r["decision"] == "needs_info" and "business purpose" in r["question"]


def test_two_unanswered_rounds_then_flagged(session):
    exp = make(session, CAROL, "Coffee & Co", [("Latte", 5.5)], "coffee")
    for answer in ("misc", "misc"):
        assert engine.review_expense(session, exp.id)["decision"] == "needs_info"
        f = session.exec(select(FollowUp).where(FollowUp.expense_id == exp.id, FollowUp.answer == None)).one()  # noqa: E711
        f.answer = answer
        session.add(f)
        session.commit()
    assert engine.review_expense(session, exp.id)["decision"] == "flagged"


def test_provider_credit_error_falls_back_to_rules(session, monkeypatch):
    def boom(provider, session, expense_id):
        raise ai_provider.ProviderError("credit", "credit balance is too low")

    monkeypatch.setattr(ai_provider, "get_provider", lambda: object())
    monkeypatch.setattr(agent, "_run_agent", boom)
    exp = make(session, CAROL, "CloudStack SaaS", [("Additional seat", 29.0)], "Extra seat subscription for new analyst")
    try:
        r = agent.review(session, exp.id)
        assert r["engine"] == "rules" and r["decision"] == "approved"
        assert ai_provider.is_degraded()
    finally:
        ai_provider.mark_degraded("", seconds=0)
