"""Payouts batch + status (CLAUDE.md 4.5). TODO(step 7).

Plan: POST /api/payouts {expense_ids} -> one batch, one item per employee, sender_item_id =
expense ids -> PayoutBatch/PayoutItem; GET /api/payouts/{id} polls PayPal and marks expenses
`paid`. Accountable plan check: substantiated > 60 days after purchase -> flag (Reg. §1.62-2).
"""
from fastapi import APIRouter, Depends, Request
from sqlmodel import Session

from app.db import get_session
from app.routers.expenses import list_rows
from app.web import page

router = APIRouter()


@router.get("/reimburse")
def reimburse_page(request: Request, ids: str = "", session: Session = Depends(get_session)):
    """`ids` (comma separated) narrows the list to the rows selected in the Finance queue."""
    picked = [int(x) for x in ids.split(",") if x.strip().isdigit()] or None
    rows = list_rows(session, status="approved", ids=picked)
    by_employee: dict[str, dict] = {}
    for r in rows:
        g = by_employee.setdefault(r["employee"], {"employee": r["employee"], "count": 0, "total": 0.0})
        g["count"] += 1
        g["total"] = round(g["total"] + r["total"], 2)
    return page(
        request,
        "reimburse.html",
        groups=list(by_employee.values()),
        grand_total=round(sum(r["total"] for r in rows), 2),
    )
