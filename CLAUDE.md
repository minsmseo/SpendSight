# SpendSight: AI expense auditor for US corporations, built on PayPal

> Build spec for Claude Code. Read this whole file before writing code.
> Hackathon: "Build What's Next with PayPal and AI" (Devpost).
> **Hard deadline: 2026-11-13 05:00 KST (= 2026-11-12 12:00 PT).** Aim to submit by 03:00 KST.
> Participant: **Minseo Kim** (minsmseo@gmail.com). Use this name in LICENSE, README and the footer.
> "SpendSight" is a working name. Keep it in one constant (`APP_NAME`) so it is easy to rename.

---

## 0. One-paragraph pitch

Companies hand employees spending power (and soon, AI purchasing agents). Finance teams then spend
hours checking every expense: was it really for work, does it break policy, and how much of it can
the company actually deduct on its US tax return? SpendSight runs on PayPal: purchases paid through
PayPal arrive as structured receipts automatically (no OCR), uploaded paper receipts are read by AI,
and an expense agent decides for each line whether it is **reimbursable under company policy** and
**how much is tax deductible under 2026 US rules** (for example 50% for client meals, 0% for office
snacks after the 2026 §274(o) change). Approved reimbursements go out through **PayPal Payouts**,
and the month-end package for the CPA is one click. Everything still works with **no AI key**: a
rule engine, local OCR and a local summarizer take over.

---

## 1. Non-negotiable requirements

1. **PayPal is central, not decorative.** Use at minimum: Orders v2 (create, capture, authorize/void),
   Payouts v1, Webhooks (with signature verification), Transaction Search v1 (backfill only).
   All in **sandbox**.
2. **AI is meaningful**: receipt understanding, business purpose judgment, tax treatment with
   reasoning, follow-up questions to the employee, policy document to rule table.
3. **Works without an AI key.** Every AI step has a deterministic fallback (section 6).
   If a key is present but fails (invalid, out of credit, rate limited, timeout), fall back
   automatically and keep going. Never crash, never block the user.
4. **Works without PayPal credentials** using a `MockPayPalClient` with the same interface,
   so a judge can run it locally in one command. Hosted demo uses real sandbox.
5. Every decision records **which engine produced it** (`llm` or `rules`) and shows it in the UI.
6. Public GitHub repo, **MIT license** (`Copyright (c) 2026 Minseo Kim`), clear README with run steps,
   hosted demo URL on Render.
7. Tax output is **decision support for a CPA**, not tax advice. Show a small disclaimer on the
   tax report and in the README.

---

## 2. Tech stack

| Layer | Choice | Why |
|---|---|---|
| Backend | Python 3.11, FastAPI, Uvicorn | fast to build |
| DB | SQLite via SQLModel | zero setup |
| Frontend | Jinja2 templates + Alpine.js + Tailwind (CDN) | no build step |
| Expense table | **AG Grid Community** (CDN) | sponsor prize "Best Use of AG Grid" |
| Charts | Chart.js (CDN) | SPC control charts |
| PayPal | REST via `httpx` (own thin client) + PayPal JS SDK for buttons | full control, easy to mock |
| LLM | Anthropic or OpenAI, chosen by which key is present | provider abstraction |
| OCR fallback | Tesseract (`pytesseract`) + regex parser | free, offline |
| Summarizer fallback | `sumy` TextRank (always) + optional local HF model | see 6.4 |
| Export | CSV + XLSX (`openpyxl`) | CPA package |
| Deploy | Docker on **Render** | sponsor prize "Best Use of Render" |

No auth system. Use a role switcher in the header (Employee view / Finance view) and seed users.

---

## 3. Repository layout

