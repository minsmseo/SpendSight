"""Company + industry + policy upload (CLAUDE.md 4.1). Policy upload/summarize is TODO(step 6)."""
import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from app.db import get_session
from app.models import Company, Employee
from app.rules import engine
from app.web import page

router = APIRouter()


def get_company(session: Session) -> Company:
    return session.exec(select(Company)).first()


@router.get("/onboarding")
def onboarding_page(request: Request, session: Session = Depends(get_session)):
    company = get_company(session)
    return page(
        request,
        "onboarding.html",
        company=company,
        industries=list(engine.load_industries().values()),
        policy=json.loads(company.policy_json or "{}"),
        employees=session.exec(select(Employee)).all(),
    )


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


@router.get("/api/industries")
def api_industries():
    return list(engine.load_industries().values())

# TODO(step 6): POST /onboarding/policy (upload PDF/DOCX/MD/TXT -> summary + proposed table),
#               POST /onboarding/policy/confirm (save edited grid to Company.policy_json).
