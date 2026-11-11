"""LLM path with a scripted fake provider: agent loop, guardrails, vision, and fallback to rules."""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.ai import agent
from app.ai import provider as ai_provider
from app.ai.provider import ProviderError, ToolTurn
from app.db import engine as db_engine
from app.db import init_db
from app.extract import vision
from app.models import FollowUp, LineItem
from seed import seed_if_empty
from tests.test_rules import make, weekday_noon

ALICE, BOB, CAROL = 1, 2, 3


class FakeProvider:
    """Plays back a script of turns: each is a list of (tool_name, input) calls, or an exception."""

    name, model = "fake", "fake-1"

    def __init__(self, script=(), vision=None):
        self.script, self.vision, self.seen = list(script), vision, []

    def chat_with_tools(self, messages, tools, timeout=45, system=""):
        self.seen.append(messages[-1])
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        calls = [{"id": f"call{len(self.seen)}_{i}", "name": n, "input": a} for i, (n, a) in enumerate(step)]
        return ToolTurn(tool_calls=calls, raw_assistant={"role": "assistant", "content": "..."})

    def tool_result_messages(self, results):
        return [{"role": "user", "content": results}]

    def vision_json(self, image_bytes, mime, prompt, schema):
        if isinstance(self.vision, Exception):
            raise self.vision
        return self.vision

    def complete_json(self, system, user, schema, timeout=30):
        return {"ok": True}


_seat_prices = iter(range(31, 60))


def seat(session):
    """A small, clearly approvable software expense; a new amount each time so duplicate detection stays quiet."""
    return make(session, CAROL, "CloudStack SaaS", [("Additional seat", float(next(_seat_prices)))],
                "Extra seat subscription for new analyst")


@pytest.fixture(scope="module")
def session():
    init_db()
    seed_if_empty()
    with Session(db_engine) as s:
        yield s


@pytest.fixture
def use(monkeypatch):
    def install(fake):
        monkeypatch.setattr(ai_provider, "get_provider", lambda: fake)
        return fake
    yield install
    ai_provider.mark_degraded("", seconds=0)


def lines(session, exp, category, rule_id, pct=None):
    items = session.exec(select(LineItem).where(LineItem.expense_id == exp.id)).all()
    extra = {"deductible_pct": pct} if pct is not None else {}
    return [{"line_id": i.id, "category": category, "rule_id": rule_id, **extra} for i in items]


def submit(session, exp, category, rule_id, decision="approved", pct=None, reasons=("Fits the stated purpose.",)):
    return ("submit_decision", {"decision": decision, "reimbursable": decision == "approved", "fit_score": 0.9,
                                "reasons": list(reasons), "lines": lines(session, exp, category, rule_id, pct)})


def test_agent_uses_tools_then_decides_and_rule_pack_sets_the_percentage(session, use):
    exp = make(session, ALICE, "Steakhouse 21", [("Chef tasting menu", 120.0)],
               "Dinner with Hooli CFO to discuss renewal", "Dana Roe (Hooli CFO, customer)", when=weekday_noon(3))
    fake = use(FakeProvider([
        [("lookup_tax_rule", {"category": "meal"}), ("get_employee_history", {"category": "meal"})],
        [submit(session, exp, "meal", "meal_client", pct=100, reasons=["Client dinner with a named customer executive."])],
    ]))
    r = agent.review(session, exp.id)
    assert (r["engine"], r["decision"], exp.status) == ("llm", "approved", "approved")
    assert r["lines"][0]["deductible_pct"] == 50 and r["lines"][0]["deductible_amount"] == 60.0
    assert r["reasons"][0] == "Client dinner with a named customer executive."
    assert any("rule pack sets 50%" in x for x in r["reasons"])          # conflict noted, pack value used
    tool_results = fake.seen[1]["content"]
    assert '"meal_client"' in tool_results[0]["content"] and '"count"' in tool_results[1]["content"]


