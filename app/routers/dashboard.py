"""Finance dashboard: where every submitted dollar ends up, what needs action, and what to
confirm with the CPA before the books are closed."""
from fastapi import APIRouter, Depends, Request
from sqlmodel import Session, select

from app.db import get_session
from app.models import Company, Decision, Expense, PayoutItem
from app.routers.reimburse import payable_rows
from app.routers.reports import MONTHS, build_report, period_range
from app.rules import engine
from app.web import page, usd

router = APIRouter()
UNDER_REVIEW = ("needs_info", "flagged", "held")


def cpa_review_items(session: Session, report: dict, company: Company) -> list[dict]:
    """Accepted expenses whose tax treatment a CPA should confirm, each with the reason why.

    Works the same with and without an AI key: the reasons come from the tax rule pack (confidence,
    caps, industry overrides) and from what the review recorded (engine, conflicts, Finance overrides).
    """
    plan = engine.load_rule_pack()["global_substantiation"]["accountable_plan"]
    notes: dict[int, list[str]] = {}
    at_stake: dict[int, float] = {}

    def add(expense_id: int, text: str, amount: float = 0.0) -> None:
        if text not in notes.setdefault(expense_id, []):
            notes[expense_id].append(text)
        at_stake[expense_id] = at_stake.get(expense_id, 0.0) + amount

    for ln in report["lines"]:
        rule = engine.get_tax_rule(ln["rule_id"], company.industry_id)
        eid, amount, deductible = ln["expense_id"], ln["amount"], ln["deductible_amount"]
        if rule.get("customized") and ln["deductible_pct"] == rule["deductible_pct"]:
            add(eid, f"Deducted at {rule['deductible_pct']:g}% under a rate set by your CPA (reviewed default {rule['default_pct']:g}%, {rule['citation']}).", amount)
        if rule.get("confidence") == "low":
            add(eid, "No specific tax rule matched, so it is treated as a general business expense. Confirm the category and Form 1120 line.", amount)
        if rule.get("overridden_by_industry"):
            add(eid, f"An industry exception was applied ({rule['citation']}). Confirm it covers this purchase.", amount)
        if ln["rule_id"] == "gift" and deductible < amount:
            add(eid, f"Only {usd(deductible)} of {usd(amount)} is deductible under the $25 per recipient limit. Confirm the recipients and earlier gifts to them this year.", amount - deductible)
        if ln["rule_id"] == "office_supplies" and amount > rule.get("de_minimis_per_item_usd", float("inf")):
            add(eid, "Above the $2,500 de minimis safe harbor. Decide whether to capitalize it (bonus depreciation or §179 may apply).", amount)
        if ln["rule_id"] == "entertainment":
            add(eid, "Entertainment is 0% deductible. If food or drink was invoiced separately, that part may be 50% deductible.", amount)
        if ln["rule_id"] == "meal_convenience" and deductible == 0:
            add(eid, "0% deductible from 2026 under §274(o). Confirm that no exception applies, for example an event open to all employees.", amount)
        if ln["rule_id"] == "event_all_staff":
            add(eid, "Deducted at 100% as an all-employee event. Confirm it was open to all staff and not only to selected people.", amount)

    for d in report["decisions"]:
        eid = d["expense_id"]
        if any("rule pack sets" in r or "verify with your CPA" in r for r in d["reasons"]):
            add(eid, "The AI suggested a different deductible percentage than the tax rule pack. The rule pack value was used.")
        if d["finance"].startswith("approved") and d["decision"] in ("flagged", "rejected"):
            add(eid, "Approved by Finance after the review flagged it. Confirm the tax treatment still holds.")

    expenses = {e.id: e for e in session.exec(select(Expense).where(Expense.id.in_(list(notes) or [0]))).all()}
    for e in session.exec(select(Expense).where(Expense.status.in_(("approved", "paid")))).all():
        if e.substantiated_at and e.purchased_at and (e.substantiated_at - e.purchased_at).days > plan["substantiate_within_days"]:
            if any(d["expense_id"] == e.id for d in report["decisions"]):
                add(e.id, f"Substantiated more than {plan['substantiate_within_days']} days after purchase. The reimbursement may be taxable to the employee ({plan['citation']}).", e.total)
                expenses[e.id] = e

    by_id = {d["expense_id"]: d for d in report["decisions"]}
    items = []
    for eid, texts in notes.items():
        d = by_id.get(eid)
        if not d:
            continue
        items.append({"expense_id": eid, "date": d["date"], "employee": d["employee"], "vendor": d["vendor"],
                      "total": d["total"], "engine": d["engine"], "notes": texts, "at_stake": round(at_stake.get(eid, 0.0), 2)})
    return sorted(items, key=lambda i: (-i["at_stake"], -i["total"]))