```
spendsight/
  app/
    main.py                 # FastAPI app, routers, startup seed
    config.py               # env loading, APP_NAME, feature flags
    db.py                   # SQLModel engine/session
    models.py               # tables (section 5)
    routers/
      onboarding.py         # company + industry + policy upload
      expenses.py           # submit, list, detail, follow-up answers
      paypal_checkout.py    # demo vendor store: create/capture/authorize/void
      webhooks.py           # PayPal webhook receiver + signature verify
      reimburse.py          # payouts batch + status
      reports.py            # tax report + exports
      settings.py           # AI key (session), provider status
    paypal/
      client.py             # PayPalClient (real) : token cache, orders, payouts, search, webhooks
      mock.py               # MockPayPalClient : same interface, in-memory
    ai/
      provider.py           # LLMProvider interface, Anthropic/OpenAI impls, failure detection
      agent.py              # ExpenseAgent: tool-calling loop
      prompts.py
    rules/
      engine.py             # deterministic decision pipeline (fallback and also guardrail)
      us_2026.json          # tax rule pack (section 7)
      industries.json       # industry profiles (section 8)
      taxonomy.json         # category keywords, merchant hints
      policy_parser.py      # policy text -> limits (regex)
    extract/
      paypal_receipt.py     # Orders v2 capture -> Receipt
      ocr.py                # Tesseract + regex parser
      vision.py             # LLM vision extraction
    anomaly/
      spc.py                # control charts + rules
    summarize/
      summarizer.py         # LLM -> local HF model -> TextRank
    templates/  static/
  demo/
    receipts/               # generated demo receipt images (PIL script)
    policy_acme.md          # sample expense policy
    make_receipts.py
  seed.py                   # 90 days of history
  tests/
  Dockerfile  render.yaml  requirements.txt  .env.example  README.md  LICENSE
```

---

## 4. Core user flows

### 4.1 Onboarding (Finance view)
1. Company name, **industry** (dropdown from `industries.json`), entity type fixed to **C corporation (Form 1120)**.
2. Optional: upload expense policy (PDF / DOCX / MD / TXT). System summarizes it and proposes a
   **policy rule table** (category, per-transaction limit, needs pre-approval above, banned items,
   receipt required above). Finance edits and confirms. If no upload, load a default policy.
3. Optional: paste an AI key (Anthropic or OpenAI). Stored **only in server memory for the session**,
   never written to disk or logs. Env var `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` also works.
4. Employees list (seeded): name, PayPal sandbox email (for payouts), department, default project.

### 4.2 Submit an expense (Employee view): three intake channels
Every channel ends in the same `Expense` record and the same review pipeline.

**A. Buy through PayPal (preferred, zero manual entry)**
- A small "Vendor Store" page sells demo items from a few demo vendors (Coffee & Co, CloudStack SaaS,
  SkyAir, Hotel Lumen, Office Depotish, Steakhouse 21, Stadium Tickets, Gift Box Co, Fitness Club).
- Employee picks items, writes the **business purpose** (required) and optional attendees, then pays
  with the PayPal JS SDK button (sandbox buyer account).
- Server creates the order with **itemized `purchase_units[].items`** (name, unit_amount, quantity,
  `category`), `custom_id = expense draft id`, `invoice_id`, and `description = vendor name`.
- On capture, `extract/paypal_receipt.py` builds the Receipt directly from the order:
  vendor, date, total, tax, line items, PayPal capture id. **No OCR needed.** Mark `source=paypal`.
- **Pre-approval mode**: if cart total is above the policy pre-approval threshold, create the order
  with `intent=AUTHORIZE`. Agent reviews first. Approve -> capture the authorization; reject -> void it.
  The UI shows "Held by SpendSight, waiting for review". This is the strongest PayPal moment in the demo.

**B. Upload a receipt** (image or PDF) + business purpose + attendees. Extraction order:
LLM vision -> Tesseract OCR + regex -> manual form prefilled with whatever was found.

**C. Import PayPal history** (Finance view): Transaction Search for a date range (max 31 days per call).
Note in UI that sandbox data can take up to 3 hours or more to appear. Imported rows have no purpose
yet, so they enter the queue as `needs_info` and the agent asks the employee for the purpose.

### 4.3 Review (automatic, then Finance)
Pipeline per expense (section 6 explains AI vs fallback for each step):
1. **Normalize** receipt (vendor, date, currency USD, total, tax, items).
2. **Categorize** each line (taxonomy category).
3. **Business purpose check**: does the purpose fit the line items, the vendor, the employee's role,
   and the company industry? Output `fit_score 0..1` and a one-sentence reason.
4. **Policy check** against the confirmed policy table (limits, banned, pre-approval, weekend/late night).
5. **Tax treatment** per line from `us_2026.json` + industry overrides: deductible %, deductible amount,
   Form 1120 line, rule citation, substantiation elements still missing.
