"""SPC control charts + anomaly rules per employee x category (CLAUDE.md section 9).

Severity: high / medium flag the expense for Finance; low is shown as a note only.
"""
from datetime import timedelta
from statistics import mean, pstdev

from sqlmodel import select

from app.models import Expense, LineItem
from app.rules.engine import _money, load_taxonomy, to_local

MIN_POINTS = 8
HISTORY_STATUSES = ("approved", "paid")


def _history(session, category: str, exclude_id: int | None) -> list[tuple[Expense, float]]:
    """(expense, amount in this category) for reviewed expenses, oldest first."""
    rows = session.exec(
        select(Expense, LineItem)
        .where(LineItem.expense_id == Expense.id, LineItem.category == category)
        .where(Expense.status.in_(HISTORY_STATUSES))
    ).all()
    sums: dict[int, list] = {}
    for exp, li in rows:
        if exp.id == exclude_id or exp.purchased_at is None:
            continue
        sums.setdefault(exp.id, [exp, 0.0])[1] += li.amount
    return sorted(((e, round(a, 2)) for e, a in sums.values()), key=lambda x: x[0].purchased_at)


def _baseline(session, employee_id: int, category: str, exclude_id: int | None):
    """Employee history if it has enough points, else company-wide. Returns (points, scope) or (None, None)."""
    everyone = _history(session, category, exclude_id)
    own = [p for p in everyone if p[0].employee_id == employee_id]
    if len(own) >= MIN_POINTS:
        return own, "this employee"
    if len(everyone) >= MIN_POINTS:
        return everyone, "the company"
    return None, None


def check(session, expense: Expense, category: str, per_txn_limit: float = 0, n_items: int = 1) -> list[dict]:
    """Returns [{rule, severity, explanation}] for one expense."""
    out: list[dict] = []
    amount = expense.total or 0.0

    def add(rule, severity, explanation):
        out.append({"rule": rule, "severity": severity, "explanation": explanation})

    # Control chart rules.
    points, scope = _baseline(session, expense.employee_id, category, expense.id)
    if points:
        values = [a for _, a in points]
        mu, sigma = mean(values), pstdev(values)
        if sigma > 0:
            if amount > mu + 3 * sigma:
                add("spc_3sigma", "high", f"{_money(amount)} is more than 3 sigma above the usual {category} spend for {scope} (mean {_money(mu)}).")
            elif amount > mu + 2 * sigma and sum(v > mu + 2 * sigma for v in values[-2:]) >= 1:
                add("spc_2of3", "medium", f"Two of the last three {category} purchases are more than 2 sigma above the mean for {scope}.")
            run = values[-5:] + [amount]
            if len(run) == 6 and all(b > a for a, b in zip(run, run[1:])):
                add("spc_trend", "low", f"Six {category} purchases in a row with increasing amounts.")

    # Same employee, other live expenses.
    others = session.exec(
        select(Expense).where(Expense.employee_id == expense.employee_id, Expense.id != expense.id)
        .where(Expense.status.notin_(("rejected", "draft")))
    ).all()
    vendor = (expense.vendor or "").strip().lower()
    same_vendor = [o for o in others if (o.vendor or "").strip().lower() == vendor and o.purchased_at]

    if expense.purchased_at:
        day = to_local(expense.purchased_at).date()
        same_day = [o for o in same_vendor if to_local(o.purchased_at).date() == day]
        combined = amount + sum(o.total for o in same_day)
        if (per_txn_limit > 0 and same_day and amount <= per_txn_limit
                and all(o.total <= per_txn_limit for o in same_day) and combined > per_txn_limit):
            add("split_purchase", "high", f"{len(same_day) + 1} purchases at {expense.vendor} on the same day total {_money(combined)}, above the {_money(per_txn_limit)} limit: possible split purchase.")
        week = timedelta(days=7)
        if any(abs(o.total - amount) < 0.005 and abs(o.purchased_at - expense.purchased_at) <= week for o in same_vendor):
            add("duplicate", "medium", f"Another {_money(amount)} purchase at {expense.vendor} within 7 days: possible duplicate.")

    all_others = session.exec(select(Expense).where(Expense.id != expense.id)).all()
    if expense.paypal_capture_id and any(o.paypal_capture_id == expense.paypal_capture_id for o in all_others):
        add("duplicate_capture", "high", "The same PayPal capture was already submitted as another expense.")
    if expense.receipt_hash and any(o.receipt_hash == expense.receipt_hash for o in all_others):
        add("duplicate_receipt", "high", "The same receipt image was already submitted as another expense.")

    # Timing and round numbers.
    is_travel = load_taxonomy()["categories"].get(category, {}).get("is_travel", False)
    if expense.purchased_at and not is_travel:
        local = to_local(expense.purchased_at)
        if local.weekday() >= 5:
            add("weekend", "low", "Purchased on a weekend.")
        elif local.hour >= 22 or local.hour < 5:
            add("late_night", "low", "Purchased between 22:00 and 05:00.")
    if amount > 200 and amount % 50 == 0 and n_items <= 1 and expense.source != "paypal":
        add("round_number", "low", f"Round amount {_money(amount)} with no itemization.")
    return out


def chart_series(session, employee_id: int, category: str, exclude_id: int | None = None) -> dict:
    """Data for the Chart.js control chart on the expense detail page."""
    points, scope = _baseline(session, employee_id, category, exclude_id)
    if not points:
        return {"points": [], "mean": 0.0, "ucl2": 0.0, "ucl3": 0.0, "scope": None}
    values = [a for _, a in points]
    mu, sigma = mean(values), pstdev(values)
    return {
        "points": [{"date": e.purchased_at.date().isoformat(), "amount": a, "expense_id": e.id} for e, a in points],
        "mean": round(mu, 2), "ucl2": round(mu + 2 * sigma, 2), "ucl3": round(mu + 3 * sigma, 2), "scope": scope,
    }
