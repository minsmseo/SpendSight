"""Channel C: import PayPal account history through Transaction Search (CLAUDE.md 4.2 C).

Backfill only. Sandbox data can lag by three hours or more, so the live flow never depends on it.
Imported rows have no business purpose, so the review puts them in `needs_info` and the employee
is asked for it.
"""
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session, select

from app import config
from app.db import get_session
from app.extract.paypal_receipt import receipt_from_transaction
from app.models import Employee, Expense, LineItem, utcnow
from app.paypal import get_paypal_client
from app.paypal.client import PayPalError
from app.rules import engine
from app.web import page

router = APIRouter()
MAX_RANGE_DAYS = 31                 # Transaction Search limit per call
MAX_PAGES = 10


class ImportBody(BaseModel):
    start_date: str                 # YYYY-MM-DD
    end_date: str
    employee_id: int
    include_incoming: bool = False  # also import payments received (sandbox: demo vendors pay into the same account)


def _parse_range(start: str, end: str) -> tuple[date, date]:
    try:
        s, e = datetime.strptime(start, "%Y-%m-%d").date(), datetime.strptime(end, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "Dates must be YYYY-MM-DD")
    if e < s:
        raise HTTPException(400, "End date is before start date")
    if (e - s).days > MAX_RANGE_DAYS - 1:
        raise HTTPException(400, f"PayPal allows at most {MAX_RANGE_DAYS} days per import")
    return s, e


@router.get("/finance/import")
def import_page(request: Request, session: Session = Depends(get_session)):
    today = utcnow().date()                     # the range is sent to PayPal in UTC
    return page(
        request,
        "import.html",
        employees=session.exec(select(Employee)).all(),
        start=(today - timedelta(days=MAX_RANGE_DAYS - 1)).isoformat(),
        end=today.isoformat(),
        max_days=MAX_RANGE_DAYS,
        mock=config.PAYPAL_MOCK,
    )


@router.post("/api/paypal/import")
def import_transactions(body: ImportBody, session: Session = Depends(get_session)):
    start, end = _parse_range(body.start_date, body.end_date)
    if not session.get(Employee, body.employee_id):
        raise HTTPException(400, "Unknown employee")
    client = get_paypal_client()
    details, refreshed = [], None
    try:
        for page_no in range(1, MAX_PAGES + 1):
            res = client.search_transactions(f"{start.isoformat()}T00:00:00Z", f"{end.isoformat()}T23:59:59Z", page=page_no)
            details += res.get("transaction_details") or []
            refreshed = res.get("last_refreshed_datetime") or refreshed
            if page_no >= int(res.get("total_pages") or 1):
                break
    except PayPalError as exc:
        name = exc.body.get("name") if isinstance(exc.body, dict) else None
        hint = " Enable Transaction Search on the PayPal app (it can take a few hours to activate)." if exc.status in (401, 403) else ""
        raise HTTPException(502, f"PayPal error {exc.status}" + (f": {name}." if name else ".") + hint)

    known = {e.paypal_capture_id for e in session.exec(select(Expense).where(Expense.paypal_capture_id != None)).all()}  # noqa: E711
    skipped = {"already_recorded": 0, "payouts": 0, "not_completed": 0, "incoming": 0, "other": 0}
    imported: list[int] = []
    for detail in details:
        r = receipt_from_transaction(detail)
        if not r["transaction_id"] or r["kind"] == "other" or r["currency"] != "USD" or r["total"] <= 0:
            skipped["other"] += 1
        elif r["kind"] == "payout":
            skipped["payouts"] += 1                     # our own reimbursements are not expenses
        elif not r["completed"]:
            skipped["not_completed"] += 1
        elif r["transaction_id"] in known:
            skipped["already_recorded"] += 1            # bought through the Vendor Store, or imported before
        elif not r["outgoing"] and not body.include_incoming:
            skipped["incoming"] += 1
        else:
            expense = Expense(
                employee_id=body.employee_id, source="import", status="draft", vendor=r["vendor"],
                purchased_at=r["purchased_at"] or utcnow(), total=r["total"], purpose="",
                paypal_capture_id=r["transaction_id"],
            )
            session.add(expense)
            session.flush()
            for it in r["items"]:
                session.add(LineItem(expense_id=expense.id, name=it["name"], quantity=it["quantity"],
                                     unit_amount=it["unit_amount"], amount=it["amount"]))
            session.commit()
            known.add(r["transaction_id"])
            # No purpose yet, so there is nothing for the LLM to judge: the rules ask for it.
            engine.review_expense(session, expense.id)
            imported.append(expense.id)
    return {"found": len(details), "imported": len(imported), "expense_ids": imported, "skipped": skipped,
            "last_refreshed": refreshed, "mode": config.PAYPAL_MODE_LABEL}