6. **Anomaly check** (section 9).
7. **Decision**: one of
   - `approved` (reimbursable; tax treatment attached)
   - `needs_info` (agent asks a specific question, e.g. "Who attended and what is their business relationship?")
   - `flagged` (policy breach or anomaly; Finance must decide)
   - `rejected` (personal or banned)

**Two separate axes are always shown:** *Reimbursable under policy?* and *Deductible for tax, and how much?*
Example: client dinner $180 -> reimbursable yes, deductible 50% = $90.
Example: office snacks for staff -> reimbursable yes, deductible 0% (2026 §274(o)) unless industry is restaurant.

### 4.4 Follow-up conversation
When `needs_info`, the employee sees the agent's question on the expense card and answers in a text box.
The answer is appended to the expense and the pipeline reruns. Max 2 rounds, then `flagged`.

### 4.5 Reimburse (Finance view)
Select approved, unpaid expenses in AG Grid -> "Pay via PayPal" -> one **Payouts** batch,
one item per employee (sum of their approved expenses), `sender_item_id = expense ids`.
Track batch and item status by webhook (`PAYMENT.PAYOUTS-ITEM.SUCCEEDED` etc.) and by polling
`GET /v1/payments/payouts/{batch_id}` as backup. Show status chips.
Also enforce the **accountable plan** timing check: expenses substantiated more than 60 days after
purchase are flagged as possibly taxable to the employee (Reg. §1.62-2).

For purchases made in channel A, the company already paid nothing to the employee (employee paid the vendor),
so the reimbursement covers them too. Keep it simple: every approved expense paid by an employee is reimbursable.

### 4.6 Tax report (Finance view)
- Period picker (month / quarter / YTD).
- Table by category and by Form 1120 line: total spent, deductible amount, nondeductible amount.
- "Missing substantiation" list (receipt missing for lodging or for any expense of $75 or more,
  missing attendees for meals, missing purpose).
- Exports: `cpa_package.xlsx` (sheets: Summary, Lines, Missing, Decisions with reasons and engine),
  and CSV.
- Disclaimer: "Prepared to support review by a qualified tax professional. Not tax advice."

---

## 5. Data model (SQLModel)

```
Company(id, name, industry_id, entity_type="C_CORP", tax_year=2026, policy_json, policy_summary, created_at)
Employee(id, name, email, paypal_email, department, role_title)
Expense(id, employee_id, source[paypal|upload|import], status[draft|held|needs_info|flagged|approved|rejected|paid],
        vendor, purchased_at, currency, total, tax_amount, purpose, attendees, receipt_path,
        paypal_order_id, paypal_capture_id, paypal_authorization_id, intent[CAPTURE|AUTHORIZE],
        created_at, substantiated_at)
LineItem(id, expense_id, name, quantity, unit_amount, amount, category, deductible_pct, deductible_amount,
         form1120_line, rule_id)
Decision(id, expense_id, engine[llm|rules], reimbursable, decision, fit_score, reasons_json,
         missing_json, anomaly_json, question, created_at)
FollowUp(id, expense_id, question, answer, created_at)
PayoutBatch(id, paypal_batch_id, status, total, created_at)
PayoutItem(id, batch_id, employee_id, paypal_item_id, amount, status, expense_ids_json)
WebhookEvent(id, event_id, event_type, verified, payload_json, received_at)   # idempotency on event_id
```

---

## 6. AI layer and fallbacks

### 6.1 Provider abstraction (`ai/provider.py`)
```python
class LLMProvider(Protocol):
    name: str
    def complete_json(self, system: str, user: str | list, schema: dict, timeout: float = 30) -> dict: ...
    def chat_with_tools(self, messages, tools, timeout: float = 45) -> ToolTurn: ...
    def vision_json(self, image_bytes: bytes, mime: str, prompt: str, schema: dict) -> dict: ...
```
- `get_provider(session)` returns Anthropic if an Anthropic key is present, else OpenAI, else `None`.
- Model ids come from env (`ANTHROPIC_MODEL`, `OPENAI_MODEL`). Pick sensible current defaults and
  verify the ids against the provider docs at build time.