def test_agent_question_and_rules_guardrails(session, use):
    # The agent asks its own question.
    exp = make(session, CAROL, "Gift Box Co", [("Client thank-you gift box", 60.0)],
               "Thank-you gift after the Initech launch", when=weekday_noon(4))
    use(FakeProvider([[("ask_employee", {"question": "Who received the gift and what is their role at Initech?"})]]))
    r = agent.review(session, exp.id)
    assert (r["engine"], r["decision"]) == ("llm", "needs_info")
    assert session.exec(select(FollowUp).where(FollowUp.expense_id == exp.id)).one().question.startswith("Who received")

    # The agent approves a client meal with no attendees: required substantiation still triggers the question.
    exp = make(session, BOB, "Steakhouse 21", [("Dinner entree", 62.0)],
               "Dinner with Globex CTO to discuss renewal", when=weekday_noon(5))
    use(FakeProvider([[submit(session, exp, "meal", "meal_client")]]))
    r = agent.review(session, exp.id)
    assert r["decision"] == "needs_info" and "attended" in r["question"]

    # The agent approves gym as "wellness": the policy ban still rejects it.
    exp = make(session, BOB, "Fitness Club", [("Gym membership (monthly)", 79.0)], "team wellness program for engineering")
    use(FakeProvider([[submit(session, exp, "personal", "personal")]]))
    r = agent.review(session, exp.id)
    assert (r["engine"], r["decision"], r["reimbursable"]) == ("llm", "rejected", False)

    # The agent approves; the rule engine finds the amount is over the policy limit.
    exp = make(session, CAROL, "CloudStack SaaS", [("Team plan subscription (annual)", 900.0)],
               "Annual analytics subscription for marketing team")
    use(FakeProvider([[submit(session, exp, "software", "software")]]))
    r = agent.review(session, exp.id)
    assert r["decision"] == "flagged" and "over_limit" in r["flags"] and r["reimbursable"] is False


def test_agent_recovers_from_invalid_decision_and_plain_text(session, use):
    exp = seat(session)
    fake = use(FakeProvider([
        [],                                                                   # plain text, no tool call -> nudged
        [("submit_decision", {"decision": "approved", "fit_score": 7})],      # invalid -> error result, retry
        [submit(session, exp, "software", "software")],
    ]))
    r = agent.review(session, exp.id)
    assert (r["engine"], r["decision"]) == ("llm", "approved")
    assert fake.seen[2]["content"][0]["is_error"] is True


@pytest.mark.parametrize("failure,degraded", [
    (ProviderError("credit", "credit balance is too low"), True),
    (ProviderError("auth"), True),
    (ProviderError("timeout"), False),
    (ProviderError("refusal"), False),
    (RuntimeError("boom"), False),
])
def test_any_llm_failure_falls_back_to_rules(session, use, failure, degraded):
    exp = seat(session)
    use(FakeProvider([failure]))
    r = agent.review(session, exp.id)
    assert (r["engine"], r["decision"], exp.status) == ("rules", "approved", "approved")
    assert ai_provider.is_degraded() is degraded


def test_agent_that_never_decides_falls_back_to_rules(session, use):
    exp = seat(session)
    use(FakeProvider([[("get_expense", {})]] * 10))
    assert agent.review(session, exp.id)["engine"] == "rules"


def test_vision_extraction_and_fallback(use):
    use(FakeProvider(vision={
        "vendor": "COFFEE & CO", "date": "2026-11-10", "currency": "USD", "total": 64.0, "tax": 0,
        "items": [{"name": "Office coffee box (12 cups)", "quantity": 1, "unit_amount": 34, "amount": 34},
                  {"name": "Assorted office snacks", "quantity": 1, "unit_amount": 30, "amount": 30}]}))
    got = vision.extract_receipt(b"png", "image/png")
    assert got["engine"] == "llm" and got["fields"]["total"] == 64.0 and len(got["fields"]["items"]) == 2
    assert got["confidence"]["total"] == 0.9
    use(FakeProvider(vision={"vendor": "", "date": "", "currency": "USD", "total": 0, "tax": 0, "items": []}))
    assert vision.extract_receipt(b"png", "image/png")["confidence"]["vendor"] == 0.2
    use(FakeProvider(vision=ProviderError("credit")))
    assert vision.extract_receipt(b"png", "image/png")["engine"] == "ocr"
    assert ai_provider.is_degraded()


def test_status_banner_and_test_button(monkeypatch):
    from app.main import app

    with TestClient(app) as client:
        assert client.post("/api/settings/ai-test").json()["ok"] is False          # no key set
        client.post("/settings/ai-key", data={"provider": "anthropic", "key": "sk-test-not-real"})
        try:
            status = client.get("/api/status").json()["ai"]
            assert (status["state"], status["provider"], status["model"]) == ("connected", "anthropic", "claude-opus-5-5")
            monkeypatch.setattr(ai_provider, "build_provider", lambda name: FakeProvider())
            assert client.post("/api/settings/ai-test").json() == {"ok": True, "provider": "anthropic", "model": "fake-1"}
            ai_provider.mark_degraded("credit")
            page = client.get("/settings").text
            assert "AI unavailable (out of credit). Running on rule engine." in page
            assert "sk-test-not-real" not in page
        finally:
            client.post("/settings/ai-key", data={"provider": "anthropic", "key": ""})
        assert client.get("/api/status").json()["ai"]["state"] == "not_set"
