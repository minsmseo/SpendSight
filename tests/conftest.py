import os
import tempfile

# Must run before any `app.*` import: isolated DB, no PayPal creds (-> mock), no AI keys (-> rules).
_tmp = tempfile.mkdtemp(prefix="spendsight-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db".replace("\\", "/")
for _k in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_WEBHOOK_ID", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
           "ANTHROPIC_MODEL", "OPENAI_MODEL", "COMPANY_TZ"):      # a local .env must never change test results
    os.environ[_k] = ""
