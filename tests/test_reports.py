"""Tax report: periods, tables, missing substantiation and the CPA package exports."""
import io
from datetime import date

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.routers.reports import period_range
from app.web import usd


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_period_ranges():
    assert period_range("ytd", 2026)[:2] == (date(2026, 1, 1), date(2027, 1, 1))
    assert period_range("Q4", 2026)[:3] == (date(2026, 10, 1), date(2027, 1, 1), "Q4 2026")
    assert period_range("q1", 2026)[:2] == (date(2026, 1, 1), date(2026, 4, 1))
    assert period_range("11", 2026)[:3] == (date(2026, 11, 1), date(2026, 12, 1), "November 2026")
    assert period_range("12", 2026)[1] == date(2027, 1, 1)
    assert period_range("nonsense", 2026)[3] == "ytd"
    assert (usd(1234.5), usd(0), usd(-5), usd(None)) == ("$1,234.50", "$0.00", "-$5.00", "")


def test_report_totals_tables_and_periods(client):
    ytd = client.get("/api/reports/tax").json()
    assert ytd["period"] == "ytd" and ytd["expense_count"] >= 100
    assert round(ytd["deductible"] + ytd["nondeductible"], 2) == ytd["spent"]
    assert round(sum(r["spent"] for r in ytd["by_category"]), 2) == ytd["spent"]
    assert round(sum(r["spent"] for r in ytd["by_line"]), 2) == ytd["spent"]             # every dollar lands on exactly one line
    by_line = {r["line"]: r for r in ytd["by_line"]}
    assert by_line["22"]["label"] == "Advertising" and by_line["26"]["label"] == "Other deductions"
    assert by_line["none"]["spent"] == ytd["nondeductible"] and by_line["none"]["deductible"] == 0   # e.g. half of client meals
    meal = next(r for r in ytd["by_category"] if r["category"] == "meal")
    assert meal["nondeductible"] > 0 and meal["label"] == "Meals"

    months = [client.get(f"/api/reports/tax?period={m:02d}").json() for m in range(1, 13)]
    assert round(sum(m["spent"] for m in months), 2) == ytd["spent"]
    quarters = [client.get(f"/api/reports/tax?period=Q{q}").json() for q in range(1, 5)]
    assert round(sum(q["spent"] for q in quarters), 2) == ytd["spent"]
    assert client.get("/api/reports/tax?period=01").json()["expense_count"] == 0           # seeded history starts in the autumn


def test_missing_substantiation_list(client):
    # An uploaded lodging expense with no receipt file, approved by Finance.
    form = {"employee_id": 2, "vendor": "Hotel Lumen", "purchased_on": "2026-11-04", "total": "60",
            "items": "Hotel room late checkout fee, 60.00", "purpose": "Hotel for customer onsite visit in Boston"}
    eid = int(client.post("/expenses/submit", data=form, follow_redirects=False).headers["location"].rsplit("/", 1)[1])
    status = next(r["status"] for r in client.get("/api/expenses").json() if r["id"] == eid)
    if status != "approved":
        client.post(f"/api/expenses/{eid}/approve")
    report = client.get("/api/reports/tax?period=11").json()
    entry = next(m for m in report["missing"] if m["expense_id"] == eid)
    assert entry["missing"] == ["receipt"]                                                # lodging needs one at any amount
    assert report["missing_count"] == len(report["missing"]) >= 1
    seeded = [m for m in client.get("/api/reports/tax").json()["missing"] if m["expense_id"] <= 150]
    assert seeded and all("receipt" in m["missing"] for m in seeded)                       # the uploads seeded without a receipt
    assert len(seeded) < 20                                                                # most seeded uploads do have one


def test_page_and_exports(client):
    page = client.get("/reports/tax?period=Q4").text
    assert "Not tax advice" in page and "Export CPA package" in page and "Missing substantiation" in page
    assert 'value="Q4" selected' in page
    assert "No approved or paid expenses" in client.get("/reports/tax?period=01").text

    report = client.get("/api/reports/tax?period=Q4").json()
    res = client.get("/reports/tax/cpa_package.xlsx?period=Q4")
    assert res.status_code == 200 and "cpa_package.xlsx" in res.headers["content-disposition"]
    wb = load_workbook(io.BytesIO(res.content))
    assert wb.sheetnames == ["Summary", "Lines", "Missing", "Decisions"]
    summary = [[c.value for c in row] for row in wb["Summary"].iter_rows()]
    flat = [v for row in summary for v in row if v is not None]
    assert "Q4 2026" in flat and report["spent"] in flat and "Acme Analytics Inc." in flat
    assert any(isinstance(v, str) and "Not tax advice" in v for v in flat)
    lines = list(wb["Lines"].iter_rows(values_only=True))
    assert lines[0][:4] == ("Expense #", "Date", "Employee", "Vendor") and "Citation" in lines[0]
    assert round(sum(r[6] for r in lines[1:]), 2) == report["spent"]
    assert round(sum(r[8] for r in lines[1:]), 2) == report["deductible"]
    assert wb["Missing"].max_row == report["missing_count"] + 1
    decisions = list(wb["Decisions"].iter_rows(values_only=True))
    assert decisions[0][6] == "Engine" and {r[6] for r in decisions[1:]} <= {"rules", "llm"}
    assert len(decisions) - 1 == report["expense_count"] and all(r[9] for r in decisions[1:])   # every decision has reasons

    csv_res = client.get("/reports/tax/lines.csv?period=Q4")
    text = csv_res.content.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("Expense #,Date,Employee,Vendor,Item,Category,Amount")
    assert "Not tax advice" in text.splitlines()[-1] and len(text.splitlines()) == len(lines) + 2
