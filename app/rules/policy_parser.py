"""Expense policy document -> summary + policy table.

With an AI key: one JSON call returns the summary and the table. Without (or on any failure):
the summary comes from summarize/ and the table from regex over the raw text. Either way the
result has the schema of rules/default_policy.json and Finance confirms it in an editable grid.
"""
import copy
import io
import re

from pydantic import BaseModel, Field

from app.ai import prompts
from app.ai import provider as ai_provider
from app.ai.provider import ProviderError
from app.rules.engine import load_default_policy, load_taxonomy
from app.summarize import summarizer

MAX_POLICY_CHARS = 150_000
AMOUNT = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)")
BANNED_PHRASE = re.compile(r"\b(not reimbursable|non-reimbursable|will not be reimbursed|prohibited|not allowed|not permitted)\b", re.I)
PER_UNIT = re.compile(r"\bper (night|day)\b", re.I)
# Keywords that tie a policy sentence to a category (first match wins, so order matters).
CATEGORY_WORDS = [
    ("lodging", ["hotel", "lodging", "accommodation"]),
    ("travel_transport", ["airfare", "flight", "airline", "rideshare", "taxi", "rental car", "train"]),
    ("entertainment", ["entertainment", "tickets", "ticket", "golf"]),
    ("gift", ["gift"]),
    ("software", ["software", "subscription", "saas"]),
    ("office_supplies", ["office supplies", "supplies", "accessories", "equipment"]),
    ("advertising", ["advertising", "marketing", "ads"]),
    ("company_event", ["team event", "holiday party", "offsite", "team outing"]),
    ("meal", ["meal", "dinner", "lunch", "breakfast", "snack", "coffee"]),
]
BANNED_CATEGORY_WORDS = [
    ("fines_penalties", ["fine", "penalt", "ticket violation"]),
    ("political", ["political", "lobbying", "campaign"]),
    ("personal", ["gym", "clothing", "groceries", "personal", "commut", "spa", "family"]),
]


# --- text extraction ----------------------------------------------------------
def extract_text(data: bytes, filename: str) -> str:
    """Plain text from PDF, DOCX, MD or TXT. Raises ValueError for anything else."""
    name = filename.lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader

        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
    if name.endswith(".docx"):
        import docx

        doc = docx.Document(io.BytesIO(data))
        cells = [c.text for t in doc.tables for row in t.rows for c in row.cells]
        return "\n".join([p.text for p in doc.paragraphs] + cells)
    if name.endswith((".md", ".txt", ".markdown")):
        return data.decode("utf-8", errors="replace")
    raise ValueError("Upload a PDF, DOCX, MD or TXT file")


# --- regex rule extraction ------------------------------------------------------
def _num(s: str) -> float:
    v = float(s.replace(",", ""))
    return int(v) if v == int(v) else v


def _category(sentence: str) -> str | None:
    low = sentence.lower()
    for category, words in CATEGORY_WORDS:
        if any(re.search(r"\b" + re.escape(w), low) for w in words):
            return category
    return None


def _singular(phrase: str) -> str:
    words = phrase.split()
    if words and len(words[-1]) > 3 and words[-1].endswith("s") and not words[-1].endswith("ss"):
        words[-1] = words[-1][:-1]
    return " ".join(words)


def _banned_items(sentence: str) -> list[str]:
    last = list(BANNED_PHRASE.finditer(sentence))[-1]
    subject = BANNED_PHRASE.sub(" ", sentence[:last.start()])      # also drops a heading that repeats the phrase
    subject = re.sub(r"\b(is|are|the following|expenses?|such as|including)\b", " ", subject, flags=re.I)
    parts = re.split(r",|;|\band\b|\bor\b", subject)
    items = [_singular(re.sub(r"[^a-z' -]", "", p.lower()).strip()) for p in parts]
    return [i for i in items if 3 <= len(i) and len(i.split()) <= 4]


def parse_policy_text(text: str) -> dict:
    """Regex extraction on the raw text. Starts from the default policy and overrides what it finds;
    `found` lists the values that came from the document."""
    policy = copy.deepcopy(load_default_policy())
    rows = {r["category"]: r for r in policy["rows"]}
    found: list[str] = []
    banned: list[str] = []
    limits: dict[str, list[float]] = {}

    for sentence in summarizer.split_sentences(text):
        category = _category(sentence)
        if BANNED_PHRASE.search(sentence):
            for item in _banned_items(sentence):
                if item not in banned:
                    banned.append(item)
                for cat, words in BANNED_CATEGORY_WORDS:
                    if any(w in item for w in words) and cat in rows:
                        rows[cat].update(banned=True, per_txn_limit=0, preapproval_above=0)
            if not AMOUNT.search(sentence):
                continue
        for m in AMOUNT.finditer(sentence):
            value = _num(m.group(1))
            before = sentence[max(0, m.start() - 60):m.start()].lower()
            after = sentence[m.end():m.end() + 30].lower()
            if re.search(r"approv", before) or (re.search(r"approv", sentence.lower()) and re.search(r"above|over|exceed|more than", before)):
                if category and not re.search(r"\bany (single )?(purchase|expense)\b", sentence.lower()):
                    rows[category]["preapproval_above"] = value
                    found.append(f"{category}: pre-approval above ${value}")
                else:
                    policy["preapproval_threshold"] = value
                    found.append(f"pre-approval threshold ${value}")
            elif "receipt" in sentence.lower() and not re.search(r"up to|limit|maximum", before):
                if category and "any expense" not in sentence.lower():
                    rows[category]["receipt_required_above"] = value
                else:
                    policy["receipt_required_above"] = value
                found.append(f"receipt required from ${value}")
            elif category:
                unit = PER_UNIT.search(after)
                if unit:                                       # a nightly / daily rate is not a per-purchase limit
                    note = f"Up to ${value} per {unit.group(1).lower()}."
                    if note not in rows[category]["notes"]:
                        rows[category]["notes"] = (rows[category]["notes"] + " " + note).strip()
                    found.append(f"{category}: ${value} per {unit.group(1).lower()}")
                else:
                    limits.setdefault(category, []).append(value)
    for category, values in limits.items():
        rows[category]["per_txn_limit"] = max(values)          # several limits in one category: the highest applies
        found.append(f"{category}: limit ${max(values)}")
    if banned:
        policy["banned_items"] = banned
        found.append(f"{len(banned)} banned items")
    policy["found"] = found
    return policy