- **Failure handling**: catch auth errors (401/403), credit or quota errors (Anthropic "credit balance is too low",
  OpenAI `insufficient_quota`, HTTP 402/429), timeouts, and invalid JSON (retry once with a repair prompt).
  On credit/auth errors mark the provider `degraded` for 10 minutes and use rules. Surface a banner:
  "AI unavailable (reason). Running on rule engine."
- Validate every LLM JSON against a Pydantic model. Invalid after retry -> rules.
- **Guardrail**: the rule engine always runs too. If the LLM says deductible % differs from the rule pack
  for a category the rule pack is certain about (meals, entertainment, gifts, convenience meals, fines,
  political), use the rule pack value and note the conflict. The LLM decides *which category and why*;
  the rule pack decides *the percentage*.

### 6.2 Expense agent (`ai/agent.py`)
Tool-calling loop, max 6 tool calls per expense. Tools:
- `get_company_profile()` -> industry, policy table
- `get_expense(expense_id)` -> receipt, items, purpose, attendees, follow-ups
- `get_paypal_order(order_id)` -> live order details from PayPal
- `lookup_tax_rule(category, context)` -> rule pack entry with citation
- `get_employee_history(employee_id, category)` -> stats for anomaly context
- `ask_employee(question)` -> sets `needs_info` with that question (ends loop)
- `submit_decision(json)` -> final decision object (ends loop)

System prompt essentials: you are a careful expense auditor for a US C corporation in industry X;
judge business purpose under "ordinary and necessary" (IRC §162); never invent receipt facts;
ask instead of guessing when attendees or purpose are missing for meals, gifts or travel;
keep each reason to one plain sentence; output via `submit_decision` only.

### 6.3 Receipt extraction fallback (`extract/ocr.py`)
- Preprocess with Pillow (grayscale, threshold, upscale small images). PDF: render first page with `pypdfium2`.
- `pytesseract.image_to_string`.
- Regex parser: total (`TOTAL|AMOUNT DUE|BALANCE` lines, take the largest money value near them),
  tax (`TAX|SALES TAX`), date (several US formats), vendor (first non-empty uppercase-ish line),
  items (lines matching `name ... $12.34`).
- Return confidence per field. Low confidence fields are highlighted in the prefilled form.

### 6.4 Policy document summary and rule extraction (`summarize/`, `rules/policy_parser.py`)
Order of engines:
1. LLM: summary + structured policy table in one JSON call.
2. **Local summarization model** (optional, `ENABLE_LOCAL_SUMMARIZER=1`): Hugging Face `transformers`
   pipeline with a small summarization model such as `Falconsai/text_summarization` (t5-small based, light)
   or `sshleifer/distilbart-cnn-12-6` (better, heavier). Check the model still exists on the Hub at build time
   and pick the lighter one if Render memory is tight. Chunk long text (about 400 words per chunk), summarize
   chunks, then summarize the joined result.
3. Always available: extractive TextRank with `sumy` (pure Python), top 6 to 8 sentences.

Rule extraction without AI is regex on the raw text, independent of the summary:
money amounts near category keywords (meal, dinner, hotel, lodging, airfare, gift, software, per day, per night),
phrases like "requires pre-approval", "not reimbursable", "prohibited", "receipt required". Produce the same
policy table schema; Finance confirms it in an editable grid.

Keep `transformers` and `torch` **out of the default requirements** (put them in `requirements-local-ml.txt`)
so the Docker image stays small on Render.

### 6.5 Business purpose fallback (`rules/engine.py`)
- Category from item names and vendor via `taxonomy.json` keywords (+ `rapidfuzz` fuzzy match).
- Purpose intent from keywords: client/customer/prospect/partner -> client meeting; conference/trip/travel/onsite
  -> travel; team/offsite/holiday party/all-hands -> company event; snacks/coffee for office -> office provisions;
  gift -> gift; personal words (gym, birthday for self, family, groceries for home) -> personal.
- Fit score = weighted: category allowed by industry profile (0.4), purpose intent matches category (0.4),
  amount within policy (0.2). Below 0.4 -> flagged, 0.4 to 0.7 -> needs_info with a template question.
- Template questions per gap: meals without attendees, gift without recipient, travel without destination
  or dates, any expense with an empty or vague purpose (fewer than 4 words).

---

## 7. US 2026 tax rule pack (`rules/us_2026.json`)

