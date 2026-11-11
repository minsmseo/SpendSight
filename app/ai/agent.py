"""ExpenseAgent: tool-calling loop (max 6 tool calls per expense) with the rule engine as guardrail.

The LLM decides *which category and tax rule, and why*. The rule pack decides *the percentage*,
and policy limits, substantiation gaps and anomaly checks from the rule engine are applied on top
of the LLM's decision. Any LLM failure falls back to the rule engine; `review` never raises
because of the LLM.
"""
import json
import logging

from pydantic import BaseModel, Field, ValidationError

from app import config
from app.ai import prompts
from app.ai import provider as ai_provider
from app.ai.provider import ProviderError
from app.rules import engine

log = logging.getLogger("spendsight.ai")
DECISIONS = ("approved", "needs_info", "flagged", "rejected")
# Substantiation the tax rules require; if it is missing the employee is asked even when the LLM approved.
REQUIRED_SUBSTANTIATION = {"attendees", "recipient_name", "business_purpose"}


class LineDecision(BaseModel):
    line_id: int
    category: str
    rule_id: str
    deductible_pct: float | None = None


class AgentDecision(BaseModel):
    decision: str
    reimbursable: bool = True
    fit_score: float = Field(default=0.8, ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)
    lines: list[LineDecision] = Field(default_factory=list)
    question: str | None = None


def _tools() -> list[dict]:
    categories = list(engine.load_taxonomy()["categories"])
    rule_ids = [r["id"] for r in engine.load_rule_pack()["rules"]]
    obj = lambda props, required: {"type": "object", "properties": props, "required": required}  # noqa: E731
    return [
        {"name": "get_company_profile",
         "description": "Returns the company's industry profile (common and unusual expense categories) and its confirmed expense policy table. Already included in the task; call it only if you need to re-read it.",
         "input_schema": obj({}, [])},
        {"name": "get_expense",
         "description": "Returns the expense under review: receipt, line items with ids, business purpose, attendees and follow-up answers. Already included in the task; call it only if you need to re-read it.",
         "input_schema": obj({"expense_id": {"type": "integer"}}, [])},
        {"name": "get_paypal_order",
         "description": "Returns the live PayPal order for this expense (status, itemized purchase units, capture or authorization). Use it when the expense was paid through PayPal and you want to confirm the receipt against PayPal's record.",
         "input_schema": obj({"order_id": {"type": "string"}}, [])},
        {"name": "lookup_tax_rule",
         "description": "Returns the candidate US 2026 tax rules for a category, each with its id, deductible percentage, citation and required substantiation, with this company's industry overrides applied. Call it before choosing between rules for meals, entertainment or gifts.",
         "input_schema": obj({"category": {"type": "string", "enum": categories},
                              "context": {"type": "string", "description": "Who, where and why, in a few words."}}, ["category"])},
        {"name": "get_employee_history",
         "description": "Returns this employee's past spending in a category (count, mean, control limits, recent amounts) so you can tell whether this purchase is unusual for them.",
         "input_schema": obj({"employee_id": {"type": "integer"}, "category": {"type": "string", "enum": categories}}, ["category"])},
        {"name": "ask_employee",
         "description": "Ask the employee one specific question when something needed to judge the expense is missing (attendees and their business relationship, gift recipient, trip destination and dates, or a vague purpose). Ends the review until they answer.",
         "input_schema": obj({"question": {"type": "string"}}, ["question"])},
        {"name": "submit_decision",
         "description": "Submit the final decision. Include every line item with its category and tax rule id, and one plain sentence per reason.",
         "input_schema": obj({
             "decision": {"type": "string", "enum": ["approved", "flagged", "rejected"]},
             "reimbursable": {"type": "boolean"},
             "fit_score": {"type": "number", "description": "0 to 1"},
             "reasons": {"type": "array", "items": {"type": "string"}},
             "lines": {"type": "array", "items": obj({
                 "line_id": {"type": "integer"},
                 "category": {"type": "string", "enum": categories},
                 "rule_id": {"type": "string", "enum": rule_ids},
                 "deductible_pct": {"type": "number", "description": "Optional: the percentage you expect; the rule pack value is what gets applied."},
             }, ["line_id", "category", "rule_id"])},
         }, ["decision", "reimbursable", "fit_score", "reasons", "lines"])},
    ]


# --- tool implementations (scoped to the expense under review) ----------------
def _company_profile(ctx: dict) -> dict:
    company = ctx["company"]
    industry = engine.load_industries().get(company.industry_id, {})
    return {
        "company": company.name, "entity_type": "C corporation (Form 1120)", "tax_year": company.tax_year,
        "industry": industry.get("label", company.industry_id),
        "common_categories": industry.get("common_categories", []),
        "unusual_categories": industry.get("unusual_categories", []),
        "policy": ctx["policy"],
    }


