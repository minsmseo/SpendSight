# SpendSight

AI expense auditor for US corporations, built on PayPal. For every expense it answers two questions:
**is it reimbursable under company policy**, and **how much is tax deductible under 2026 US rules**.

> Status: skeleton (build step 1). Demo video, hosted URL, screenshot and architecture diagram are added before submission.

## Quick start (no keys needed)

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # macOS/Linux: .venv/bin/python
.venv/Scripts/python -m uvicorn app.main:app --reload
```

Open http://localhost:8000. With no `.env`, PayPal runs on an in-memory mock and all decisions come
from the rule engine. The database is seeded on first start (Acme Analytics Inc., 3 employees, 90 days of history).

Docker:

```bash
docker build -t spendsight . && docker run -p 8000:8000 spendsight
```

Tests:

```bash
.venv/Scripts/python -m pytest -q
```

## Configuration

Copy `.env.example` to `.env`. Set `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` to use the PayPal sandbox,
and `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` to enable the AI agent. Everything keeps working without them.

## Layout

| Path | What |
|---|---|
| `app/main.py` | FastAPI app, routers, startup seed |
| `app/models.py` | SQLModel tables |
| `app/rules/` | Tax rule pack (`us_2026.json`), industries, taxonomy, default policy, rule engine |
| `app/paypal/` | Thin REST client (Orders v2, Payouts, Transaction Search, Webhooks) and mock |
| `app/ai/` | Provider abstraction, expense agent, prompts |
| `app/extract/` | PayPal order to receipt, OCR, vision |
| `app/anomaly/`, `app/summarize/` | SPC rules, policy summarizer |
| `seed.py`, `demo/` | Demo history, sample policy, receipt generator |

## Disclaimer

Prepared to support review by a qualified tax professional. Not tax advice.

## License and author

MIT. Copyright (c) 2026 Minseo Kim (minsmseo@gmail.com).