def build_dashboard(session: Session, period: str = "ytd") -> dict:
    company = session.exec(select(Company)).first()
    report = build_report(session, period)
    start, end, label, value = period_range(period, company.tax_year)

    in_period = []
    for e in session.exec(select(Expense).where(Expense.status != "draft")).all():
        if e.purchased_at and start <= engine.to_local(e.purchased_at).date() < end:
            in_period.append(e)
    under_review = [e for e in in_period if e.status in UNDER_REVIEW]
    rejected = [e for e in in_period if e.status == "rejected"]
    review_total = round(sum(e.total for e in under_review), 2)
    rejected_total = round(sum(e.total for e in rejected), 2)
    submitted = round(report["spent"] + review_total + rejected_total, 2)
    split = [
        {"key": "deductible", "label": "Deductible", "amount": report["deductible"], "note": "Reimbursed and deductible on Form 1120"},
        {"key": "nondeductible", "label": "Nondeductible", "amount": report["nondeductible"], "note": "Reimbursed, but not deductible"},
        {"key": "review", "label": "Under review", "amount": review_total, "note": "Waiting for an answer or a Finance decision"},
        {"key": "rejected", "label": "Not reimbursable", "amount": rejected_total, "note": "Personal or banned by policy"},
    ]
    for s in split:
        s["pct"] = round(s["amount"] / submitted * 100, 1) if submitted else 0.0

    # Monthly trend from accepted lines.
    monthly: dict[str, dict] = {}
    for ln in report["lines"]:
        m = monthly.setdefault(ln["date"][:7], {"month": ln["date"][:7], "label": MONTHS[int(ln["date"][5:7]) - 1][:3], "deductible": 0.0, "nondeductible": 0.0})
        m["deductible"] += ln["deductible_amount"]
        m["nondeductible"] += ln["nondeductible_amount"]
    months = [{**m, "deductible": round(m["deductible"], 2), "nondeductible": round(m["nondeductible"], 2)} for _, m in sorted(monthly.items())]

    by_status = {s: {"count": 0, "amount": 0.0} for s in ("needs_info", "flagged", "held")}
    for e in under_review:
        by_status[e.status]["count"] += 1
        by_status[e.status]["amount"] = round(by_status[e.status]["amount"] + e.total, 2)
    payable = payable_rows(session)
    sources = {"paypal": 0, "upload": 0, "import": 0}
    for e in in_period:
        sources[e.source] = sources.get(e.source, 0) + 1
    engines = {"llm": 0, "rules": 0}
    ids = {e.id for e in in_period}
    latest: dict[int, str] = {}
    for d in session.exec(select(Decision).where(Decision.engine != "finance").order_by(Decision.id)).all():
        if d.expense_id in ids:
            latest[d.expense_id] = d.engine
    for eng in latest.values():
        engines[eng] = engines.get(eng, 0) + 1
    paid_out = round(sum(i.amount for i in session.exec(select(PayoutItem).where(PayoutItem.status == "SUCCESS")).all()), 2)

    return {
        "company": report["company"], "industry": report["industry"], "tax_year": report["tax_year"],
        "period": value, "period_label": label, "options": report["options"],
        "submitted": submitted, "submitted_count": len(in_period),
        "deductible": report["deductible"], "nondeductible": report["nondeductible"],
        "deductible_pct": round(report["deductible"] / submitted * 100, 1) if submitted else 0.0,
        "split": split, "months": months, "by_category": report["by_category"],
        "queue": by_status, "review_count": len(under_review), "review_total": review_total,
        "payable_count": len(payable), "payable_total": round(sum(r["total"] for r in payable), 2),
        "missing_count": report["missing_count"], "paid_out": paid_out,
        "sources": sources, "engines": engines,
        "cpa_items": cpa_review_items(session, report, company),
    }


@router.get("/dashboard")
def dashboard_page(request: Request, period: str = "ytd", session: Session = Depends(get_session)):
    return page(request, "dashboard.html", d=build_dashboard(session, period))


@router.get("/api/dashboard")
def api_dashboard(period: str = "ytd", session: Session = Depends(get_session)):
    return build_dashboard(session, period)