# --- sanitizing (used for LLM output and for the grid on confirm) -----------------
def sanitize_policy(raw: dict) -> dict:
    """Coerce any policy-shaped dict into the stored schema; unknown categories and bad numbers are dropped."""
    default = load_default_policy()
    categories = set(load_taxonomy()["categories"])

    def number(v, fallback=0.0):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return fallback
        v = max(0.0, min(v, 1_000_000.0))
        return int(v) if v == int(v) else round(v, 2)

    rows, seen = [], set()
    for r in raw.get("rows") or []:
        cat = str(r.get("category", "")).strip()
        if cat not in categories or cat in seen:
            continue
        seen.add(cat)
        rows.append({
            "category": cat, "per_txn_limit": number(r.get("per_txn_limit")),
            "preapproval_above": number(r.get("preapproval_above")),
            "receipt_required_above": number(r.get("receipt_required_above")),
            "banned": bool(r.get("banned")), "notes": str(r.get("notes") or "")[:200],
        })
    items = raw.get("banned_items") or []
    if isinstance(items, str):
        items = items.split(",")
    return {
        "preapproval_threshold": number(raw.get("preapproval_threshold"), default["preapproval_threshold"]),
        "receipt_required_above": number(raw.get("receipt_required_above"), default["receipt_required_above"]),
        "late_night_window": default["late_night_window"],
        "banned_items": [str(i).strip().lower()[:60] for i in items if str(i).strip()][:40],
        "rows": rows or copy.deepcopy(default["rows"]),
    }


# --- LLM path -----------------------------------------------------------------------
class _Row(BaseModel):
    category: str
    per_txn_limit: float = 0
    preapproval_above: float = 0
    receipt_required_above: float = 0
    banned: bool = False
    notes: str = ""


class _LLMPolicy(BaseModel):
    summary: str
    preapproval_threshold: float = 0
    receipt_required_above: float = 0
    banned_items: list[str] = Field(default_factory=list)
    rows: list[_Row] = Field(default_factory=list)


def _schema() -> dict:
    n, s = {"type": "number"}, {"type": "string"}
    row = {"type": "object", "additionalProperties": False,
           "properties": {"category": {"type": "string", "enum": list(load_taxonomy()["categories"])},
                          "per_txn_limit": n, "preapproval_above": n, "receipt_required_above": n,
                          "banned": {"type": "boolean"}, "notes": s},
           "required": ["category", "per_txn_limit", "preapproval_above", "receipt_required_above", "banned", "notes"]}
    return {"type": "object", "additionalProperties": False,
            "properties": {"summary": s, "preapproval_threshold": n, "receipt_required_above": n,
                           "banned_items": {"type": "array", "items": s}, "rows": {"type": "array", "items": row}},
            "required": ["summary", "preapproval_threshold", "receipt_required_above", "banned_items", "rows"]}


def _with_llm(provider, text: str) -> dict:
    data = provider.complete_json(prompts.POLICY_SYSTEM, f"<policy_document>\n{text}\n</policy_document>", _schema(), timeout=60)
    parsed = _LLMPolicy.model_validate(data)
    default_rows = {r["category"]: r for r in load_default_policy()["rows"]}
    merged = {**default_rows, **{r.category: r.model_dump() for r in parsed.rows}}   # categories the document skips keep defaults
    policy = sanitize_policy({**parsed.model_dump(), "rows": list(merged.values())})
    return {"summary": parsed.summary.strip(), "summary_engine": "llm", "policy": policy, "table_engine": "llm",
            "found": [f"{len(parsed.rows)} categories read by AI"]}


def analyze_policy(text: str) -> dict:
    """Summary + proposed policy table. Never raises: LLM first, then the local chain + regex."""
    note = ""
    if len(text) > MAX_POLICY_CHARS:
        text = text[:MAX_POLICY_CHARS]
        note = f"The document is very long; only the first {MAX_POLICY_CHARS:,} characters were analyzed."
    provider = ai_provider.get_provider()
    if provider is not None:
        try:
            return {**_with_llm(provider, text), "note": note}
        except ProviderError as exc:
            ai_provider.report_failure(exc)
        except Exception:
            pass
    policy = parse_policy_text(text)
    found = policy.pop("found")
    summary = summarizer.summarize(text)
    return {"summary": summary["summary"], "summary_engine": summary["engine"], "policy": sanitize_policy(policy),
            "table_engine": "rules", "found": found, "note": note}
