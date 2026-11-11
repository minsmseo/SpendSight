"""Record the demo video and the gallery screenshots by driving the real app in a browser.

    pip install playwright && python -m playwright install ffmpeg
    python demo/record_demo.py                 # video + screenshots into submission/
    python demo/record_demo.py --shots-only

Point DEMO_URL at a freshly seeded instance (default http://localhost:18001), for example
`docker run --rm -p 18001:8000 -e PORT=8000 spendsight`. The script uses the "Simulate PayPal
payment" button, so the instance must run in mock PayPal mode; with sandbox credentials the buyer
has to approve in the PayPal window by hand.
"""
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

BASE = os.environ.get("DEMO_URL", "http://localhost:18001")
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "submission"
CHANNEL = os.environ.get("DEMO_BROWSER", "msedge")        # an installed browser: msedge or chrome
VIDEO = {"width": 1920, "height": 1080}
SHOT = {"width": 1500, "height": 1000}                      # 3:2 for the Devpost gallery

DINNER_PURPOSE = "Dinner with Globex CTO to discuss renewal"
PACE = float(os.environ.get("DEMO_PACE", "0.84"))          # scales every caption hold; keep the video under 3:00

# Caption bar + visible cursor, re-created on every page load (state lives in sessionStorage).
OVERLAY_JS = """
(() => {
  const mount = () => {
    if (document.getElementById('demo-caption')) return;
    const bar = document.createElement('div');
    bar.id = 'demo-caption';
    bar.style.cssText = 'position:fixed;left:50%;bottom:34px;transform:translateX(-50%);max-width:1500px;z-index:2147483647;' +
      'background:rgba(11,18,32,.94);color:#fff;font:500 25px/1.4 Inter,Segoe UI,system-ui,sans-serif;padding:15px 28px;' +
      'border-radius:10px;box-shadow:0 10px 34px rgba(0,0,0,.28);text-align:center;pointer-events:none;letter-spacing:-.005em;';
    const store = (() => { try { return window.sessionStorage; } catch (e) { return null; } })();   // title cards have no storage
    const text = store ? (store.getItem('demoCaption') || '') : '';
    bar.textContent = text; bar.style.display = text ? 'block' : 'none';
    const dot = document.createElement('div');
    dot.id = 'demo-cursor';
    dot.style.cssText = 'position:fixed;width:26px;height:26px;margin:-13px 0 0 -13px;border-radius:50%;z-index:2147483647;' +
      'background:rgba(14,102,85,.30);border:2px solid #0E6655;pointer-events:none;transition:transform .08s;';
    const pos = JSON.parse((store && store.getItem('demoCursor')) || '[960,540]');
    if (!store) dot.style.display = 'none';
    dot.style.left = pos[0] + 'px'; dot.style.top = pos[1] + 'px';
    document.documentElement.append(bar, dot);
    document.addEventListener('mousemove', e => {
      dot.style.left = e.clientX + 'px'; dot.style.top = e.clientY + 'px';
      if (store) store.setItem('demoCursor', JSON.stringify([e.clientX, e.clientY]));
    }, true);
    document.addEventListener('mousedown', () => { dot.style.transform = 'scale(.7)'; }, true);
    document.addEventListener('mouseup', () => { dot.style.transform = 'scale(1)'; }, true);
  };
  if (document.documentElement) mount();
  document.addEventListener('DOMContentLoaded', mount);
})();
"""

CARD = """<html><body style="margin:0;height:100vh;display:flex;flex-direction:column;justify-content:center;
background:#0B1220;color:#fff;font-family:Inter,Segoe UI,system-ui,sans-serif;padding:0 190px;box-sizing:border-box">
<div style="font-size:19px;letter-spacing:.14em;text-transform:uppercase;color:#78B8A7;margin-bottom:22px">{eyebrow}</div>
<div style="font-size:70px;font-weight:600;letter-spacing:-.025em;line-height:1.1;margin-bottom:34px">{title}</div>
{lines}</body></html>"""


