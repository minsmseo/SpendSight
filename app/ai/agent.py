"""ExpenseAgent: tool-calling loop (max 6 tool calls per expense). TODO(step 5).

Tools: get_company_profile, get_expense, get_paypal_order, lookup_tax_rule,
get_employee_history, ask_employee (ends loop), submit_decision (ends loop).
Guardrail: the rule engine always runs too; for rule ids in the pack's `certain_rule_ids`
the pack's deductible % wins and the conflict is noted in the reasons.
"""
from app.ai import provider as ai_provider
from app.rules import engine

TOOLS = [
    {"name": "get_company_profile", "description": "Industry and confirmed policy table.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_expense", "description": "Receipt, items, purpose, attendees, follow-ups.", "input_schema": {"type": "object", "properties": {"expense_id": {"type": "integer"}}, "required": ["expense_id"]}},
    {"name": "get_paypal_order", "description": "Live order details from PayPal.", "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}},
    {"name": "lookup_tax_rule", "description": "Rule pack entry with citation.", "input_schema": {"type": "object", "properties": {"category": {"type": "string"}, "context": {"type": "string"}}, "required": ["category"]}},
    {"name": "get_employee_history", "description": "Spending stats for anomaly context.", "input_schema": {"type": "object", "properties": {"employee_id": {"type": "integer"}, "category": {"type": "string"}}, "required": ["employee_id", "category"]}},
    {"name": "ask_employee", "description": "Ask one specific question; sets needs_info.", "input_schema": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}},
    {"name": "submit_decision", "description": "Final decision object.", "input_schema": {"type": "object", "properties": {"decision": {"type": "string"}, "reimbursable": {"type": "boolean"}, "fit_score": {"type": "number"}, "reasons": {"type": "array", "items": {"type": "string"}}, "lines": {"type": "array", "items": {"type": "object"}}}, "required": ["decision", "reimbursable", "reasons"]}},
]


def review(session, expense_id: int) -> dict:
    """Single entry point for the review pipeline: LLM agent if available, else rules. Never raises
    because of the LLM."""
    provider = ai_provider.get_provider()
    if provider is not None:
        try:
            return _run_agent(provider, session, expense_id)
        except ai_provider.ProviderError as exc:
            if exc.kind in ("auth", "credit", "rate_limit"):
                ai_provider.mark_degraded(exc.kind)
        except Exception:
            pass  # any other LLM-side failure -> rules
    return engine.review_expense(session, expense_id)


def _run_agent(provider, session, expense_id: int) -> dict:
    raise ai_provider.ProviderError("other", "agent loop lands in build step 5")
