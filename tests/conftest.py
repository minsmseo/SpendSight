import os
import tempfile

# Must run before any `app.*` import: isolated DB, no PayPal creds (-> mock), no AI keys (-> rules).
_tmp = tempfile.mkdtemp(prefix="spendsight-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db".replace("\\", "/")
for _k in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    os.environ[_k] = ""
