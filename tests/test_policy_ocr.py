"""No-key document handling: policy summary + regex rule table, and the receipt text parser."""
import shutil

import pytest
from fastapi.testclient import TestClient

from app.ai import provider as ai_provider
from app.ai.provider import ProviderError
from app.extract import ocr
from app.rules import policy_parser
from app.summarize import summarizer
from demo.make_receipts import RECEIPTS

POLICY_TEXT = open("demo/policy_acme.md", encoding="utf-8").read()


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def rows_by_category(policy):
    return {r["category"]: r for r in policy["rows"]}


def test_regex_reads_limits_thresholds_and_banned_items():
    policy = policy_parser.parse_policy_text(POLICY_TEXT)
    rows = rows_by_category(policy)
    assert policy["preapproval_threshold"] == 1000 and policy["receipt_required_above"] == 75
    assert rows["meal"]["per_txn_limit"] == 250                      # highest of the meal limits, not the $100 snack line
    assert rows["travel_transport"]["per_txn_limit"] == 800
    assert (rows["entertainment"]["per_txn_limit"], rows["entertainment"]["preapproval_above"]) == (500, 300)
    assert rows["gift"]["per_txn_limit"] == 100 and rows["software"]["per_txn_limit"] == 500
    assert rows["office_supplies"]["per_txn_limit"] == 300
    assert rows["lodging"]["per_txn_limit"] == 1500 and "$350 per night" in rows["lodging"]["notes"]   # nightly rate is a note
    assert {"gym membership", "personal clothing", "traffic fine", "political contribution"} <= set(policy["banned_items"])
    assert rows["personal"]["banned"] and rows["political"]["banned"]


def test_analyze_without_key_uses_textrank_and_rules():
    result = policy_parser.analyze_policy(POLICY_TEXT)
    assert (result["summary_engine"], result["table_engine"]) == ("textrank", "rules")
    assert 5 <= len(summarizer.split_sentences(result["summary"])) <= 8
    assert "found" not in result["policy"] and result["found"]


def test_sanitize_drops_bad_rows_and_numbers():
    policy = policy_parser.sanitize_policy({
        "preapproval_threshold": "abc", "banned_items": "Gym Membership, ,spa",
        "rows": [{"category": "meal", "per_txn_limit": "-5", "banned": 0, "notes": None},
                 {"category": "meal", "per_txn_limit": 9}, {"category": "made_up", "per_txn_limit": 1}],
    })
    assert [r["category"] for r in policy["rows"]] == ["meal"] and policy["rows"][0]["per_txn_limit"] == 0
    assert policy["preapproval_threshold"] == 1000 and policy["banned_items"] == ["gym membership", "spa"]


class FakePolicyProvider:
    name, model = "fake", "fake-1"

    def __init__(self, result):
        self.result = result

    def complete_json(self, system, user, schema, timeout=30):
        if isinstance(self.result, Exception):
            raise self.result
        assert "<policy_document>" in user
        return self.result


def test_llm_policy_path_and_fallback(monkeypatch):
    llm = {"summary": "Meals up to $80.", "preapproval_threshold": 700, "receipt_required_above": 50, "banned_items": ["Spa"],
           "rows": [{"category": "meal", "per_txn_limit": 80, "preapproval_above": 0, "receipt_required_above": 0, "banned": False, "notes": ""}]}
    monkeypatch.setattr(ai_provider, "get_provider", lambda: FakePolicyProvider(llm))
    result = policy_parser.analyze_policy(POLICY_TEXT)
    assert (result["summary_engine"], result["table_engine"], result["summary"]) == ("llm", "llm", "Meals up to $80.")
    rows = rows_by_category(result["policy"])
    assert rows["meal"]["per_txn_limit"] == 80 and rows["lodging"]["per_txn_limit"] == 1500   # skipped categories keep defaults
    assert result["policy"]["preapproval_threshold"] == 700

    monkeypatch.setattr(ai_provider, "get_provider", lambda: FakePolicyProvider(ProviderError("timeout")))
    assert policy_parser.analyze_policy(POLICY_TEXT)["table_engine"] == "rules"
    monkeypatch.setattr(ai_provider, "get_provider", lambda: FakePolicyProvider({"summary": 5}))   # fails validation
    assert policy_parser.analyze_policy(POLICY_TEXT)["table_engine"] == "rules"


