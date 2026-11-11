"""Reimbursement through PayPal Payouts (CLAUDE.md 4.5).

One batch per click, one payout item per employee (sum of their approved expenses). Status is
tracked by webhook (routers/webhooks.py) and by polling GET /v1/payments/payouts/{batch_id}, so
it also works locally where PayPal cannot reach a webhook URL.
"""
import json
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session, select

from app.db import get_session
from app.models import Employee, Expense, PayoutBatch, PayoutItem
from app.paypal import get_paypal_client
from app.paypal.client import PayPalError
from app.routers.expenses import list_rows
from app.rules import engine
from app.web import page

router = APIRouter()

# PayPal payout item `transaction_status` values.
ITEM_DONE = {"SUCCESS"}
ITEM_FAILED = {"FAILED", "RETURNED", "BLOCKED", "REFUNDED", "REVERSED", "DENIED", "CANCELED"}
BATCH_FINAL = {"SUCCESS", "DENIED", "CANCELED"}


def sender_item_id(item: PayoutItem) -> str:
    """The expense ids this item pays (PayPal allows 63 characters; longer lists use the item id)."""
    ids = ",".join(str(i) for i in json.loads(item.expense_ids_json))
    return ids if len(ids) <= 63 else f"PI-{item.id}"


def locked_expense_ids(session: Session) -> set[int]:
    """Expenses already in a payout that has not failed: never pay those twice."""
    locked: set[int] = set()
    for item in session.exec(select(PayoutItem)).all():
        if item.status not in ITEM_FAILED:
            locked.update(json.loads(item.expense_ids_json))
    return locked


def payable_rows(session: Session, ids: list[int] | None = None) -> list[dict]:
    locked = locked_expense_ids(session)
    plan = engine.load_rule_pack()["global_substantiation"]["accountable_plan"]
    expenses = {e.id: e for e in session.exec(select(Expense).where(Expense.status == "approved")).all()}
    rows = []
    for r in list_rows(session, status="approved", ids=ids):
        if r["id"] in locked:
            continue
        e = expenses[r["id"]]
        days = (e.substantiated_at - e.purchased_at).days if e.substantiated_at and e.purchased_at else 0
        r["late_days"] = days if days > plan["substantiate_within_days"] else 0   # may be taxable to the employee
        rows.append(r)
    return rows


def apply_item_status(session: Session, item: PayoutItem, status: str, paypal_item_id: str | None = None) -> None:
    """Record a payout item's status; a successful item marks its expenses paid."""
    status = (status or "").upper() or item.status
    if item.status in ITEM_DONE:
        return                                           # a late PENDING/UNCLAIMED event never undoes a success
    item.status = status
    if paypal_item_id:
        item.paypal_item_id = paypal_item_id
    session.add(item)
    if status in ITEM_DONE:
        for eid in json.loads(item.expense_ids_json):
            expense = session.get(Expense, eid)
            if expense and expense.status == "approved":
                expense.status = "paid"
                session.add(expense)


def find_item(session: Session, batch: PayoutBatch, paypal_item_id: str | None, sender_id: str | None) -> PayoutItem | None:
    items = session.exec(select(PayoutItem).where(PayoutItem.batch_id == batch.id)).all()
    for item in items:
        if (paypal_item_id and item.paypal_item_id == paypal_item_id) or (sender_id and sender_item_id(item) == sender_id):
            return item
    return None


def sync_batch(session: Session, batch: PayoutBatch, payload: dict) -> None:
    """Apply a GET /v1/payments/payouts/{id} response to the local batch and items."""
    header = payload.get("batch_header", {})
    batch.status = header.get("batch_status", batch.status)
    session.add(batch)
    for remote in payload.get("items", []):
        item = find_item(session, batch, remote.get("payout_item_id"), remote.get("payout_item", {}).get("sender_item_id"))
        if item:
            apply_item_status(session, item, remote.get("transaction_status"), remote.get("payout_item_id"))
    if batch.status in ("DENIED", "CANCELED"):
        for item in session.exec(select(PayoutItem).where(PayoutItem.batch_id == batch.id)).all():
            if item.status not in ITEM_DONE:
                apply_item_status(session, item, "FAILED")
    session.commit()


def batch_view(session: Session, batch: PayoutBatch) -> dict:
    employees = {e.id: e for e in session.exec(select(Employee)).all()}
    items = session.exec(select(PayoutItem).where(PayoutItem.batch_id == batch.id)).all()
    return {
        "id": batch.id, "paypal_batch_id": batch.paypal_batch_id, "status": batch.status, "total": batch.total,
        "created_at": batch.created_at.isoformat(),
        "done": batch.status in BATCH_FINAL and all(i.status in ITEM_DONE | ITEM_FAILED | {"UNCLAIMED"} for i in items),
        "items": [{
            "employee": employees[i.employee_id].name if i.employee_id in employees else "",
            "paypal_email": employees[i.employee_id].paypal_email if i.employee_id in employees else "",
            "amount": i.amount, "status": i.status, "paypal_item_id": i.paypal_item_id,
            "expense_ids": json.loads(i.expense_ids_json),
        } for i in items],
    }