Each entry: `id, category, deductible_pct, form1120_line, citation, substantiation[], notes, confidence`.
These are the defaults for a C corporation, tax year 2026. Keep the file data-driven and add a
`verify_with_cpa: true` flag on everything.

| id | Category / situation | Deductible | Notes and citation |
|---|---|---|---|
| meal_client | Meal with client, prospect or partner, business discussed, not lavish | 50% | IRC §274(k), §274(n). Needs attendees and business relationship |
| meal_travel | Employee meals while traveling away from home overnight | 50% | §274(n); per diem allowed as alternative |
| meal_convenience | Office snacks, coffee, on-site meals, overtime meals for convenience of employer | **0% from 2026** | §274(o) as amended. Industry override: restaurants feeding their own staff stay deductible |
| event_all_staff | Holiday party, team event open to all employees | 100% | §274(e)(4) |
| entertainment | Tickets, golf, club dues, shows (even with clients) | 0% | §274(a). Food invoiced separately at the event may be 50% |
| gift | Business gifts | up to $25 per recipient per year | §274(b). Needs recipient name |
| travel_transport | Airfare, train, rideshare, rental car on business travel | 100% | §162. Commuting is never deductible |
| lodging | Hotel on business travel | 100% | Receipt required at any amount |
| software | SaaS subscriptions, software | 100% | §162 |
| office_supplies | Office supplies, small equipment | 100% if per-item cost is within de minimis safe harbor ($2,500 per item or invoice without AFS) | Reg. §1.263(a)-1(f). Above that: capitalize, flag for CPA (bonus depreciation or §179 may apply) |
| advertising | Ads, marketing | 100% | |
| fines_penalties | Fines, penalties | 0% | §162(f) |
| political | Political contributions, lobbying | 0% | §162(e) |
| personal | Gym, personal clothing, groceries for home, commuting | 0%, not reimbursable | |

Global substantiation rules (used for the "Missing" list):
- Receipt required for lodging at any amount and for any other expense of **$75 or more** (Reg. §1.274-5(c)(2)(iii)).
- Record amount, date, place, business purpose and, for meals and gifts, who and business relationship (IRS Pub. 463).
- Accountable plan: substantiate within 60 days, return excess within 120 days (Reg. §1.62-2(g)).

Form 1120 line mapping: put it in the JSON and keep it simple (most of these roll into line 26
"Other deductions" with a supporting statement; advertising to its own line). Mark every mapping `verify_with_cpa`.
Do not hardcode tax logic in Python; the engine reads this file so a Korean pack (`kr_2026.json`) can be added later.

---

## 8. Industry profiles (`rules/industries.json`)

Each profile: `id, label, naics_hint, common_categories[], unusual_categories[], overrides[]`.
Seed five:
- `software_saas`: common software, travel, meals, advertising; unusual fuel, heavy equipment.
- `consulting`: common travel, lodging, client meals; unusual inventory purchases.
- `restaurant`: override `meal_convenience` -> deductible for meals provided to own staff (OBBBA restaurant exception);
  common food supplies.
- `ecommerce_retail`: common shipping, packaging, advertising, inventory.
- `construction`: common tools, fuel, equipment, safety gear; unusual SaaS at high amounts.

"Unusual for this industry" lowers the fit score and is mentioned in the reason. The demo shows the
same office-lunch receipt judged differently for `software_saas` (0%) vs `restaurant` (deductible).

---

## 9. Anomaly detection (`anomaly/spc.py`)

Per employee x category, using the seeded 90-day history:
- Individual chart: mean and sigma of transaction amounts (min 8 points; else use company-wide category stats).
- Rules: point above mean + 3 sigma; 2 of 3 consecutive above mean + 2 sigma; 6 increasing in a row (trend).
- **Split purchase**: same employee, same vendor, same day, each under the policy limit but the sum above it.
- **Duplicate**: same amount and vendor within 7 days, or same PayPal capture id / same receipt image hash.
- Weekend or 22:00 to 05:00 purchases in categories that are not travel.
- Round-number amounts above $200 with no itemization.
Return a list of `{rule, severity, explanation}`. Show the control chart (Chart.js) on the expense detail page
with the current point highlighted.

---

## 10. PayPal integration details (`paypal/client.py`)

