"""Policy text -> policy table using regex only (no AI). TODO(step 6).

Looks for money amounts near category keywords (meal, dinner, hotel, lodging, airfare, gift,
software, per day, per night) and phrases like "requires pre-approval", "not reimbursable",
"prohibited", "receipt required". Output uses the same schema as rules/default_policy.json.
"""
from app.rules.engine import load_default_policy


def parse_policy_text(text: str) -> dict:
    # TODO(step 6): regex extraction. Until then, start from the default policy.
    return dict(load_default_policy())
