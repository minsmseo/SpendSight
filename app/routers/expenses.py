"""Submit, list, detail, follow-up answers and Finance actions (CLAUDE.md 4.2-4.4).

Automated decisions carry engine `llm` or `rules`. A Finance override is stored as its own
Decision row with engine `finance`, so the automated reasoning stays visible next to it.
"""
import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from app import config
from app.ai import agent
from app.anomaly import spc
from app.db import get_session
from app.extract import vision
from app.models import Decision, Employee, Expense, FollowUp, LineItem, utcnow
from app.rules import engine
from app.web import current_role, page

router = APIRouter()

UPLOAD_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "application/pdf": ".pdf"}
MAX_UPLOAD_BYTES = 8 * 1024 * 1024


# --- listing ----------------------------------------------------------------
def primary_category(items: list[LineItem]) -> str:
    totals: dict[str, float] = {}
    for i in items:
        totals[i.category] = totals.get(i.category, 0.0) + i.amount
    return max(totals, key=totals.get) if totals else ""


def list_rows(session: Session, employee_id: int | None = None, status: str | None = None,
              ids: list[int] | None = None) -> list[dict]:
    q = select(Expense).order_by(Expense.purchased_at.desc())
    if employee_id:
        q = q.where(Expense.employee_id == employee_id)
    if status:
        q = q.where(Expense.status == status)
    else:
        q = q.where(Expense.status != "draft")       # unpaid carts never reach the queue
    if ids is not None:
        q = q.where(Expense.id.in_(ids))
    expenses = session.exec(q).all()
    employees = {e.id: e for e in session.exec(select(Employee)).all()}
    items: dict[int, list[LineItem]] = {}
    for li in session.exec(select(LineItem)).all():
        items.setdefault(li.expense_id, []).append(li)
    auto: dict[int, Decision] = {}
    latest: dict[int, Decision] = {}
    for d in session.exec(select(Decision).order_by(Decision.id)).all():
        latest[d.expense_id] = d
        if d.engine != "finance":
            auto[d.expense_id] = d
    open_q = {f.expense_id: f.question for f in session.exec(select(FollowUp).where(FollowUp.answer == None)).all()}  # noqa: E711
    rows = []
    for e in expenses:
        emp, lines, d, last = employees.get(e.employee_id), items.get(e.id, []), auto.get(e.id), latest.get(e.id)
        rows.append({
            "id": e.id,
            "status": e.status,
            "employee": emp.name if emp else "",
            "employee_id": e.employee_id,
            "vendor": e.vendor,
            "purchased_at": e.purchased_at.isoformat() if e.purchased_at else None,
            "total": e.total,
            "category": primary_category(lines),
            "deductible_amount": round(sum(i.deductible_amount for i in lines), 2),
            "reimbursable": last.reimbursable if last else None,
            "engine": d.engine if d else None,
            "finance": last.decision if last and last.engine == "finance" else None,
            "anomalies": len(json.loads(d.anomaly_json)) if d else 0,
            "question": open_q.get(e.id) if e.status == "needs_info" else None,
            "source": e.source,
            "purpose": e.purpose,
        })
    return rows


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
    return page(request, "queue.html", categories=list(engine.load_taxonomy()["categories"]))


@router.get("/api/expenses")
def api_expenses(
    employee_id: int | None = None, status: str | None = None, session: Session = Depends(get_session)
):
    return list_rows(session, employee_id, status)


# --- channel B: upload a receipt ----------------------------------------------
@router.post("/expenses/upload")
async def upload_receipt(
    request: Request, employee_id: int = Form(1), receipt: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    """Step 1: store the file, extract what we can, show the prefilled form."""
    ext = UPLOAD_TYPES.get(receipt.content_type or "")
    data = await receipt.read(MAX_UPLOAD_BYTES + 1)
    if not ext or not data or len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "Upload a PNG, JPEG, WebP or PDF receipt up to 8 MB")
    digest = hashlib.sha256(data).hexdigest()
    config.UPLOAD_DIR.mkdir(exist_ok=True)
    name = digest[:32] + ext                     # content-addressed, never the client file name
    (config.UPLOAD_DIR / name).write_bytes(data)
    extracted = vision.extract_receipt(data, receipt.content_type)
    return page(
        request,
        "upload_form.html",
        employee=session.get(Employee, employee_id),
        receipt_name=name,
        receipt_hash=digest,
        is_pdf=ext == ".pdf",
        fields=extracted.get("fields", {}),
        confidence=extracted.get("confidence", {}),
        extract_engine=extracted.get("engine"),
        today=engine.to_local(utcnow()).date().isoformat(),
    )


def _parse_items(text: str) -> list[tuple[str, float]]:
    """One item per line: `name, 12.34` (also accepts `name $12.34`)."""
    out = []
    for line in text.splitlines():
        name, _, amount = line.strip().rpartition(",") if "," in line else line.strip().rpartition(" ")
        try:
            value = float(amount.strip().lstrip("$").replace(",", ""))
        except ValueError:
            continue
        if name.strip() and value > 0:
            out.append((name.strip(), round(value, 2)))
    return out