- Base URL `https://api-m.sandbox.paypal.com`. OAuth2 client credentials, cache token until expiry.
- Orders v2: `POST /v2/checkout/orders` (intent CAPTURE or AUTHORIZE, itemized, `custom_id`),
  `POST /v2/checkout/orders/{id}/capture`, `POST /v2/checkout/orders/{id}/authorize`,
  `POST /v2/payments/authorizations/{id}/capture`, `POST /v2/payments/authorizations/{id}/void`,
  `GET /v2/checkout/orders/{id}`.
- Payouts: `POST /v1/payments/payouts` (`recipient_type=EMAIL`, unique `sender_batch_id`),
  `GET /v1/payments/payouts/{batch_id}`.
- Transaction Search: `GET /v1/reporting/transactions` with `start_date`, `end_date` (max 31 days), `fields=all`.
  Needs "Transaction Search" enabled in the sandbox app settings. Data can lag 3 hours or more, so it is
  backfill only; the live flow never depends on it.
- Webhooks: `POST /webhooks/paypal`. Verify with `POST /v1/notifications/verify-webhook-signature`
  using `PAYPAL_WEBHOOK_ID`. Store events idempotently. Subscribe to: `CHECKOUT.ORDER.APPROVED`,
  `PAYMENT.CAPTURE.COMPLETED`, `PAYMENT.AUTHORIZATION.CREATED`, `PAYMENT.AUTHORIZATION.VOIDED`,
  `PAYMENT.PAYOUTSBATCH.SUCCESS`, `PAYMENT.PAYOUTSBATCH.DENIED`, `PAYMENT.PAYOUTS-ITEM.SUCCEEDED`,
  `PAYMENT.PAYOUTS-ITEM.FAILED`, `PAYMENT.PAYOUTS-ITEM.UNCLAIMED`.
  Locally (no public URL) everything still works because capture/payout responses are handled synchronously
  and payout status is polled.
- Frontend: PayPal JS SDK with `client-id` from `/config` endpoint, `intent` set per cart.
- `MockPayPalClient`: same methods, returns realistic fake ids and statuses, simulates payout success after 3 seconds.
  Selected automatically when `PAYPAL_CLIENT_ID` is empty. In mock mode the checkout page shows a
  "Simulate PayPal payment" button instead of the SDK button. UI badge: "PayPal: sandbox" or "PayPal: mock".

---

## 11. Screens

1. **Onboarding**: company, industry cards, policy upload with summary + editable policy grid, AI key field
   with live status (Connected / Not set / Degraded: reason).
2. **Employee home**: "Buy through PayPal" (Vendor Store), "Upload receipt", my expenses with status and agent questions.
3. **Vendor Store**: vendors, items, cart, purpose and attendees fields, PayPal button. Pre-approval notice when above threshold.
4. **Finance queue**: AG Grid with columns status chip, employee, vendor, date, total, reimbursable,
   deductible amount, engine badge, anomaly icon, source icon (PayPal / receipt / import).
   Filters by status, grouping by employee or category, row selection for payouts, inline edit of category.
5. **Expense detail**: receipt (image or PayPal itemized receipt), extracted fields with confidence,
   decision card (two axes), one-line reasons, citations, missing substantiation, follow-up thread,
   control chart, actions: approve / reject / ask / capture or void authorization.
6. **Reimburse**: selected expenses grouped by employee, total, "Send PayPal Payouts", live status.
7. **Tax report**: summary tiles (spent, deductible, nondeductible, missing docs), table by category and Form 1120 line,
   exports, disclaimer.
8. **Settings**: AI provider and model, test button, PayPal mode, local summarizer flag.

Design: clean, finance-tool look, light theme, one accent color, generous spacing. Every AI output shows
the engine badge and a "why" line. Keep text in English (judges are global).

---

## 12. Demo data and scenario (for the 3-minute video)

`seed.py`: company "Acme Analytics Inc." (`software_saas`), 3 employees, 90 days of normal expenses
(about 150 rows) so SPC has history. `demo/make_receipts.py` renders receipt images with Pillow.

Live demo cases (each must produce the stated result with **and** without an AI key):
1. **Client dinner via PayPal** $186 at Steakhouse 21, purpose "Dinner with Globex CTO to discuss renewal",
   attendees given -> approved, deductible 50% ($93).
