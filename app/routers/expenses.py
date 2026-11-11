"""Submit, list, detail, follow-up answers (CLAUDE.md 4.2-4.4).

Done: list API for AG Grid, queue page, detail page, employee home.
TODO(step 4): upload intake, follow-up answers, approve / reject / ask actions.
"""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from app.db import get_session
from app.models import Decision, Employee, Expense, FollowUp, LineItem
from app.web import current_role, page

router = APIRouter()


def expense_row(e: Expense, emp: Employee | None, items: list[LineItem], d: Decision | None) -> dict:
    return {
        "id": e.id,
        "status": e.status,
        "employee": emp.name if emp else "",
        "employee_id": e.employee_id,
        "vendor": e.vendor,
        "purchased_at": e.purchased_at.isoformat() if e.purchased_at else None,
        "total": e.total,
        "category": items[0].category if items else "",
        "deductible_amount": round(sum(i.deductible_amount for i in items), 2),
        "reimbursable": d.reimbursable if d else None,
        "engine": d.engine if d else None,
        "anomalies": len(json.loads(d.anomaly_json)) if d else 0,
        "question": d.question if d else None,
        "source": e.source,
        "purpose": e.purpose,
    }


def list_rows(session: Session, employee_id: int | None = None, status: str | None = None) -> list[dict]:
    q = select(Expense).order_by(Expense.purchased_at.desc())
    if employee_id:
        q = q.where(Expense.employee_id == employee_id)
    if status:
        q = q.where(Expense.status == status)
    expenses = session.exec(q).all()
    employees = {e.id: e for e in session.exec(select(Employee)).all()}
    items: dict[int, list[LineItem]] = {}
    for li in session.exec(select(LineItem)).all():
        items.setdefault(li.expense_id, []).append(li)
    latest: dict[int, Decision] = {}
    for d in session.exec(select(Decision).order_by(Decision.id)).all():
        latest[d.expense_id] = d
    return [expense_row(e, employees.get(e.employee_id), items.get(e.id, []), latest.get(e.id)) for e in expenses]


@router.get("/")
def home(request: Request):
    return RedirectResponse("/employee" if current_role(request) == "employee" else "/finance/queue")


@router.get("/employee")
def employee_home(request: Request, employee_id: int = 1, session: Session = Depends(get_session)):
    return page(
        request,
        "employee_home.html",
        employees=session.exec(select(Employee)).all(),
        employee_id=employee_id,
        rows=list_rows(session, employee_id=employee_id)[:25],
    )


@router.get("/finance/queue")
def finance_queue(request: Request):
    return page(request, "queue.html")


@router.get("/api/expenses")
def api_expenses(
    employee_id: int | None = None, status: str | None = None, session: Session = Depends(get_session)
):
    return list_rows(session, employee_id, status)


@router.post("/api/expenses/{expense_id}/review")
def api_review(expense_id: int, session: Session = Depends(get_session)):
    """(Re)run the review pipeline: LLM agent when available, rule engine otherwise."""
    from app.ai import agent

    if not session.get(Expense, expense_id):
        raise HTTPException(404, "Expense not found")
    return agent.review(session, expense_id)


@router.get("/expenses/{expense_id}")
def expense_detail(expense_id: int, request: Request, session: Session = Depends(get_session)):
    e = session.get(Expense, expense_id)
    if not e:
        raise HTTPException(404, "Expense not found")
    decision = session.exec(
        select(Decision).where(Decision.expense_id == expense_id).order_by(Decision.id.desc())
    ).first()
    return page(
        request,
        "expense_detail.html",
        e=e,
        employee=session.get(Employee, e.employee_id),
        items=session.exec(select(LineItem).where(LineItem.expense_id == expense_id)).all(),
        decision=decision,
        reasons=json.loads(decision.reasons_json) if decision else [],
        missing=json.loads(decision.missing_json) if decision else [],
        anomalies=json.loads(decision.anomaly_json) if decision else [],
        followups=session.exec(select(FollowUp).where(FollowUp.expense_id == expense_id)).all(),
    )
