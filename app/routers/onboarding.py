"""Company + industry + policy upload (CLAUDE.md 4.1)."""
import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from app.db import get_session
from app.models import Company, Employee
from app.rules import engine, policy_parser
from app.web import page

router = APIRouter()
MAX_POLICY_BYTES = 10 * 1024 * 1024


def get_company(session: Session) -> Company:
    return session.exec(select(Company)).first()


def _page(request: Request, session: Session, proposal: dict | None = None, error: str = ""):
    company = get_company(session)
    return page(
        request,
        "onboarding.html",
        company=company,
        industries=list(engine.load_industries().values()),
        policy=json.loads(company.policy_json or "{}") or engine.load_default_policy(),
        categories=list(engine.load_taxonomy()["categories"]),
        employees=session.exec(select(Employee)).all(),
        proposal=proposal,
        error=error,
    )


@router.get("/onboarding")
def onboarding_page(request: Request, session: Session = Depends(get_session)):
    return _page(request, session)


@router.post("/onboarding/company")
def save_company(
    name: str = Form(...),
    industry_id: str = Form(...),
    session: Session = Depends(get_session),
):
    company = get_company(session)
    if industry_id in engine.load_industries():
        company.industry_id = industry_id
    company.name = name.strip() or company.name
    session.add(company)
    session.commit()
    return RedirectResponse("/onboarding", status_code=303)


@router.post("/onboarding/policy")
async def upload_policy(request: Request, policy_file: UploadFile = File(...), session: Session = Depends(get_session)):
    """Read the document and show a proposed summary + rule table. Nothing is saved until Finance confirms."""
    data = await policy_file.read(MAX_POLICY_BYTES + 1)
    if not data or len(data) > MAX_POLICY_BYTES:
        return _page(request, session, error="Upload a policy document up to 10 MB.")
    try:
        text = policy_parser.extract_text(data, policy_file.filename or "")
    except ValueError as exc:
        return _page(request, session, error=str(exc))
    except Exception:
        return _page(request, session, error="Could not read that file. Try a text-based PDF, DOCX, MD or TXT.")
    if len(text.strip()) < 40:
        return _page(request, session, error="No readable text was found in that file (scanned PDFs are not supported).")
    proposal = policy_parser.analyze_policy(text)
    proposal["filename"] = (policy_file.filename or "")[:80]
    return _page(request, session, proposal=proposal)


@router.get("/onboarding/policy")
def policy_refresh():
    """Reloading the proposal page goes back to onboarding (the proposal itself is never stored)."""
    return RedirectResponse("/onboarding", status_code=303)


class PolicyBody(BaseModel):
    policy: dict
    summary: str = ""


@router.post("/api/onboarding/policy")
def confirm_policy(body: PolicyBody, session: Session = Depends(get_session)):
    """Finance confirms the (edited) table; it becomes the policy every review is checked against."""
    policy = policy_parser.sanitize_policy(body.policy)
    if not policy["rows"]:
        raise HTTPException(400, "The policy table is empty")
    company = get_company(session)
    company.policy_json = json.dumps(policy)
    if body.summary.strip():
        company.policy_summary = body.summary.strip()[:4000]
    session.add(company)
    session.commit()
    return {"ok": True, "rows": len(policy["rows"])}


@router.post("/api/onboarding/policy/default")
def load_default(session: Session = Depends(get_session)):
    company = get_company(session)
    company.policy_json = json.dumps(engine.load_default_policy())
    company.policy_summary = "Default policy loaded."
    session.add(company)
    session.commit()
    return {"ok": True}


@router.get("/api/industries")
def api_industries():
    return list(engine.load_industries().values())