class PayoutBody(BaseModel):
    expense_ids: list[int] | None = None                 # None = every approved, unpaid expense


@router.post("/api/payouts")
def create_payout(body: PayoutBody = PayoutBody(), session: Session = Depends(get_session)):
    rows = payable_rows(session, body.expense_ids)
    if not rows:
        raise HTTPException(409, "No approved, unpaid expenses to reimburse")
    employees = {e.id: e for e in session.exec(select(Employee)).all()}
    by_employee: dict[int, list[dict]] = {}
    for r in rows:
        by_employee.setdefault(r["employee_id"], []).append(r)
    missing = [employees[eid].name for eid in by_employee if not employees[eid].paypal_email]
    if missing:
        raise HTTPException(400, f"No PayPal email on file for: {', '.join(missing)}")

    batch = PayoutBatch(paypal_batch_id="", status="PENDING", total=round(sum(r["total"] for r in rows), 2))
    session.add(batch)
    session.flush()
    items = []
    for employee_id, group in by_employee.items():
        item = PayoutItem(batch_id=batch.id, employee_id=employee_id, amount=round(sum(r["total"] for r in group), 2),
                          status="PENDING", expense_ids_json=json.dumps(sorted(r["id"] for r in group)))
        session.add(item)
        items.append(item)
    session.flush()
    try:
        created = get_paypal_client().create_payout(
            sender_batch_id=f"SS-{batch.id}-{secrets.token_hex(4)}",
            items=[{"receiver_email": employees[i.employee_id].paypal_email, "amount": i.amount,
                    "sender_item_id": sender_item_id(i),
                    "note": f"Expense reimbursement ({len(json.loads(i.expense_ids_json))} expenses)"} for i in items],
        )
    except PayPalError as exc:
        session.rollback()                               # nothing was sent: leave the expenses payable
        name = exc.body.get("name") if isinstance(exc.body, dict) else None
        raise HTTPException(502, f"PayPal error {exc.status}" + (f": {name}" if name else ""))
    header = created.get("batch_header", {})
    batch.paypal_batch_id = header.get("payout_batch_id", "")
    batch.status = header.get("batch_status", "PENDING")
    session.add(batch)
    session.commit()
    return batch_view(session, batch)


@router.get("/api/payouts/{batch_id}")
def payout_status(batch_id: int, session: Session = Depends(get_session)):
    """Poll PayPal for the batch (backup for webhooks) and return the current state."""
    batch = session.get(PayoutBatch, batch_id)
    if not batch:
        raise HTTPException(404, "Payout batch not found")
    if batch.paypal_batch_id and not batch_view(session, batch)["done"]:
        try:
            sync_batch(session, batch, get_paypal_client().get_payout(batch.paypal_batch_id))
        except PayPalError:
            pass                                         # keep the last known state; the next poll retries
    return batch_view(session, batch)


@router.get("/api/payouts")
def payout_list(session: Session = Depends(get_session)):
    batches = session.exec(select(PayoutBatch).order_by(PayoutBatch.id.desc()).limit(10)).all()
    return [batch_view(session, b) for b in batches]


@router.get("/reimburse")
def reimburse_page(request: Request, ids: str = "", session: Session = Depends(get_session)):
    """`ids` (comma separated) narrows the list to the rows selected in the Finance queue."""
    picked = [int(x) for x in ids.split(",") if x.strip().isdigit()] or None
    rows = payable_rows(session, picked)
    employees = {e.id: e for e in session.exec(select(Employee)).all()}
    groups: dict[int, dict] = {}
    for r in rows:
        g = groups.setdefault(r["employee_id"], {
            "employee": r["employee"], "paypal_email": employees[r["employee_id"]].paypal_email, "total": 0.0, "rows": []})
        g["rows"].append(r)
        g["total"] = round(g["total"] + r["total"], 2)
    return page(
        request,
        "reimburse.html",
        groups=list(groups.values()),
        expense_ids=[r["id"] for r in rows],
        selected=bool(picked),
        grand_total=round(sum(r["total"] for r in rows), 2),
        batches=[batch_view(session, b) for b in session.exec(select(PayoutBatch).order_by(PayoutBatch.id.desc()).limit(5)).all()],
    )