2. **Same dinner without attendees** -> needs_info, question about attendees and relationship; answer -> approved.
3. **Office snacks receipt upload** $64 -> reimbursable, deductible 0% (2026 §274(o)); switch industry to
   restaurant -> deductible.
4. **Stadium tickets "with client"** $420 -> reimbursable per policy maybe, deductible 0% (entertainment).
5. **Split purchase**: two $240 laptop accessory orders same day, same vendor, policy limit $300 -> flagged.
6. **High-value hotel** $1,150 via PayPal with `intent=AUTHORIZE` -> held, Finance approves -> authorization captured.
7. **Gym membership** "team wellness" -> rejected, personal.
8. Approve all valid -> **Payouts batch** -> statuses turn to Succeeded.
9. Tax report -> export CPA package.

Video outline (under 3 minutes): problem (15s), onboarding with industry and policy (25s), PayPal purchase
with automatic receipt (30s), agent follow-up question (20s), held authorization approve and capture (20s),
office snacks 0% vs restaurant override (20s), split purchase flag with control chart (15s),
Payouts (20s), tax report export (15s), architecture and no-key fallback (10s).

---

## 13. Config (`.env.example`)

```
APP_NAME=SpendSight
PAYPAL_CLIENT_ID=
PAYPAL_CLIENT_SECRET=
PAYPAL_WEBHOOK_ID=
PAYPAL_ENV=sandbox
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=
OPENAI_API_KEY=
OPENAI_MODEL=
ENABLE_LOCAL_SUMMARIZER=0
DATABASE_URL=sqlite:///./spendsight.db
```
Never log keys. Redact `Authorization` headers in logs.

---

## 14. Build order (with time boxes, KST)

| # | Until | Task | Done when |
|---|---|---|---|
| 1 | 11-11 22:00 | Skeleton, models, seed, rule pack JSON, industries, taxonomy | `uvicorn` runs, seeded DB |
| 2 | 11-12 00:00 | Rule engine end to end (no AI) + tests for the 9 demo cases | `pytest` green |
| 3 | 11-12 02:00 | PayPal client + mock, Vendor Store, capture -> receipt, authorize/capture/void | sandbox order shows up as expense |
| 4 | 11-12 04:00 | Finance queue (AG Grid), expense detail, follow-ups | full review loop in UI |
| 5 | 11-12 09:00 | LLM provider, agent, vision extraction, fallback switching | demo cases pass with key; kill key mid-run and still pass |
| 6 | 11-12 12:00 | OCR fallback, policy upload + summarizer + regex rules | upload policy and receipt without key works |
| 7 | 11-12 15:00 | Payouts + webhooks + status | batch succeeds in sandbox |
| 8 | 11-12 18:00 | Tax report + XLSX export, SPC chart, polish UI | |
| 9 | 11-12 21:00 | Docker, Render deploy, webhook URL registered, README, LICENSE | hosted URL works |
| 10 | 11-13 01:00 | Record video, upload to YouTube (public), Devpost text | |
| 11 | 11-13 03:00 | Submit on Devpost | before 05:00 hard deadline |

If behind schedule, cut in this order: Transaction Search import, local HF summarizer (keep TextRank),
SPC chart (keep rules), Korean pack mention.

---

## 15. Tests (minimum)

- Rule engine: each of the 9 demo cases -> expected decision, deductible %, question.
- Industry override: office snacks under `software_saas` vs `restaurant`.
- Provider failure: mock provider raising credit error -> pipeline returns `engine=rules`, no exception.
- PayPal mock: order create/capture/authorize/void, payouts batch lifecycle, webhook idempotency.
- OCR parser: 3 demo receipt images -> total and date parsed.

---

## 16. README must include

Title and one-line pitch, demo video link, hosted URL, screenshot, problem and who it is for,
how PayPal is used (list of APIs and where), how AI is used (agent, vision, summarizer, fallbacks),
architecture diagram (Mermaid), quick start (Docker one-liner and local), sandbox setup steps
(create app, enable Payouts and Transaction Search, create buyer and employee sandbox accounts,
set webhook), running without keys, tax disclaimer, sponsor tools used (AG Grid, Render), license, author:
Minseo Kim (minsmseo@gmail.com).
