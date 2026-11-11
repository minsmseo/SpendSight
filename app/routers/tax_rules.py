"""Tax rules screen: reviewed defaults from the rule pack, adjustable by the company's CPA.

Every rate and keyword list starts from the shipped rule pack (with its citation). The CPA can
set a different rate or change the keywords that map purchases to a category; the reviewed default
stays visible and one click restores it. Because rates, keywords and citations are data, the same
tool can carry another jurisdiction's rule pack.
"""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.db import get_session
from app.models import CategoryOverride, Company, TaxRuleOverride, utcnow
from app.rules import engine
from app.web import page

router = APIRouter()


def rules_view(session: Session) -> dict:
    company = session.exec(select(Company)).first()
    pack = engine.load_rule_pack()
    base_categories = engine.base_taxonomy()["categories"]
    categories = engine.load_taxonomy()["categories"]
    industry = engine.load_industries().get(company.industry_id, {})
    rules = []
    for r in pack["rules"]:
        current = engine.get_tax_rule(r["id"], company.industry_id)
        default_pct = current.get("default_pct", current["deductible_pct"])      # after any industry exception
        rules.append({
            "id": r["id"], "label": r["label"], "category": r["category"],
            "default_pct": default_pct, "deductible_pct": current["deductible_pct"],
            "customized": bool(current.get("customized")), "override_note": current.get("override_note", ""),
            "industry_exception": bool(current.get("overridden_by_industry")),
            "citation": current["citation"], "form1120_line": current["form1120_line"],
            "confidence": r.get("confidence", ""), "notes": r.get("notes", ""),
            "cap": r.get("cap_per_recipient_per_year_usd"), "reimbursable": r.get("reimbursable", True),
        })
    cats = []
    for cid, c in categories.items():
        cats.append({
            "id": cid, "label": c["label"], "keywords": ", ".join(c["keywords"]),
            "default_keywords": ", ".join(base_categories[cid]["keywords"]),
            "default_rule": c["default_rule"], "base_rule": base_categories[cid]["default_rule"],
            "customized": bool(c.get("customized")),
        })
    return {
        "pack": {"id": pack["pack_id"], "jurisdiction": pack["jurisdiction"], "tax_year": pack["tax_year"], "form": pack["form"]},
        "company": company.name, "industry": industry.get("label", company.industry_id),
        "rules": rules, "categories": cats, "rule_ids": [r["id"] for r in pack["rules"]],
        "customized_count": sum(r["customized"] for r in rules) + sum(c["customized"] for c in cats),
    }


@router.get("/tax-rules")
def tax_rules_page(request: Request, session: Session = Depends(get_session)):
    return page(request, "tax_rules.html", view=rules_view(session))


@router.get("/api/tax-rules")
def api_tax_rules(session: Session = Depends(get_session)):
    return rules_view(session)


class RateBody(BaseModel):
    deductible_pct: float = Field(ge=0, le=100)
    note: str = ""


def _rule_or_404(rule_id: str) -> dict:
    rule = next((r for r in engine.load_rule_pack()["rules"] if r["id"] == rule_id), None)
    if not rule:
        raise HTTPException(404, "Unknown tax rule")
    return rule


@router.put("/api/tax-rules/{rule_id}")
def set_rate(rule_id: str, body: RateBody, session: Session = Depends(get_session)):
    """The CPA sets a deductible rate. Setting it back to the reviewed default removes the override."""
    _rule_or_404(rule_id)
    company = session.exec(select(Company)).first()
    existing = session.exec(select(TaxRuleOverride).where(TaxRuleOverride.rule_id == rule_id)).first()
    if existing:
        session.delete(existing)
        session.commit()
        engine.reload_overrides(session)
    default_pct = engine.get_tax_rule(rule_id, company.industry_id)["deductible_pct"]
    pct = round(body.deductible_pct, 2)
    if pct != default_pct:
        session.add(TaxRuleOverride(rule_id=rule_id, deductible_pct=pct, note=body.note.strip()[:300], updated_at=utcnow()))
        session.commit()
        engine.reload_overrides(session)
    return next(r for r in rules_view(session)["rules"] if r["id"] == rule_id)


@router.delete("/api/tax-rules/{rule_id}")
def reset_rate(rule_id: str, session: Session = Depends(get_session)):
    _rule_or_404(rule_id)
    for o in session.exec(select(TaxRuleOverride).where(TaxRuleOverride.rule_id == rule_id)).all():
        session.delete(o)
    session.commit()
    engine.reload_overrides(session)
    return next(r for r in rules_view(session)["rules"] if r["id"] == rule_id)


class CategoryBody(BaseModel):
    keywords: str = ""                     # comma separated
    default_rule: str = ""


@router.put("/api/tax-categories/{category}")
def set_category(category: str, body: CategoryBody, session: Session = Depends(get_session)):
    """Change the keywords that map a purchase to this category, and the tax rule the category uses."""
    base = engine.base_taxonomy()["categories"]
    if category not in base:
        raise HTTPException(404, "Unknown category")
    rule_ids = {r["id"] for r in engine.load_rule_pack()["rules"]}
    default_rule = body.default_rule or base[category]["default_rule"]
    if default_rule not in rule_ids:
        raise HTTPException(400, "Unknown tax rule")
    keywords, seen = [], set()
    for k in body.keywords.split(","):
        k = " ".join(k.lower().split())[:40]
        if k and k not in seen:
            seen.add(k)
            keywords.append(k)
    for o in session.exec(select(CategoryOverride).where(CategoryOverride.category == category)).all():
        session.delete(o)
    if keywords[:80] != [k.lower() for k in base[category]["keywords"]] or default_rule != base[category]["default_rule"]:
        session.add(CategoryOverride(category=category, keywords_json=json.dumps(keywords[:80]), default_rule=default_rule, updated_at=utcnow()))
    session.commit()
    engine.reload_overrides(session)
    return next(c for c in rules_view(session)["categories"] if c["id"] == category)


@router.delete("/api/tax-categories/{category}")
def reset_category(category: str, session: Session = Depends(get_session)):
    if category not in engine.base_taxonomy()["categories"]:
        raise HTTPException(404, "Unknown category")
    for o in session.exec(select(CategoryOverride).where(CategoryOverride.category == category)).all():
        session.delete(o)
    session.commit()
    engine.reload_overrides(session)
    return next(c for c in rules_view(session)["categories"] if c["id"] == category)