class Demo:
    def __init__(self, page: Page):
        self.page = page

    # --- helpers ---
    def wait(self, seconds: float) -> None:
        self.page.wait_for_timeout(int(seconds * 1000))

    def say(self, text: str, hold: float = 0.0) -> None:
        self.page.evaluate(
            """t => { try { sessionStorage.setItem('demoCaption', t); } catch (e) {} const b = document.getElementById('demo-caption');
                      if (b) { b.textContent = t; b.style.display = t ? 'block' : 'none'; } }""", text)
        if hold:
            self.wait(hold * PACE)

    def go(self, path: str) -> None:
        for attempt in range(3):
            try:
                self.page.goto(BASE + path, wait_until="networkidle")
                return
            except Exception:                              # a page's own reload can abort the navigation
                if attempt == 2:
                    raise
                self.wait(0.8)

    def click(self, locator, pause: float = 0.35) -> None:
        locator = locator.first
        locator.scroll_into_view_if_needed()
        box = locator.bounding_box()
        self.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=22)
        self.wait(0.15)
        locator.click()
        self.wait(pause)

    def type(self, locator, text: str) -> None:
        self.click(locator, pause=0.1)
        self.page.keyboard.type(text, delay=14)
        self.wait(0.25)

    def scroll(self, pixels: int) -> None:
        step = 70 if pixels > 0 else -70
        for _ in range(abs(pixels) // 70):
            self.page.mouse.wheel(0, step)
            self.wait(0.02)
        self.wait(0.3)

    def card(self, eyebrow: str, title: str, lines: list[str], hold: float) -> None:
        html = "".join(f'<div style="font-size:31px;line-height:1.5;color:#C9D1DE;margin-bottom:8px">{ln}</div>' for ln in lines)
        self.page.set_content(CARD.format(eyebrow=eyebrow, title=title, lines=html))
        self.say("")
        self.wait(hold * PACE)

    def api(self, path: str, data: dict | None = None) -> dict:
        return self.page.request.post(BASE + path, data=data or {}).json()

    def buy_quietly(self, cart: dict) -> dict:
        order = self.api("/api/paypal/orders", cart)
        return self.api(f"/api/paypal/orders/{order['order_id']}/complete")

    def expense_id(self) -> int:
        return int(self.page.url.rstrip("/").rsplit("/", 1)[1])

    def add_item(self, name: str) -> None:
        self.click(self.page.locator("li", has_text=name).get_by_role("button", name="Add"))

    def pay(self) -> int:
        self.click(self.page.get_by_role("button", name="Simulate PayPal payment"), pause=0)
        self.page.wait_for_url("**/expenses/*", wait_until="networkidle")
        return self.expense_id()

    def status(self, expense_id: int) -> str:
        rows = self.page.request.get(BASE + "/api/expenses").json()
        return next(r["status"] for r in rows if r["id"] == expense_id)

    # --- the story ---
    def run(self) -> None:
        p = self.page
        self.card("Build What's Next with PayPal and AI", "SpendSight",
                  ["AI expense auditor for US corporations, built on PayPal."], 4)
        self.card("The problem", "Two questions for every expense",
                  ["1. Is it reimbursable under company policy?",
                   "2. How much is tax deductible under 2026 US rules?",
                   "A $186 client dinner: fully reimbursable, only 50% deductible."], 7)

        # Dashboard
        self.go("/role/finance")
        self.say("Finance dashboard: where every submitted dollar ends up.", 4.5)
        self.scroll(1050)
        self.say("And the expenses to confirm with the CPA, each with the reason why.", 5)

        # Onboarding: industry + policy document
        self.go("/onboarding")
        self.say("Onboarding: choose the industry and upload the company expense policy.", 2.5)
        p.set_input_files("input[name=policy_file]", str(ROOT / "demo" / "policy_acme.md"))
        self.click(p.get_by_role("button", name="Upload and analyze"), pause=0)
        p.wait_for_load_state("networkidle")
        p.locator("#policyGrid").scroll_into_view_if_needed()
        self.say("The policy is summarized and turned into a rule table that Finance edits and confirms.", 5.5)
        self.click(p.get_by_role("button", name="Confirm policy"), pause=1.6)

        # Channel A: buy through PayPal
        self.go("/role/employee")
        self.go("/store")
        self.say("An employee buys through PayPal. The itemized PayPal order is the receipt: no upload, no typing.", 2)
        self.add_item("Dinner for two")
        self.add_item("Wine pairing")
        self.type(p.locator("aside textarea"), DINNER_PURPOSE)
        self.type(p.locator("aside input").last, "Jane Doe (Globex CTO, customer), Alice Chen")
        dinner = self.pay()
        self.say("Reviewed in seconds: reimbursable, and 50% deductible as a client meal. $93 of $186.", 6)
        print("client dinner:", self.status(dinner))

        # Follow-up question
        self.go("/store")
        self.say("The same dinner from another employee, this time without attendees.", 1)
        p.locator("aside select").select_option(label="Carol Singh")
        self.add_item("Dinner for two")
        self.add_item("Wine pairing")
        self.type(p.locator("aside textarea"), DINNER_PURPOSE)
        asked = self.pay()
        box = p.get_by_placeholder("Answer as Carol Singh")
        box.scroll_into_view_if_needed()
        self.say("Tax rules require who attended, so SpendSight asks instead of guessing.", 4)
        self.type(box, "Jane Doe, CTO of Globex, our customer")
        self.click(p.get_by_role("button", name="Answer", exact=True), pause=0)
        p.wait_for_load_state("networkidle")
        self.say("The employee answers and the review reruns: approved.", 4)
        print("dinner after answer:", self.status(asked))

        # Pre-approval hold: authorize, then capture
        self.go("/store")
        self.say("A $1,150 hotel is above the pre-approval threshold.", 0.5)
        self.add_item("Hotel room, 3 nights")
        self.type(p.locator("aside textarea"), "Hotel for DataCon conference in Las Vegas, Dec 1 to Dec 4")
        self.say("PayPal only authorizes the payment. Nothing is captured until Finance decides.", 3.5)
        hotel = self.pay()
        self.go("/role/finance")
        self.go(f"/expenses/{hotel}")
        self.say("Finance sees the held payment. Approve captures the authorization; reject voids it.", 4)
        self.click(p.get_by_role("button", name="Approve and capture"), pause=0)
        p.wait_for_load_state("networkidle")
        self.say("Approved: the PayPal authorization is captured.", 3)
        print("hotel:", self.status(hotel))

        # Channel B: paper receipt, and the industry override
        self.go("/role/employee")
        self.go("/employee")
        self.say("Paper receipts work too.", 0.5)
        p.set_input_files("input[name=receipt]", str(ROOT / "demo" / "receipts" / "office_snacks.png"))
        self.click(p.get_by_role("button", name="Upload"), pause=0)
        p.wait_for_load_state("networkidle")
        self.say("Without an AI key, OCR and a parser read the vendor, date, total and items.", 4)
        self.type(p.locator("textarea[name=purpose]"), "Snacks and coffee for the office")
        self.click(p.get_by_role("button", name="Submit for review"), pause=0)
        p.wait_for_url("**/expenses/*", wait_until="networkidle")
        snacks = self.expense_id()
        self.say("Office snacks: reimbursable, but 0% deductible from 2026 (IRC 274(o)).", 5.5)
        self.go("/role/finance")
        self.api_form("/onboarding/company", {"name": "Acme Analytics Inc.", "industry_id": "restaurant"})
        self.go(f"/expenses/{snacks}")
        self.say("Change the company's industry to restaurant and run the review again...", 1.5)
        self.click(p.get_by_role("button", name="Re-run review"), pause=0)
        p.wait_for_load_state("networkidle")
        self.say("...and the same receipt is deductible: staff meals are an industry exception.", 5)
        self.api_form("/onboarding/company", {"name": "Acme Analytics Inc.", "industry_id": "software_saas"})
        self.api(f"/api/expenses/{snacks}/review")

        # Anomaly: split purchase
        accessories = {"employee_id": 2, "vendor_id": "officedepotish", "items": [{"sku": "od-dock", "quantity": 1}],
                       "purpose": "Laptop accessories for new engineer workstation"}
        self.buy_quietly(accessories)
        split = self.buy_quietly(accessories)["expense_id"]
        self.buy_quietly({"employee_id": 2, "vendor_id": "fitnessclub", "items": [{"sku": "fc-month", "quantity": 1}],
                          "purpose": "team wellness"})
        self.go(f"/expenses/{split}")
        self.say("Two $240 orders on the same day against a $300 limit: flagged as a split purchase.", 4)
        p.locator("#spcWrap").scroll_into_view_if_needed()
        self.say("A control chart compares each purchase with the employee's history.", 4)

        # Queue
        self.go("/finance/queue")
        self.say("The review queue runs on AG Grid: every expense with its decision and engine.", 3)
        self.click(p.locator("button[data-status=flagged]"), pause=1.6)
        self.click(p.locator("button[data-status=rejected]"), pause=1.6)
        self.say('A gym membership filed as "team wellness" is rejected as personal.', 3)
        self.click(p.locator("button[data-status='']"), pause=0.6)

        # Tax rules: reviewed defaults that the CPA can adjust
        self.go("/tax-rules")
        self.say("Every deductible rate starts from a reviewed default with its citation. The CPA owns this table.", 4)
        rate = p.get_by_label("Rate for Other ordinary and necessary business expense")
        self.click(rate, pause=0.1)
        p.keyboard.press("Control+A")
        p.keyboard.type("0", delay=60)
        p.keyboard.press("Tab")
        self.say("Changing a rate always shows the reviewed default first, and records the reason.", 1.5)
        p.keyboard.type("Hold at 0% until our CPA reviews these", delay=14)
        self.wait(2.2 * PACE)
        self.click(p.get_by_role("button", name="Change to 0%"), pause=1.0)
        p.locator("text=Category keywords").first.scroll_into_view_if_needed()
        self.say("Rates and keywords are data, so another country is just another rule pack.", 4)

        # Payouts
        self.go("/reimburse")
        self.say("Approved expenses are reimbursed in one PayPal Payouts batch: one payment per employee.", 3)
        self.click(p.get_by_role("button", name="Send PayPal Payouts"), pause=0)
        p.locator("text=Payout batches").scroll_into_view_if_needed()
        p.wait_for_selector("text=succeeded", timeout=20000)
        self.say("Status is tracked by webhook and by polling until every payment succeeds.", 3.5)

        # Tax report
        self.go("/reports/tax")
        self.say("The tax report splits spend by category and Form 1120 line, and lists missing documents.", 5)
        with p.expect_download() as download:
            self.click(p.get_by_role("link", name="Export CPA package (XLSX)"), pause=0)
        download.value.save_as(str(OUT / "cpa_package.xlsx"))
        self.say("One click exports the CPA package: summary, lines, missing documents and every decision.", 4.5)

        self.card("How it is built", "PayPal at the center. AI with a safety net.",
                  ["PayPal: Orders v2 (capture, authorize, void), Payouts, Webhooks, Transaction Search.",
                   "AI: expense agent, receipt vision and policy reading, guarded by a tax rule pack.",
                   "Rates, keywords and citations are editable data: a CPA adjusts them, another country adds a rule pack.",
                   "This recording uses no AI key and the mock PayPal client: every step ran on the built-in fallbacks.",
                   "github.com/minsmseo/SpendSight"], 9)

    def api_form(self, path: str, form: dict) -> None:
        self.page.request.post(BASE + path, form=form)


def record(browser) -> float:
    context = browser.new_context(viewport=VIDEO, record_video_dir=str(OUT / "_video"), record_video_size=VIDEO,
                                  accept_downloads=True)
    context.add_init_script(OVERLAY_JS)
    page = context.new_page()
    started = time.time()
    try:
        Demo(page).run()
    finally:
        elapsed = time.time() - started
        video = page.video
        context.close()                                   # flushes the video file
        target = OUT / "spendsight_demo.webm"
        target.unlink(missing_ok=True)
        Path(video.path()).rename(target)
    return elapsed


def screenshots(browser) -> None:
    context = browser.new_context(viewport=SHOT, device_scale_factor=2)
    page = context.new_page()
    demo = Demo(page)
    shots = OUT / "screenshots"
    shots.mkdir(exist_ok=True)

    def shoot(name: str, wait: float = 1.2) -> None:
        demo.wait(wait)
        page.screenshot(path=str(shots / name))
        print("saved", name)

    demo.go("/role/finance")
    held = demo.buy_quietly({"employee_id": 3, "vendor_id": "hotellumen", "items": [{"sku": "hl-1n", "quantity": 4}],
                             "purpose": "Hotel for customer onsite visit in Austin, Dec 8 to Dec 12"})["expense_id"]
    asked = demo.buy_quietly({"employee_id": 2, "vendor_id": "steakhouse21", "items": [{"sku": "st-dinner-1", "quantity": 2}],
                              "purpose": "Dinner with Initech VP to discuss the pilot"})["expense_id"]
    demo.go("/dashboard")
    shoot("01_dashboard.png", 2)
    page.locator("text=Confirm with your CPA").first.scroll_into_view_if_needed()
    demo.scroll(420)
    shoot("02_cpa_review_list.png")
    demo.go("/finance/queue")
    shoot("03_review_queue.png", 2)
    demo.go(f"/expenses/{held}")
    shoot("04_held_paypal_authorization.png", 2)
    demo.go(f"/expenses/{asked}")
    shoot("05_follow_up_question.png", 2)
    demo.go("/reimburse")
    shoot("06_paypal_payouts.png")
    demo.go("/reports/tax")
    shoot("07_tax_report.png")
    demo.go("/onboarding")
    page.locator("#policyGrid").scroll_into_view_if_needed()
    demo.scroll(-160)
    shoot("08_policy_table.png")
    demo.go("/finance/import")
    shoot("09_paypal_history_import.png")
    demo.go("/tax-rules")
    shoot("11_tax_rules.png")
    rate = page.get_by_label("Rate for Meal with client, prospect or partner")
    rate.click()
    page.keyboard.press("Control+A")
    page.keyboard.type("40")
    page.keyboard.press("Tab")
    page.get_by_placeholder("e.g. Conservative position agreed with our CPA").fill("Conservative position agreed with our CPA")
    page.mouse.move(40, 500)
    shoot("12_change_rate_shows_default.png")
    page.get_by_role("button", name="Keep 50%").click()
    demo.go("/role/employee")
    demo.go("/store")
    demo.add_item("Hotel room, 3 nights")
    page.locator("aside textarea").fill("Hotel for DataCon conference in Las Vegas, Dec 1 to Dec 4")
    page.mouse.move(40, 500)
    shoot("10_vendor_store_hold.png")
    context.close()


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel=CHANNEL, headless=True)
        if "--shots-only" not in sys.argv:
            seconds = record(browser)
            print(f"video: {OUT / 'spendsight_demo.webm'}  ({seconds:.0f}s, limit 180s)")
        if "--video-only" not in sys.argv:
            screenshots(browser)
        browser.close()