def _receipt_kind(e) -> str:
    """What stands behind this expense as a receipt, in words the model cannot misread."""
    if e.source == "paypal":
        return "PayPal itemized order (structured receipt from PayPal; this is the receipt)"
    if e.source == "import":
        return "PayPal transaction record (imported account history)"
    return "uploaded receipt image" if e.receipt_path else "no receipt attached"


def _expense_view(session, ctx: dict) -> dict:
    from app.models import Employee

    e = ctx["expense"]
    emp = session.get(Employee, e.employee_id)
    return {
        "expense_id": e.id, "source": e.source, "vendor": e.vendor,
        "purchased_at_local": engine.to_local(e.purchased_at).strftime("%Y-%m-%d %H:%M %a") if e.purchased_at else None,
        "total": e.total, "tax": e.tax_amount, "currency": e.currency,
        "business_purpose": e.purpose or "(none given)", "attendees": e.attendees or "(none given)",
        "employee": {"id": emp.id, "name": emp.name, "role": emp.role_title, "department": emp.department} if emp else None,
        "paypal_order_id": e.paypal_order_id, "receipt": _receipt_kind(e),
        "line_items": [{"line_id": i.id, "name": i.name, "quantity": i.quantity, "amount": i.amount} for i in ctx["items"]],
        "follow_ups": [{"question": f.question, "answer": f.answer} for f in ctx["followups"]],
    }


def _run_tool(session, ctx: dict, name: str, args: dict) -> dict:
    expense = ctx["expense"]
    if name == "get_company_profile":
        return _company_profile(ctx)
    if name == "get_expense":
        return _expense_view(session, ctx)
    if name == "get_paypal_order":
        if not expense.paypal_order_id:
            return {"error": "This expense was not paid through PayPal."}
        from app.paypal import get_paypal_client

        order = get_paypal_client().get_order(expense.paypal_order_id)
        unit = (order.get("purchase_units") or [{}])[0]
        return {"id": order.get("id"), "status": order.get("status"), "intent": order.get("intent"),
                "merchant": unit.get("description"), "amount": unit.get("amount"), "items": unit.get("items"),
                "payments": unit.get("payments")}
    if name == "lookup_tax_rule":
        category = args.get("category", "")
        industry_id = ctx["company"].industry_id
        rules = [engine.get_tax_rule(r["id"], industry_id) for r in engine.load_rule_pack()["rules"]
                 if r["category"] == category or (category == "meal" and r["id"] == "event_all_staff")]
        if not rules:
            default = engine.load_taxonomy()["categories"].get(category, {}).get("default_rule", "other_business")
            rules = [engine.get_tax_rule(default, industry_id)]
        keep = ("id", "label", "deductible_pct", "citation", "substantiation", "notes", "overridden_by_industry",
                "cap_per_recipient_per_year_usd", "de_minimis_per_item_usd")
        return {"category": category, "rules": [{k: r[k] for k in keep if k in r} for r in rules]}
    if name == "get_employee_history":
        from app.anomaly import spc

        series = spc.chart_series(session, expense.employee_id, args.get("category", ""), exclude_id=expense.id)
        amounts = [p["amount"] for p in series["points"]]
        return {"category": args.get("category"), "scope": series.get("scope"), "count": len(amounts),
                "mean": series["mean"], "plus_2_sigma": series["ucl2"], "plus_3_sigma": series["ucl3"],
                "recent_amounts": amounts[-8:]}
    return {"error": f"Unknown tool {name}"}


# --- the loop -----------------------------------------------------------------
def _run_agent(provider, session, ctx: dict) -> AgentDecision:
    tools = _tools()
    company = ctx["company"]
    industry = engine.load_industries().get(company.industry_id, {}).get("label", company.industry_id)
    system = prompts.AGENT_SYSTEM.format(company=company.name, industry=industry)
    task = prompts.AGENT_TASK.format(
        company=json.dumps(_company_profile(ctx)),
        expense=json.dumps(_expense_view(session, ctx)),
        categories="\n".join(
            f"- {cid}: {c['label']}" + (f" (for example: {', '.join(c['keywords'][:10])})" if c["keywords"] else "")
            for cid, c in engine.load_taxonomy()["categories"].items()),
        rules=", ".join(r["id"] for r in engine.load_rule_pack()["rules"]),
    )
    messages: list = [{"role": "user", "content": task}]
    calls, nudged, retried = 0, False, False
    while True:
        turn = provider.chat_with_tools(messages, tools, timeout=45, system=system)
        if turn.raw_assistant:
            messages.append(turn.raw_assistant)
        if not turn.tool_calls:
            if nudged:
                raise ProviderError("bad_json", "agent did not finish with a tool call")
            nudged = True
            messages.append({"role": "user", "content": "Finish by calling submit_decision or ask_employee."})
            continue
        results = []
        for call in turn.tool_calls:
            calls += 1
            args = call["input"] if isinstance(call["input"], dict) else {}
            if call["name"] == "ask_employee" and str(args.get("question", "")).strip():
                return AgentDecision(decision="needs_info", reimbursable=False, question=str(args["question"]).strip())
            if call["name"] == "submit_decision":
                try:
                    decision = AgentDecision.model_validate(args)
                    if decision.decision not in DECISIONS:
                        raise ValueError("decision must be approved, flagged or rejected")
                    return decision
                except (ValidationError, ValueError) as exc:
                    if retried:
                        raise ProviderError("bad_json", "agent decision failed validation twice")
                    retried = True
                    results.append({"id": call["id"], "is_error": True,
                                    "content": f"Invalid decision: {str(exc)[:400]}. Call submit_decision again with corrected input."})
                    continue
            try:
                payload = _run_tool(session, ctx, call["name"], args)
                results.append({"id": call["id"], "content": json.dumps(payload, default=str)})
            except Exception as exc:                        # a tool failure is reported to the model, not raised
                results.append({"id": call["id"], "is_error": True, "content": f"Tool failed: {type(exc).__name__}"})
        if calls >= config.AGENT_MAX_TOOL_CALLS:
            raise ProviderError("other", "agent used its tool budget without deciding")
        messages.extend(provider.tool_result_messages(results))


