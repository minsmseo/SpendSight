"""Deterministic decision pipeline: the no-AI fallback and the guardrail for the LLM.

categorize -> purpose intent + fit score -> policy check -> tax treatment -> anomaly -> decision.
No tax logic is hardcoded here; percentages, citations and Form 1120 lines come from the JSON
packs so another jurisdiction (`kr_2026.json`) can be added later.
"""
import json
import re
from datetime import datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from rapidfuzz import fuzz, process

from app import config

W_INDUSTRY, W_INTENT, W_POLICY = 0.4, 0.4, 0.2
FIT_FLAG_BELOW, FIT_ASK_BELOW = 0.4, 0.7
ATTENDEE_QUESTIONS = ("meal_attendees", "gift_recipient", "entertainment_attendees")


# --- data packs -------------------------------------------------------------
def _load(name: str) -> dict:
    return json.loads((config.RULES_DIR / name).read_text(encoding="utf-8"))


@lru_cache
def load_rule_pack(pack: str = "us_2026") -> dict:
    return _load(f"{pack}.json")


@lru_cache
def load_industries() -> dict[str, dict]:
    return {i["id"]: i for i in _load("industries.json")["industries"]}


@lru_cache
def load_taxonomy() -> dict:
    return _load("taxonomy.json")


@lru_cache
def load_default_policy() -> dict:
    return _load("default_policy.json")


@lru_cache
def load_vendors() -> list[dict]:
    return _load("vendors.json")["vendors"]


def get_tax_rule(rule_id: str, industry_id: str | None = None, pack: str = "us_2026") -> dict:
    """Rule pack entry with the industry override (if any) applied on top."""
    rules = {r["id"]: r for r in load_rule_pack(pack)["rules"]}
    rule = dict(rules.get(rule_id) or rules["other_business"])
    industry = load_industries().get(industry_id or "")
    if industry:
        for ov in industry.get("overrides", []):
            if ov["rule_id"] == rule["id"]:
                rule.update({k: v for k, v in ov.items() if k != "rule_id"})
                rule["overridden_by_industry"] = industry["id"]
    return rule


# --- helpers ----------------------------------------------------------------
def to_local(dt: datetime) -> datetime:
    """Company-local time (for weekend / late-night / same-day checks)."""
    try:
        return dt.astimezone(ZoneInfo(config.COMPANY_TZ))
    except Exception:
        return dt


@lru_cache(maxsize=2048)
def _kw_regex(keyword: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(keyword.lower()) + r"s?(?![a-z0-9])")


def _hits(text: str, keywords: list[str]) -> int:
    text = text.lower()
    return sum(1 for k in keywords if _kw_regex(k).search(text))


def categorize(item_name: str, vendor: str = "") -> tuple[str, bool]:
    """Taxonomy category for one receipt line. Returns (category, confident)."""
    cats = load_taxonomy()["categories"]
    best, best_score = "other", 0
    for cat_id, cat in cats.items():
        score = 2 * _hits(item_name, cat["keywords"]) + 2 * _hits(vendor, cat["vendor_hints"])
        if score > best_score:
            best, best_score = cat_id, score
    if best_score:
        return best, True
    # Fuzzy fallback for OCR noise ("dinnner", "subscripton").
    choices = {k: cat_id for cat_id, cat in cats.items() for k in cat["keywords"] if len(k) >= 5}
    match = process.extractOne(item_name.lower(), list(choices), scorer=fuzz.partial_ratio, score_cutoff=90)
    return (choices[match[0]], False) if match else ("other", False)


def detect_intent(text: str) -> str | None:
    best, best_score = None, 0
    for intent_id, intent in load_taxonomy()["purpose_intents"].items():
        score = _hits(text, intent["keywords"])
        if score > best_score:
            best, best_score = intent_id, score
    return best


def policy_row(policy: dict, category: str) -> dict:
    for row in policy.get("rows", []):
        if row.get("category") == category:
            return row
    return {}


def needs_preapproval(policy: dict, category: str, category_total: float, total: float | None = None) -> bool:
    """Above the category pre-approval line or the company-wide threshold. The Vendor Store uses the
    same test to pick intent=AUTHORIZE, so a held payment never also gets a "no pre-approval" flag."""
    category_line = float(policy_row(policy, category).get("preapproval_above") or 0)
    company_line = float(policy.get("preapproval_threshold") or 0)
    total = category_total if total is None else total
    return (category_line > 0 and category_total > category_line) or (company_line > 0 and total > company_line)