def test_upload_propose_confirm_and_policy_takes_effect(client):
    page = client.post("/onboarding/policy", files={"policy_file": ("policy_acme.md", POLICY_TEXT.encode(), "text/markdown")})
    assert page.status_code == 200 and "not saved yet" in page.text and "gym membership" in page.text
    bad = client.post("/onboarding/policy", files={"policy_file": ("policy.exe", b"MZ" * 50, "application/octet-stream")})
    assert "Upload a PDF, DOCX, MD or TXT file" in bad.text

    policy = policy_parser.sanitize_policy(policy_parser.parse_policy_text(POLICY_TEXT))
    next(r for r in policy["rows"] if r["category"] == "office_supplies")["per_txn_limit"] = 100    # Finance edits the grid
    try:
        saved = client.post("/api/onboarding/policy", json={"policy": policy, "summary": "Edited policy."})
        assert saved.json() == {"ok": True, "rows": 12}
        assert "Edited policy." in client.get("/onboarding").text
        cart = {"employee_id": 2, "vendor_id": "officedepotish", "items": [{"sku": "od-paper", "quantity": 3}],
                "purpose": "Printer paper supplies for the engineering office"}
        order = client.post("/api/paypal/orders", json=cart).json()
        done = client.post(f"/api/paypal/orders/{order['order_id']}/complete").json()
        assert done["status"] == "flagged"                           # $126 is over the edited $100 limit
        assert "above the $100.00 policy limit" in client.get(f"/expenses/{done['expense_id']}").text
    finally:
        assert client.post("/api/onboarding/policy/default").json() == {"ok": True}
    assert client.post("/api/onboarding/policy", json={"policy": {"rows": []}}).json()["rows"] == 12   # empty -> defaults


def receipt_text(vendor, when, items, tax):
    subtotal = sum(p for _, p in items)
    body = "\n".join(f"{name}    ${price:.2f}" for name, price in items)
    return f"{vendor}\n{when}\n{body}\n\nSUBTOTAL    ${subtotal:.2f}\nSALES TAX    ${tax:.2f}\nTOTAL    ${subtotal + tax:.2f}\n"


@pytest.mark.parametrize("name", list(RECEIPTS))
def test_parser_reads_total_date_vendor_and_items(name):
    vendor, when, items, tax = RECEIPTS[name]
    got = ocr.parse_receipt_text(receipt_text(vendor, when, items, tax))
    fields = got["fields"]
    assert fields["total"] == round(sum(p for _, p in items) + tax, 2) and fields["tax"] == tax
    assert fields["date"] == f"2026-11-{when[3:5]}" and fields["vendor"] == vendor
    assert [i["name"] for i in fields["items"]] == [n for n, _ in items]
    assert got["confidence"]["total"] >= 0.85 and got["confidence"]["items"] >= 0.85


def test_parser_handles_noise_and_missing_total():
    got = ocr.parse_receipt_text("Joe's Diner\nNov 3, 2026\n2 Burger 24.00\nCoke 3.50\nVISA ****1234 27.50\n")
    assert got["fields"]["date"] == "2026-11-03" and got["fields"]["total"] == 27.5
    assert got["confidence"]["total"] == 0.9                          # no TOTAL line, but the items add up to it
    assert got["fields"]["items"][0] == {"name": "Burger", "quantity": 2.0, "unit_amount": 12.0, "amount": 24.0}
    assert ocr.parse_receipt_text("")["fields"] == {}
    assert ocr.find_date("Date: 3/7/26") == "2026-03-07" and ocr.find_date("no date here") is None


def test_unreadable_upload_never_raises():
    got = ocr.extract_receipt(b"not an image", "image/png")
    assert got["fields"] == {} and got["engine"] == "ocr"


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract is not installed on this machine")
@pytest.mark.parametrize("name", list(RECEIPTS))
def test_tesseract_reads_demo_receipt_images(name):
    vendor, when, items, tax = RECEIPTS[name]
    with open(f"demo/receipts/{name}", "rb") as fh:
        fields = ocr.extract_receipt(fh.read(), "image/png")["fields"]
    assert fields["total"] == round(sum(p for _, p in items) + tax, 2)
    assert fields["date"] == f"2026-11-{when[3:5]}"
