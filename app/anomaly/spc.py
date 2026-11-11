"""SPC control charts + anomaly rules per employee x category. TODO(step 2 rules, step 8 chart).

Rules: point > mean + 3 sigma; 2 of 3 consecutive > mean + 2 sigma; 6 increasing in a row;
split purchase (same employee/vendor/day, each under limit, sum above); duplicate (same
amount + vendor within 7 days, same capture id or receipt hash); weekend or 22:00-05:00 in
non-travel categories; round amounts above $200 with no itemization.
Min 8 points per employee x category, else company-wide category stats.
"""


def check(session, expense_id: int) -> list[dict]:
    """Returns [{rule, severity, explanation}]."""
    return []  # TODO(step 2)


def chart_series(session, employee_id: int, category: str) -> dict:
    """Returns {points: [{date, amount, expense_id}], mean, ucl2, ucl3} for Chart.js."""
    return {"points": [], "mean": 0.0, "ucl2": 0.0, "ucl3": 0.0}  # TODO(step 8)
