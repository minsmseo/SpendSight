"""Seed: Acme Analytics Inc. (software_saas), 3 employees, 90 days of normal history (~150 rows)
so SPC control charts have data. Deterministic (fixed random seed).

Run directly to reset and reseed:  python seed.py --reset
"""
import json
import random
import sys
from datetime import datetime, timedelta

from sqlmodel import Session, SQLModel, select

from app.db import engine as db_engine
from app.db import init_db
from app.models import Company, Decision, Employee, Expense, LineItem, utcnow
from app.rules import engine as rules

EMPLOYEES = [
    dict(name="Alice Chen", email="alice@acme-analytics.example", paypal_email="alice-employee@personal.example.com",
         department="Sales", role_title="Account Executive", default_project="Globex renewal"),
    dict(name="Bob Martinez", email="bob@acme-analytics.example", paypal_email="bob-employee@personal.example.com",
         department="Engineering", role_title="Senior Software Engineer", default_project="Platform"),
    dict(name="Carol Singh", email="carol@acme-analytics.example", paypal_email="carol-employee@personal.example.com",
         department="Marketing", role_title="Marketing Manager", default_project="Q4 launch"),
]

# (vendor, category, rule_id, item name, low, high, purpose, attendees, weight per employee index)
PATTERNS = [
    ("CloudStack SaaS", "software", "software", "Team plan subscription", 49, 299,
     "Monthly subscription for analytics tooling", "", (1, 4, 2)),
    ("Steakhouse 21", "meal", "meal_client", "Client dinner", 80, 210,
     "Dinner with client to discuss contract renewal", "Client contact (customer), self", (4, 0, 1)),
    ("Coffee & Co", "meal", "meal_client", "Coffee meeting", 9, 32,
     "Coffee with prospect to discuss pilot project", "Prospect contact, self", (3, 1, 2)),
    ("SkyAir", "travel_transport", "travel_transport", "Economy airfare", 220, 640,
     "Flight for customer onsite visit", "", (3, 1, 1)),
    ("RideNow", "travel_transport", "travel_transport", "Rideshare", 12, 58,
     "Rideshare to client office during business trip", "", (3, 1, 2)),
    ("Hotel Lumen", "lodging", "lodging", "Hotel room", 180, 420,
     "Hotel for customer onsite visit", "", (2, 1, 1)),
    ("Office Depotish", "office_supplies", "office_supplies", "Office supplies", 15, 180,
     "Supplies and accessories for workstation", "", (1, 3, 2)),
    ("AdReach", "advertising", "advertising", "Sponsored campaign", 100, 500,
     "Ad spend for product launch campaign", "", (0, 0, 4)),
]


def _seed_receipt(expense_id: int, vendor: str, when: datetime, item_name: str, amount: float) -> str | None:
    """Render a receipt image that matches the seeded expense, so history looks like real uploads."""
    try:
        from app import config
        from demo.make_receipts import render

        config.UPLOAD_DIR.mkdir(exist_ok=True)
        name = f"seed-{expense_id}.png"
        render(name, vendor.upper(), rules.to_local(when).strftime("%m/%d/%Y %I:%M %p"), [(item_name, amount)], 0.0,
               out_dir=config.UPLOAD_DIR)
        return name
    except Exception:
        return None                                    # no Pillow / read-only disk: seed still works


def _add_expense(session: Session, rng: random.Random, emp: Employee, when: datetime, now: datetime) -> None:
    idx = emp.id - 1
    pattern = rng.choices(PATTERNS, weights=[p[8][idx % 3] for p in PATTERNS])[0]
    vendor, category, rule_id, item_name, low, high, purpose, attendees, _ = pattern
    amount = round(rng.uniform(low, high), 2)
    rule = rules.get_tax_rule(rule_id, "software_saas")
    pct = float(rule["deductible_pct"])
    source = "paypal" if rng.random() < 0.6 else "upload"
    status = "paid" if (now - when).days > 14 else "approved"
    exp = Expense(
        employee_id=emp.id, source=source, status=status, vendor=vendor, purchased_at=when,
        total=amount, tax_amount=0.0, purpose=purpose, attendees=attendees,
        paypal_order_id=f"SEED{rng.randrange(10**12):012d}" if source == "paypal" else None,
        paypal_capture_id=f"SEEDCAP{rng.randrange(10**10):010d}" if source == "paypal" else None,
        created_at=when + timedelta(hours=2), substantiated_at=when + timedelta(days=rng.randint(0, 6)),
    )
    session.add(exp)
    session.flush()
    if source == "upload" and exp.id % 9:              # every 9th upload has no receipt: feeds the "Missing" list
        exp.receipt_path = _seed_receipt(exp.id, vendor, when, item_name, amount)
    session.add(LineItem(
        expense_id=exp.id, name=item_name, quantity=1, unit_amount=amount, amount=amount, category=category,
        deductible_pct=pct, deductible_amount=round(amount * pct / 100, 2),
        form1120_line=rule["form1120_line"], rule_id=rule["id"],
    ))
    session.add(Decision(
        expense_id=exp.id, engine="rules", reimbursable=True, decision="approved", fit_score=0.9,
        reasons_json=json.dumps([
            f"{rule['label']} is a common expense for this industry and the stated purpose fits.",
            f"Deductible {int(pct)}% under {rule['citation']}.",
        ]),
        created_at=when + timedelta(hours=2),
    ))


def seed(session: Session, days: int = 90, rows: int = 150) -> None:
    rng = random.Random(42)
    now = utcnow().replace(minute=0, second=0, microsecond=0)
    session.add(Company(
        name="Acme Analytics Inc.", industry_id="software_saas",
        policy_json=json.dumps(rules.load_default_policy()),
        policy_summary="Default policy: client meals up to $250, office supplies up to $300 per purchase, "
                       "pre-approval above $1,000, receipts required at $75 or more, no personal expenses.",
    ))
    employees = [Employee(**e) for e in EMPLOYEES]
    session.add_all(employees)
    session.flush()
    for _ in range(rows):
        emp = rng.choice(employees)
        day = now - timedelta(days=rng.randint(2, days))
        while day.weekday() >= 5:                      # history is weekdays, business hours
            day -= timedelta(days=1)
        # 17:00-03:00 UTC = 9am-7pm Pacific, so seeded purchases fall in local business hours.
        when = day.replace(hour=0, minute=rng.choice((0, 10, 25, 40, 55))) + timedelta(hours=rng.randint(9, 19) + 8)
        _add_expense(session, rng, emp, when, now)
    session.commit()


def seed_if_empty() -> bool:
    with Session(db_engine) as session:
        if session.exec(select(Company)).first():
            return False
        seed(session)
        return True


if __name__ == "__main__":
    if "--reset" in sys.argv:
        SQLModel.metadata.drop_all(db_engine)
    init_db()
    print("seeded" if seed_if_empty() else "already seeded (use --reset to rebuild)")
