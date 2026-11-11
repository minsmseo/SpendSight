"""Environment loading and feature flags. Never log secrets from here."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent          # app/
ROOT_DIR = BASE_DIR.parent                          # repo root
RULES_DIR = BASE_DIR / "rules"
UPLOAD_DIR = ROOT_DIR / "uploads"

APP_NAME = os.getenv("APP_NAME", "SpendSight")
AUTHOR = "Minseo Kim"
TAX_YEAR = 2026
TAX_DISCLAIMER = "Prepared to support review by a qualified tax professional. Not tax advice."

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./spendsight.db")

PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "").strip()
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET", "").strip()
PAYPAL_WEBHOOK_ID = os.getenv("PAYPAL_WEBHOOK_ID", "").strip()
PAYPAL_ENV = os.getenv("PAYPAL_ENV", "sandbox").strip() or "sandbox"
PAYPAL_BASE_URL = (
    "https://api-m.paypal.com" if PAYPAL_ENV == "live" else "https://api-m.sandbox.paypal.com"
)
PAYPAL_MOCK = not (PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET)
PAYPAL_MODE_LABEL = "mock" if PAYPAL_MOCK else PAYPAL_ENV

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
# TODO(step 5): verify model ids against provider docs before the demo.
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "").strip() or "claude-sonnet-5-5"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "").strip() or "gpt-4o-mini"

ENABLE_LOCAL_SUMMARIZER = os.getenv("ENABLE_LOCAL_SUMMARIZER", "0") == "1"

MAX_FOLLOWUP_ROUNDS = 2
AGENT_MAX_TOOL_CALLS = 6
PROVIDER_DEGRADED_SECONDS = 600
