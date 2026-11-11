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
import threading
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
        script = """t => { try { sessionStorage.setItem('demoCaption', t); } catch (e) {} const b = document.getElementById('demo-caption');
                      if (b) { b.textContent = t; b.style.display = t ? 'block' : 'none'; } }"""
        for attempt in range(3):
            try:
                self.page.evaluate(script, text)
                break
            except Exception:                              # the page was mid-navigation: wait and try again
                self.page.wait_for_load_state("networkidle")
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

    def click_and_reload(self, locator) -> None:
        """Click a button whose handler calls the API and then reloads the page (a review can take seconds)."""
        locator = locator.first
        locator.scroll_into_view_if_needed()
        box = locator.bounding_box()
        self.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=22)
        self.wait(0.15)
        with self.page.expect_navigation(wait_until="networkidle", timeout=120000):
            locator.click()
        self.wait(0.3)

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
    def row(self, expense_id: int) -> dict:
        rows = self.page.request.get(BASE + "/api/expenses").json()
        return next(r for r in rows if r["id"] == expense_id)

    def reviewing(self, text: str = "The AI agent reads the receipt, looks up the tax rule and writes its reasons...") -> None:
        self.say(text)

    def run(self) -> None:
        p = self.page
        ai = self.page.request.get(BASE + "/api/status").json()["ai"]
        live_ai = ai["state"] == "connected"
        who = "The AI agent" if live_ai else "SpendSight"

        # Prepare the side stories while the intro plays (each review takes a few seconds with a live model).
        side: dict = {}

        def prepare() -> None:
            import httpx

            def buy(cart: dict) -> dict:
                with httpx.Client(base_url=BASE, timeout=120) as http:
                    order = http.post("/api/paypal/orders", json=cart).json()
                    return http.post(f"/api/paypal/orders/{order['order_id']}/complete").json()

            side["asked"] = buy({"employee_id": 3, "vendor_id": "steakhouse21",
                                 "items": [{"sku": "st-dinner-2", "quantity": 1}, {"sku": "st-wine", "quantity": 1}],
                                 "purpose": DINNER_PURPOSE})["expense_id"]
            accessories = {"employee_id": 2, "vendor_id": "officedepotish", "items": [{"sku": "od-dock", "quantity": 1}],
                           "purpose": "Laptop accessories for new engineer workstation"}
            buy(accessories)
            side["split"] = buy(accessories)["expense_id"]
            buy({"employee_id": 2, "vendor_id": "fitnessclub", "items": [{"sku": "fc-month", "quantity": 1}], "purpose": "team wellness"})

        worker = threading.Thread(target=prepare, daemon=True)
        worker.start()

        # Pitch: the problem, who it is for, why it matters
        self.card("Build What's Next with PayPal and AI", "SpendSight",
                  ["An AI expense auditor for US companies, built on PayPal."], 3.2)
        self.card("The problem", "Every expense needs two answers",
                  ["Finance teams check receipts by hand: is this reimbursable under our policy?",
                   "Their CPA asks a different question: how much of it is tax deductible?",
                   "A $186 client dinner is fully reimbursable, but only 50% deductible.",
                   "SpendSight answers both at the moment of purchase, and shows its reasoning."], 9)

        # Dashboard
        self.go("/role/finance")
        self.say("For the finance team: where every submitted dollar ends up, and what needs a decision.", 4.2)
        self.scroll(1050)
        self.say("For the CPA: the expenses whose tax treatment needs a second look, each with the reason.", 4.2)

        # Channel A: buy through PayPal, reviewed by the agent
        self.go("/role/employee")
        self.go("/store")
        self.say("An employee pays a vendor through PayPal. The itemized PayPal order is the receipt.", 1.5)
        self.add_item("Dinner for two")
        self.add_item("Wine pairing")
        self.type(p.locator("aside textarea"), DINNER_PURPOSE)
        self.type(p.locator("aside input").last, "Jane Doe (Globex CTO, customer), Alice Chen")
        self.reviewing()
        dinner = self.pay()
        r = self.row(dinner)
        print("client dinner:", r["status"], r["engine"], r["deductible_amount"], r["question"])
        if r["status"] == "approved" and r["deductible_amount"] == 93.0:
            self.say(f"{who} approves it: reimbursable in full, 50% deductible as a client meal. $93 of $186.", 6)
        else:
            self.say(f"{who} explains its decision. The deductible share always comes from the tax rule pack.", 6)

        # Follow-up question
        worker.join(timeout=150)
        asked = side["asked"]
        self.go(f"/expenses/{asked}")
        box = p.get_by_placeholder("Answer as Carol Singh")
        box.scroll_into_view_if_needed()
        self.say("The same dinner without attendees: tax rules need to know who was there, so it asks instead of guessing.", 4.5)
        self.type(box, "Jane Doe, CTO of Globex (our customer), and me, Carol Singh")
        self.reviewing("The employee answers, and the review runs again...")
        self.click_and_reload(p.get_by_role("button", name="Answer", exact=True))
        r = self.row(asked)
        print("dinner after answer:", r["status"], r["engine"], r["question"])
        self.say("Approved after one answer." if r["status"] == "approved" else "The answer is on the record and the decision is updated.", 3.2)

        # Pre-approval hold: authorize, then capture
        self.go("/store")
        self.add_item("Hotel room, 3 nights")
        self.say("A $1,150 hotel is above the pre-approval limit, so PayPal only authorizes the payment.", 1.2)
        self.type(p.locator("aside textarea"), "Hotel for DataCon conference in Las Vegas, Dec 1 to Dec 4")
        hotel = self.pay()
        self.go("/role/finance")
        self.go(f"/expenses/{hotel}")
        self.say("Nothing is captured until Finance decides. Approve captures the authorization; reject voids it.", 4.2)
        self.click_and_reload(p.get_by_role("button", name="Approve and capture"))
        print("hotel:", self.row(hotel)["status"])
        self.say("Approved: the PayPal authorization is captured.", 2.6)

        # Channel B: paper receipt read by the model
        self.go("/role/employee")
        self.go("/employee")
        p.set_input_files("input[name=receipt]", str(ROOT / "demo" / "receipts" / "office_snacks.png"))
        self.say("Paper receipts work too. " + ("A vision model reads the image..." if live_ai else "OCR reads the image..."))
        self.click(p.get_by_role("button", name="Upload"), pause=0)
        p.wait_for_load_state("networkidle")
        self.say("Vendor, date, total and items are filled in for the employee to confirm.", 3)
        self.type(p.locator("textarea[name=purpose]"), "Snacks and coffee for the office")
        self.reviewing()
        self.click(p.get_by_role("button", name="Submit for review"), pause=0)
        p.wait_for_url("**/expenses/*", wait_until="networkidle")
        snacks = self.expense_id()
        r = self.row(snacks)
        print("office snacks:", r["status"], r["engine"], r["deductible_amount"])
        if r["deductible_amount"] == 0:
            self.say("Office snacks: reimbursable, but 0% deductible from 2026 under IRC 274(o).", 5)
        else:
            self.say("The agent picks the category; the tax rule pack sets the deductible share.", 5)

        # Anomaly: split purchase
        self.go("/role/finance")
        self.go(f"/expenses/{side['split']}")
        self.say("Two $240 orders on one day against a $300 limit: flagged as a split purchase.", 3.6)
        if p.locator("#spcWrap").is_visible():
            p.locator("#spcWrap").scroll_into_view_if_needed()
            self.say("A control chart compares each purchase with past spending.", 3)

        # Queue
        self.go("/finance/queue")
        self.say("The review queue runs on AG Grid: every expense with its decision and the engine that made it.", 3.2)
        self.click(p.locator("button[data-status=rejected]"), pause=0.8)
        self.say('A gym membership filed as "team wellness" is rejected as personal.', 2.8)

        # Tax rules: reviewed defaults that the CPA can adjust
        self.go("/tax-rules")
        self.say("The AI never sets a tax rate. Rates come from reviewed defaults with citations, and the CPA owns them.", 4)
        rate = p.get_by_label("Rate for Other ordinary and necessary business expense")
        self.click(rate, pause=0.1)
        p.keyboard.press("Control+A")
        p.keyboard.type("0", delay=60)
        p.keyboard.press("Tab")
        self.say("Changing a rate always shows the reviewed default first, and records the reason.", 1.2)
        p.keyboard.type("Hold at 0% until our CPA reviews these", delay=14)
        self.wait(2.0 * PACE)
        self.click(p.get_by_role("button", name="Change to 0%"), pause=0.8)
        self.say("Rates and keywords are data, so another country is another rule pack.", 3.2)

        # Payouts
        self.go("/reimburse")
        self.say("Approved expenses are reimbursed in one PayPal Payouts batch: one payment per employee.", 2.6)
        self.click(p.get_by_role("button", name="Send PayPal Payouts"), pause=0)
        p.locator("text=Payout batches").scroll_into_view_if_needed()
        p.wait_for_selector("text=succeeded", timeout=20000)
        self.say("Status is tracked by webhook and by polling until every payment succeeds.", 3)

        # Tax report
        self.go("/reports/tax")
        self.say("The tax report splits spend by category and Form 1120 line, and lists missing documents.", 4)
        with p.expect_download() as download:
            self.click(p.get_by_role("link", name="Export CPA package (XLSX)"), pause=0)
        download.value.save_as(str(OUT / "cpa_package.xlsx"))
        self.say("One click exports the CPA package, with every decision and its reasons.", 3.4)

        recorded = (f"Recorded with a live AI model ({ai.get('model', '')}) and the PayPal mock client."
                    if live_ai else "Recorded with no AI key and the PayPal mock client: every step ran on the built-in fallbacks.")
        self.card("How it is built", "PayPal at the center. AI with a safety net.",
                  ["PayPal: Orders v2 (capture, authorize, void), Payouts, Webhooks, Transaction Search.",
                   "AI: an expense agent with tools, receipt vision and policy reading.",
                   "If the AI key is missing or fails, a rule engine and OCR take over. Nothing stops.",
                   recorded,
                   "github.com/minsmseo/SpendSight"], 8)

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