def _money(v: float) -> str:
    return f"${v:,.2f}"


def _count_people(attendees: str) -> int:
    names = re.sub(r"\([^)]*\)", "", attendees or "")      # "Dana Park (Initech, customer)" is one person
    parts = [p for p in re.split(r",|;|\band\b|&|\n", names) if p.strip()]
    return max(1, len(parts))


# --- the pipeline -----------------------------------------------------------
def evaluate(*, expense, items, industry_id: str, policy: dict, followups=(), anomaly_fn=None,
             now: datetime | None = None, rule_overrides: dict | None = None) -> dict:
    """Pure decision function. `expense` / `items` / `followups` are model-like objects.

    anomaly_fn(primary_category, per_txn_limit) -> [{rule, severity, explanation}] is called after
    categorization so the SPC rules know which history to compare against.
    """
    tax = load_taxonomy()
    cats, intents, questions = tax["categories"], tax["purpose_intents"], tax["question_templates"]
    industry = load_industries().get(industry_id, {})
    reasons: list[str] = []
    missing: list[str] = []
    flags: list[str] = []

    # 1. Follow-up answers extend purpose and (for attendee questions) attendees.
    answered = [f for f in followups if (f.answer or "").strip()]
    attendee_qs = {questions[k] for k in ATTENDEE_QUESTIONS}
    attendees = (expense.attendees or "").strip()
    if not attendees:
        attendees = " ".join(f.answer.strip() for f in answered if f.question in attendee_qs)
    purpose = " ".join([(expense.purpose or "").strip()] + [f.answer.strip() for f in answered]).strip()

    # 2. Categorize lines. A category that is already set (earlier review, Finance edit in the
    #    grid) is kept, so a manual correction survives later re-reviews.
    lines = []
    for it in items:
        if it.category in cats:
            category = it.category
        else:
            category, _ = categorize(it.name, expense.vendor or "")
        lines.append({"item": it, "category": category})
    totals: dict[str, float] = {}
    for ln in lines:
        totals[ln["category"]] = totals.get(ln["category"], 0.0) + ln["item"].amount
    primary = max(totals, key=totals.get) if totals else "other"
    primary_total = totals.get(primary, expense.total or 0.0)
    primary_label = cats.get(primary, {}).get("label", primary)

    # 3. Purpose intent and fit score.
    intent = detect_intent(purpose)
    intent_fits = bool(intent) and primary in intents[intent]["fits_categories"]
    row = policy_row(policy, primary)
    limit = float(row.get("per_txn_limit") or 0)
    over_limit = limit > 0 and primary_total > limit
    if primary in industry.get("common_categories", []):
        s_industry = W_INDUSTRY
    elif primary in industry.get("unusual_categories", []):
        s_industry = 0.0
        reasons.append(f"{primary_label} is unusual for a {industry.get('label', industry_id)} company.")
    else:
        s_industry = W_INDUSTRY / 2
    s_intent = W_INTENT if intent_fits else (W_INTENT / 2 if intent is None else 0.0)
    fit = round(s_industry + s_intent + (0.0 if over_limit else W_POLICY), 2)
    if intent_fits:
        reasons.append(f"The stated purpose ({intent.replace('_', ' ')}) fits this {primary_label.lower()} purchase.")
    elif intent:
        reasons.append(f"The stated purpose reads as {intent.replace('_', ' ')}, which does not match a {primary_label.lower()} purchase.")

    # 4. Tax treatment per line (rule pack + industry override decide the percentage).
    out_lines = []
    all_rule_ids = {r["id"] for r in load_rule_pack()["rules"]}
    n_fit_reasons = len(reasons)
    rule_notes: dict[str, dict] = {}
    for ln in lines:
        it, category = ln["item"], ln["category"]
        rule_id = cats.get(category, cats["other"])["default_rule"]
        if category == "meal" and intent and intents[intent].get("meal_rule"):
            rule_id = intents[intent]["meal_rule"]
        if rule_overrides and rule_overrides.get(it.id) in all_rule_ids:
            rule_id = rule_overrides[it.id]
        rule = get_tax_rule(rule_id, industry_id)
        pct = float(rule["deductible_pct"])
        deductible = round(it.amount * pct / 100, 2)
        cap = rule.get("cap_per_recipient_per_year_usd")
        if cap:
            deductible = round(min(it.amount, cap * _count_people(attendees)), 2)
        de_minimis = rule.get("de_minimis_per_item_usd")
        if de_minimis and (it.unit_amount or it.amount) > de_minimis:
            deductible = 0.0
            flags.append("capitalize")
            reasons.append(f"{it.name} costs more than the {_money(de_minimis)} de minimis safe harbor, so it may need to be capitalized (CPA review).")
        if it.amount and deductible != round(it.amount * pct / 100, 2):
            pct = round(deductible / it.amount * 100, 1)
        out_lines.append({
            "line_id": it.id, "name": it.name, "amount": it.amount, "category": category,
            "rule_id": rule["id"], "deductible_pct": pct, "deductible_amount": deductible,
            "form1120_line": rule["form1120_line"], "citation": rule["citation"],
        })
        note = rule_notes.setdefault(rule["id"], {"rule": rule, "amount": 0.0, "deductible": 0.0})
        note["amount"] += it.amount
        note["deductible"] += deductible
    for note in rule_notes.values():
        rule = note["rule"]
        pct = round(note["deductible"] / note["amount"] * 100) if note["amount"] else 0
        extra = " (industry override)" if rule.get("overridden_by_industry") else ""
        reasons.append(f"{rule['label']}: {pct}% deductible, {_money(note['deductible'])} of {_money(note['amount'])}, under {rule['citation']}{extra}.")
    n_tax_reasons = len(reasons)
    rule_ids = {ln["rule_id"] for ln in out_lines}
    line_cats = {ln["category"] for ln in out_lines}

    # 5. Policy check.
    banned_words = [b for b in policy.get("banned_items", []) if any(_hits(ln["item"].name, [b]) for ln in lines)]
    rejected = (
        any(policy_row(policy, c).get("banned") for c in line_cats)
        or any(get_tax_rule(r).get("reimbursable") is False for r in rule_ids)
        or bool(banned_words)
    )
    if rejected:
        reasons.append("This is a personal or banned item under the expense policy, so it is not reimbursable.")
    if over_limit:
        flags.append("over_limit")
        reasons.append(f"{_money(primary_total)} is above the {_money(limit)} policy limit for {primary_label.lower()}.")
    if needs_preapproval(policy, primary, primary_total, expense.total or 0) and expense.intent != "AUTHORIZE":
        flags.append("no_preapproval")
        reasons.append("The amount is above the pre-approval threshold and no pre-approval was requested.")
    now = now or datetime.now(timezone.utc)
    plan = load_rule_pack()["global_substantiation"]["accountable_plan"]
    if expense.purchased_at:
        days = ((expense.substantiated_at or now) - expense.purchased_at).days
        if days > plan["substantiate_within_days"]:
            flags.append("accountable_plan_late")
            reasons.append(f"Substantiated {days} days after purchase; beyond {plan['substantiate_within_days']} days it may be taxable to the employee ({plan['citation']}).")

    # 6. Substantiation gaps and the follow-up question.
    sub = load_rule_pack()["global_substantiation"]
    needs_receipt = "lodging" in line_cats or (expense.total or 0) >= sub["receipt_threshold_usd"]
    if expense.source == "upload" and needs_receipt and not expense.receipt_path:
        missing.append("receipt")
    question_key = None
    if len(purpose.split()) < 4:
        missing.append("business_purpose")
        question_key = "vague_purpose"
    if not attendees:
        for cond, key, label in (
            ("meal_client" in rule_ids, "meal_attendees", "attendees"),
            ("entertainment" in line_cats, "entertainment_attendees", "attendees"),
            ("gift" in line_cats, "gift_recipient", "recipient_name"),
        ):
            if cond:
                missing.append(label)
                question_key = question_key or key
    # Free text cannot be parsed reliably for places and dates, so a travel or client purpose counts
    # as stating the trip; anything else on a travel line gets the travel question.
    if line_cats & {"travel_transport", "lodging"} and intent not in ("travel", "client_meeting"):
        missing.append("travel_destination_and_dates")
        question_key = question_key or "travel_details"
    question = questions[question_key] if question_key else None
    if not question and FIT_FLAG_BELOW <= fit < FIT_ASK_BELOW:
        question = f"How does this {primary_label.lower()} purchase relate to your work? Please describe the business purpose."

    # 7. Anomalies and the final decision.
    anomalies = list(anomaly_fn(primary, limit) if anomaly_fn else [])
    serious = [a for a in anomalies if a["severity"] in ("high", "medium")]
    if rejected:
        decision = "rejected"
    elif question and len(answered) < config.MAX_FOLLOWUP_ROUNDS:
        decision = "needs_info"
    elif question:
        decision, question = "flagged", None
        reasons.append("Still unclear after two follow-up rounds, so Finance must decide.")
    elif flags or serious or fit < FIT_FLAG_BELOW:
        decision = "flagged"
        if fit < FIT_FLAG_BELOW:
            reasons.append("The purchase does not fit the stated purpose, the role or the industry.")
    else:
        decision = "approved"
    if decision != "needs_info":
        question = None
    return {
        "engine": "rules",
        "decision": decision,
        "reimbursable": decision in ("approved", "flagged") and "over_limit" not in flags,
        "fit_score": fit,
        "reasons": reasons,
        "fit_reasons": reasons[:n_fit_reasons],
        "tax_reasons": reasons[n_fit_reasons:n_tax_reasons],
        "policy_reasons": reasons[n_tax_reasons:],
        "rejected_by_policy": rejected,
        "missing": sorted(set(missing)),
        "anomalies": anomalies,
        "flags": flags,
        "question": question,
        "primary_category": primary,
        "intent": intent,
        "lines": out_lines,
    }


