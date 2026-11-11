"""Tax report + exports (CLAUDE.md 4.6).

Done: summary by category and Form 1120 line.
TODO(step 8): period picker, missing substantiation list, cpa_package.xlsx + CSV export.
"""
from fastapi import APIRouter, Depends, Request
from sqlmodel import Session, select

from app.db import get_session
from app.models import Expense, LineItem
from app.web import page

router = APIRouter()
COUNTED = ("approved", "paid")


def tax_summary(session: Session) -> dict:
    expenses = {e.id: e for e in session.exec(select(Expense).where(Expense.status.in_(COUNTED))).all()}
    rows: dict[tuple, dict] = {}
    for li in session.exec(select(LineItem)).all():
        if li.expense_id not in expenses:
            continue
        r = rows.setdefault(
            (li.category, li.form1120_line),
            {"category": li.category, "form1120_line": li.form1120_line, "spent": 0.0, "deductible": 0.0},
        )
        r["spent"] += li.amount
        r["deductible"] += li.deductible_amount
    out = []
    for r in sorted(rows.values(), key=lambda x: -x["spent"]):
        r["spent"] = round(r["spent"], 2)
        r["deductible"] = round(r["deductible"], 2)
        r["nondeductible"] = round(r["spent"] - r["deductible"], 2)
        out.append(r)
    spent = round(sum(r["spent"] for r in out), 2)
    deductible = round(sum(r["deductible"] for r in out), 2)
    return {"rows": out, "spent": spent, "deductible": deductible, "nondeductible": round(spent - deductible, 2)}


@router.get("/reports/tax")
def tax_report(request: Request, session: Session = Depends(get_session)):
    return page(request, "report.html", summary=tax_summary(session))


@router.get("/api/reports/tax")
def api_tax_report(session: Session = Depends(get_session)):
    return tax_summary(session)
