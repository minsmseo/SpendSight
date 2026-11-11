"""Deterministic decision pipeline: the no-AI fallback and the guardrail for the LLM.

Step 1 (done): data loaders + tax rule resolution with industry overrides.
Step 2 (TODO): categorize -> purpose fit -> policy check -> tax treatment -> anomaly -> decision.
No tax logic is hardcoded here; everything comes from the JSON packs so `kr_2026.json` can be added.
"""
import json
from functools import lru_cache

from app import config


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


def review_expense(session, expense_id: int) -> dict:
    """TODO(step 2): run the full rules pipeline and return a Decision-shaped dict.

    Order: normalize -> categorize (taxonomy + rapidfuzz) -> purpose intent + fit score
    (0.4 industry / 0.4 intent match / 0.2 within policy) -> policy check -> tax treatment
    per line via get_tax_rule -> anomaly (app.anomaly.spc) -> decision + template question.
    """
    raise NotImplementedError("rules pipeline lands in build step 2")
