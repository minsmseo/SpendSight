"""SQLModel tables (CLAUDE.md section 5). JSON payloads are stored as TEXT in *_json columns."""
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """Timezone-aware UTC. SQLModel stores and returns all datetimes as aware UTC; never pass naive ones."""
    return datetime.now(timezone.utc)


# String "enums" kept as constants so they are easy to use in templates and JSON.
SOURCES = ("paypal", "upload", "import")
STATUSES = ("draft", "held", "needs_info", "flagged", "approved", "rejected", "paid")
DECISIONS = ("approved", "needs_info", "flagged", "rejected")
ENGINES = ("llm", "rules")
INTENTS = ("CAPTURE", "AUTHORIZE")


class Company(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    industry_id: str = "software_saas"
    entity_type: str = "C_CORP"
    tax_year: int = 2026
    policy_json: str = "{}"
    policy_summary: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class Employee(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    email: str
    paypal_email: str
    department: str = ""
    role_title: str = ""
    default_project: str = ""


class Expense(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    employee_id: int = Field(foreign_key="employee.id", index=True)
    source: str = "upload"                     # paypal | upload | import
    status: str = Field(default="draft", index=True)
    vendor: str = ""
    purchased_at: Optional[datetime] = Field(default=None, index=True)
    currency: str = "USD"
    total: float = 0.0
    tax_amount: float = 0.0
    purpose: str = ""
    attendees: str = ""
    receipt_path: Optional[str] = None
    receipt_hash: Optional[str] = None         # duplicate detection for uploads
    paypal_order_id: Optional[str] = Field(default=None, index=True)
    paypal_capture_id: Optional[str] = None
    paypal_authorization_id: Optional[str] = None
    intent: str = "CAPTURE"                    # CAPTURE | AUTHORIZE
    followup_rounds: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    substantiated_at: Optional[datetime] = None


class LineItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    expense_id: int = Field(foreign_key="expense.id", index=True)
    name: str
    quantity: float = 1
    unit_amount: float = 0.0
    amount: float = 0.0
    category: str = ""
    deductible_pct: float = 0.0
    deductible_amount: float = 0.0
    form1120_line: str = ""
    rule_id: str = ""


class Decision(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    expense_id: int = Field(foreign_key="expense.id", index=True)
    engine: str = "rules"                      # llm | rules
    reimbursable: bool = False
    decision: str = "flagged"                  # approved | needs_info | flagged | rejected
    fit_score: float = 0.0
    reasons_json: str = "[]"
    missing_json: str = "[]"
    anomaly_json: str = "[]"
    question: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


class FollowUp(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    expense_id: int = Field(foreign_key="expense.id", index=True)
    question: str
    answer: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


class PayoutBatch(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    paypal_batch_id: str = Field(index=True)
    status: str = "PENDING"
    total: float = 0.0
    created_at: datetime = Field(default_factory=utcnow)


class PayoutItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    batch_id: int = Field(foreign_key="payoutbatch.id", index=True)
    employee_id: int = Field(foreign_key="employee.id")
    paypal_item_id: Optional[str] = None
    amount: float = 0.0
    status: str = "PENDING"
    expense_ids_json: str = "[]"


class WebhookEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: str = Field(index=True, unique=True)   # idempotency key
    event_type: str = ""
    verified: bool = False
    payload_json: str = "{}"
    received_at: datetime = Field(default_factory=utcnow)


class TaxRuleOverride(SQLModel, table=True):
    """A deductible rate the company's CPA set in place of the reviewed default in the rule pack."""

    id: Optional[int] = Field(default=None, primary_key=True)
    rule_id: str = Field(index=True, unique=True)
    deductible_pct: float = 0.0
    note: str = ""
    updated_at: datetime = Field(default_factory=utcnow)


class CategoryOverride(SQLModel, table=True):
    """Company-specific keywords (and the tax rule they map to) for one expense category."""

    id: Optional[int] = Field(default=None, primary_key=True)
    category: str = Field(index=True, unique=True)
    keywords_json: str = "[]"
    default_rule: str = ""
    updated_at: datetime = Field(default_factory=utcnow)