@router.post("/expenses/submit")
def submit_expense(
    employee_id: int = Form(...), vendor: str = Form(...), purchased_on: str = Form(...),
    total: float = Form(...), tax_amount: float = Form(0.0), items: str = Form(""),
    purpose: str = Form(""), attendees: str = Form(""), receipt_name: str = Form(""),
    receipt_hash: str = Form(""), session: Session = Depends(get_session),
):
    """Step 2: create the expense from the confirmed form and run the review."""
    if not session.get(Employee, employee_id):
        raise HTTPException(400, "Unknown employee")
    if total <= 0 or not vendor.strip():
        raise HTTPException(400, "Vendor and a positive total are required")
    try:
        local = datetime.strptime(purchased_on, "%Y-%m-%d").replace(hour=12, tzinfo=ZoneInfo(config.COMPANY_TZ))
    except ValueError:
        raise HTTPException(400, "Date must be YYYY-MM-DD")
    if receipt_name and not (config.UPLOAD_DIR / receipt_name).name == receipt_name:
        raise HTTPException(400, "Bad receipt reference")
    has_file = bool(receipt_name) and (config.UPLOAD_DIR / receipt_name).is_file()
    expense = Expense(
        employee_id=employee_id, source="upload", status="draft", vendor=vendor.strip(),
        purchased_at=local, total=round(total, 2), tax_amount=round(tax_amount, 2),
        purpose=purpose.strip(), attendees=attendees.strip(),
        receipt_path=receipt_name if has_file else None, receipt_hash=receipt_hash if has_file else None,
    )
    session.add(expense)
    session.flush()
    lines = _parse_items(items) or [(vendor.strip() + " purchase", round(total - tax_amount, 2))]
    for name, amount in lines:
        session.add(LineItem(expense_id=expense.id, name=name, unit_amount=amount, amount=amount))
    session.commit()
    agent.review(session, expense.id)
    return RedirectResponse(f"/expenses/{expense.id}", status_code=303)


# --- review loop ------------------------------------------------------------
class AnswerBody(BaseModel):
    answer: str


class NoteBody(BaseModel):
    note: str = ""


class AskBody(BaseModel):
    question: str


class CategoryBody(BaseModel):
    category: str


def _expense(session: Session, expense_id: int) -> Expense:
    expense = session.get(Expense, expense_id)
    if not expense:
        raise HTTPException(404, "Expense not found")
    return expense


def _latest(session: Session, expense_id: int, automated: bool) -> Decision | None:
    q = select(Decision).where(Decision.expense_id == expense_id).order_by(Decision.id.desc())
    q = q.where(Decision.engine != "finance") if automated else q.where(Decision.engine == "finance")
    return session.exec(q).first()


def _finance_decision(session: Session, expense: Expense, decision: str, note: str, question: str | None = None):
    prev = _latest(session, expense.id, automated=True)
    session.add(Decision(
        expense_id=expense.id, engine="finance", decision=decision, reimbursable=decision == "approved",
        fit_score=prev.fit_score if prev else 0.0, reasons_json=json.dumps([note] if note else []),
        missing_json=prev.missing_json if prev else "[]", anomaly_json=prev.anomaly_json if prev else "[]",
        question=question,
    ))
    session.commit()


def _summary(expense: Expense, result: dict | None = None) -> dict:
    out = {"expense_id": expense.id, "status": expense.status}
    if result:
        out.update(decision=result["decision"], engine=result["engine"], question=result["question"])
    return out


@router.post("/api/expenses/{expense_id}/review")
def api_review(expense_id: int, session: Session = Depends(get_session)):
    """(Re)run the review pipeline: LLM agent when available, rule engine otherwise."""
    _expense(session, expense_id)
    return agent.review(session, expense_id)


@router.post("/api/expenses/{expense_id}/answer")
def answer_followup(expense_id: int, body: AnswerBody, session: Session = Depends(get_session)):
    """Employee answers the open question; the pipeline reruns with the answer."""
    expense = _expense(session, expense_id)
    followup = session.exec(
        select(FollowUp).where(FollowUp.expense_id == expense_id, FollowUp.answer == None)  # noqa: E711
    ).first()
    if not followup:
        raise HTTPException(409, "No open question on this expense")
    if not body.answer.strip():
        raise HTTPException(400, "Answer is required")
    followup.answer = body.answer.strip()
    session.add(followup)
    templates = engine.load_taxonomy()["question_templates"]
    if not expense.attendees and followup.question in {templates[k] for k in engine.ATTENDEE_QUESTIONS}:
        expense.attendees = followup.answer       # the answer is the attendee list: keep it on the record
        session.add(expense)
    session.commit()
    return _summary(expense, agent.review(session, expense_id))