def apply_result(session, expense, result: dict) -> dict:
    """Persist a decision (from rules or the LLM): line items, Decision row, follow-up, status."""
    from sqlmodel import select

    from app.models import Decision, FollowUp, LineItem, utcnow

    for ln in result["lines"]:
        li = session.get(LineItem, ln["line_id"])
        if li:
            li.category, li.rule_id = ln["category"], ln["rule_id"]
            li.deductible_pct, li.deductible_amount = ln["deductible_pct"], ln["deductible_amount"]
            li.form1120_line = ln["form1120_line"]
            session.add(li)
    session.add(Decision(
        expense_id=expense.id, engine=result["engine"], reimbursable=result["reimbursable"],
        decision=result["decision"], fit_score=result["fit_score"],
        reasons_json=json.dumps(result["reasons"]), missing_json=json.dumps(result["missing"]),
        anomaly_json=json.dumps(result["anomalies"]), question=result["question"],
    ))
    if result["question"]:
        open_q = session.exec(
            select(FollowUp).where(FollowUp.expense_id == expense.id, FollowUp.answer == None)  # noqa: E711
        ).first()
        if not open_q:
            session.add(FollowUp(expense_id=expense.id, question=result["question"]))
    if expense.status != "paid":
        status = result["decision"]
        held = expense.intent == "AUTHORIZE" and expense.paypal_authorization_id and not expense.paypal_capture_id
        if held and status in ("approved", "flagged"):
            status = "held"                       # money stays authorized until Finance decides
        expense.status = status
        if status == "approved" and not expense.substantiated_at:
            expense.substantiated_at = utcnow()
        session.add(expense)
    session.commit()
    result["status"] = expense.status
    return result