def _merge(session, ctx: dict, llm: AgentDecision) -> dict:
    """Combine the LLM decision with the rule engine: pack percentages, policy, substantiation, anomalies."""
    cats = engine.load_taxonomy()["categories"]
    rule_ids = {r["id"] for r in engine.load_rule_pack()["rules"]}
    certain = set(engine.load_rule_pack().get("certain_rule_ids", []))
    by_id = {i.id: i for i in ctx["items"]}
    overrides: dict[int, str] = {}
    stated: dict[int, float] = {}
    for ln in llm.lines:
        item = by_id.get(ln.line_id)
        if not item or ln.category not in cats or ln.rule_id not in rule_ids:
            continue                                      # unknown ids: the rules choice stands for that line
        item.category = ln.category
        overrides[item.id] = ln.rule_id
        if ln.deductible_pct is not None:
            stated[item.id] = ln.deductible_pct

    rules = engine.run_rules(session, ctx, rule_overrides=overrides)
    reasons = [r.strip() for r in llm.reasons if r and r.strip()][:4]
    for ln in rules["lines"]:
        said = stated.get(ln["line_id"])
        if said is None or abs(said - ln["deductible_pct"]) < 0.5:
            continue
        if ln["rule_id"] in certain or said > ln["deductible_pct"]:
            reasons.append(f"AI suggested {said:g}% for {ln['name']}, but the tax rule pack sets {ln['deductible_pct']:g}% ({ln['citation']}), so the rule pack value is used.")
        else:                                             # uncertain rule and the AI is more conservative: take it, for CPA review
            ln["deductible_pct"] = max(0.0, said)
            ln["deductible_amount"] = round(ln["amount"] * ln["deductible_pct"] / 100, 2)
            reasons.append(f"AI lowered {ln['name']} to {said:g}% deductible; verify with your CPA.")
    reasons += rules["tax_reasons"] + rules["policy_reasons"]

    answered = sum(1 for f in ctx["followups"] if (f.answer or "").strip())
    can_ask = answered < config.MAX_FOLLOWUP_ROUNDS
    serious = [a for a in rules["anomalies"] if a["severity"] in ("high", "medium")]
    required_gap = REQUIRED_SUBSTANTIATION & set(rules["missing"])
    decision, question = llm.decision, None
    if rules["rejected_by_policy"] or decision == "rejected":
        decision = "rejected"
    elif decision == "needs_info" and llm.question and can_ask:
        question = llm.question
    elif required_gap and can_ask and rules["question"]:
        decision, question = "needs_info", rules["question"]
        reasons.append("Required substantiation is missing, so the employee is asked before this can be approved.")
    elif decision == "needs_info" or required_gap:
        decision = "flagged"
        reasons.append("Still unclear after two follow-up rounds, so Finance must decide.")
    elif decision == "approved" and (rules["flags"] or serious):
        decision = "flagged"                              # policy breach or anomaly found by the rule engine
    return {
        **rules,
        "engine": "llm",
        "decision": decision,
        "reimbursable": decision in ("approved", "flagged") and "over_limit" not in rules["flags"] and (llm.reimbursable or decision == "approved"),
        "fit_score": round(llm.fit_score, 2),
        "reasons": reasons,
        "question": question,
    }


def review(session, expense_id: int) -> dict:
    """Single entry point for the review pipeline: LLM agent if available, else rules."""
    provider = ai_provider.get_provider()
    if provider is not None:
        try:
            ctx = engine.load_context(session, expense_id)
            result = _merge(session, ctx, _run_agent(provider, session, ctx))
            return engine.apply_result(session, ctx["expense"], result)
        except ProviderError as exc:
            ai_provider.report_failure(exc)
        except Exception as exc:                            # anything unexpected on the LLM path -> rules
            log.warning("AI review failed (%s); using the rule engine", type(exc).__name__)
        session.rollback()                                  # drop any category changes made while merging
    return engine.review_expense(session, expense_id)