@router.post("/api/expenses/{expense_id}/approve")
def approve(expense_id: int, body: NoteBody = NoteBody(), session: Session = Depends(get_session)):
    """Finance approves. A held PayPal authorization is captured as part of the approval."""
    from app.routers.paypal_checkout import capture_authorization

    expense = _expense(session, expense_id)
    if expense.status == "held":
        capture_authorization(expense_id, session)
    elif expense.status in ("flagged", "needs_info", "rejected"):
        expense.status = "approved"
        expense.substantiated_at = expense.substantiated_at or utcnow()
        session.add(expense)
    else:
        raise HTTPException(409, f"Cannot approve an expense that is {expense.status}")
    _finance_decision(session, expense, "approved", body.note.strip() or "Approved by Finance.")
    return _summary(expense)


@router.post("/api/expenses/{expense_id}/reject")
def reject(expense_id: int, body: NoteBody = NoteBody(), session: Session = Depends(get_session)):
    """Finance rejects. A held PayPal authorization is voided, so no money moves."""
    from app.routers.paypal_checkout import void_authorization

    expense = _expense(session, expense_id)
    if expense.status == "held":
        void_authorization(expense_id, session)
    elif expense.status in ("flagged", "needs_info", "approved"):
        expense.status = "rejected"
        session.add(expense)
    else:
        raise HTTPException(409, f"Cannot reject an expense that is {expense.status}")
    _finance_decision(session, expense, "rejected", body.note.strip() or "Rejected by Finance.")
    return _summary(expense)


@router.post("/api/expenses/{expense_id}/ask")
def ask(expense_id: int, body: AskBody, session: Session = Depends(get_session)):
    """Finance sends its own question to the employee."""
    expense = _expense(session, expense_id)
    question = body.question.strip()
    if not question:
        raise HTTPException(400, "Question is required")
    if expense.status not in ("flagged", "needs_info", "approved", "rejected"):
        raise HTTPException(409, f"Cannot ask about an expense that is {expense.status}")
    for f in session.exec(select(FollowUp).where(FollowUp.expense_id == expense_id, FollowUp.answer == None)).all():  # noqa: E711
        session.delete(f)                         # Finance's question replaces an unanswered one
    session.add(FollowUp(expense_id=expense_id, question=question))
    expense.status = "needs_info"
    session.add(expense)
    _finance_decision(session, expense, "needs_info", "Finance asked the employee a question.", question)
    return _summary(expense)


@router.patch("/api/expenses/{expense_id}/category")
def set_category(expense_id: int, body: CategoryBody, session: Session = Depends(get_session)):
    """Finance corrects the category in the grid; tax treatment and decision are recomputed by rules."""
    expense = _expense(session, expense_id)
    if body.category not in engine.load_taxonomy()["categories"]:
        raise HTTPException(400, "Unknown category")
    if expense.status in ("paid", "draft"):
        raise HTTPException(409, f"Cannot edit an expense that is {expense.status}")
    for li in session.exec(select(LineItem).where(LineItem.expense_id == expense_id)).all():
        li.category = body.category
        session.add(li)
    session.commit()
    return _summary(expense, engine.review_expense(session, expense_id))


@router.get("/api/expenses/{expense_id}/chart")
def chart(expense_id: int, session: Session = Depends(get_session)):
    """Control chart data: history for this employee x category plus the current point."""
    expense = _expense(session, expense_id)
    items = session.exec(select(LineItem).where(LineItem.expense_id == expense_id)).all()
    category = primary_category(items)
    series = spc.chart_series(session, expense.employee_id, category, exclude_id=expense_id)
    series["category"] = category
    series["current"] = {
        "date": expense.purchased_at.date().isoformat() if expense.purchased_at else None,
        "amount": round(sum(i.amount for i in items if i.category == category), 2),
    }
    return series


@router.get("/expenses/{expense_id}")
def expense_detail(expense_id: int, request: Request, session: Session = Depends(get_session)):
    e = _expense(session, expense_id)
    decision = _latest(session, expense_id, automated=True)
    finance = _latest(session, expense_id, automated=False)
    return page(
        request,
        "expense_detail.html",
        e=e,
        employee=session.get(Employee, e.employee_id),
        items=session.exec(select(LineItem).where(LineItem.expense_id == expense_id)).all(),
        decision=decision,
        finance=finance,
        finance_note=" ".join(json.loads(finance.reasons_json)) if finance else "",
        reasons=json.loads(decision.reasons_json) if decision else [],
        missing=json.loads(decision.missing_json) if decision else [],
        anomalies=json.loads(decision.anomaly_json) if decision else [],
        followups=session.exec(select(FollowUp).where(FollowUp.expense_id == expense_id).order_by(FollowUp.id)).all(),
        local_time=engine.to_local(e.purchased_at) if e.purchased_at else None,
    )