def load_context(session, expense_id: int) -> dict:
    """Everything a review needs: expense, company, policy, line items, follow-ups."""
    from sqlmodel import select

    from app.models import Company, Expense, FollowUp, LineItem

    expense = session.get(Expense, expense_id)
    if expense is None:
        raise ValueError(f"expense {expense_id} not found")
    company = session.exec(select(Company)).first()
    return {
        "expense": expense,
        "company": company,
        "policy": json.loads(company.policy_json or "{}") or load_default_policy(),
        "items": session.exec(select(LineItem).where(LineItem.expense_id == expense_id)).all(),
        "followups": session.exec(select(FollowUp).where(FollowUp.expense_id == expense_id).order_by(FollowUp.id)).all(),
    }


def run_rules(session, ctx: dict, rule_overrides: dict | None = None) -> dict:
    """Rules decision for a loaded context, not persisted. `rule_overrides` maps line id -> rule id
    (the LLM's choice of rule); percentages still come from the rule pack."""
    from app.anomaly import spc

    expense, items = ctx["expense"], ctx["items"]
    return evaluate(
        expense=expense, items=items, industry_id=ctx["company"].industry_id, policy=ctx["policy"],
        followups=ctx["followups"], rule_overrides=rule_overrides,
        anomaly_fn=lambda category, limit: spc.check(session, expense, category, limit, len(items)),
    )


def review_expense(session, expense_id: int) -> dict:
    """Run the rules pipeline on a stored expense and persist the result."""
    ctx = load_context(session, expense_id)
    return apply_result(session, ctx["expense"], run_rules(session, ctx))
