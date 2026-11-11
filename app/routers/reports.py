"""Tax report + CPA package exports (CLAUDE.md 4.6).

Decision support for a CPA, not tax advice: every table and export carries the disclaimer.
"""
import csv
import io
import json
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlmodel import Session, select

from app import config
from app.db import get_session
from app.models import Company, Decision, Employee, Expense, LineItem
from app.rules import engine
from app.web import page

router = APIRouter()
COUNTED = ("approved", "paid")                   # only reviewed, accepted expenses reach the tax report
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
NONDEDUCTIBLE = "Nondeductible (Schedule M-1 adjustment)"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# --- periods ----------------------------------------------------------------
def period_options(year: int) -> list[dict]:
    return ([{"value": "ytd", "label": f"{year} year to date"}]
            + [{"value": f"Q{q}", "label": f"Q{q} {year}"} for q in range(1, 5)]
            + [{"value": f"{m:02d}", "label": f"{MONTHS[m - 1]} {year}"} for m in range(1, 13)])


def period_range(period: str, year: int) -> tuple[date, date, str, str]:
    """(start, end exclusive, label, normalized value). Unknown values fall back to year to date."""
    def month_start(m: int) -> date:
        return date(year + (m - 1) // 12, (m - 1) % 12 + 1, 1)

    period = (period or "ytd").strip()
    if period.upper() in ("Q1", "Q2", "Q3", "Q4"):
        q = int(period[1])
        return month_start(3 * q - 2), month_start(3 * q + 1), f"Q{q} {year}", f"Q{q}"
    if period.isdigit() and 1 <= int(period) <= 12:
        m = int(period)
        return month_start(m), month_start(m + 1), f"{MONTHS[m - 1]} {year}", f"{m:02d}"
    return date(year, 1, 1), date(year + 1, 1, 1), f"{year} year to date", "ytd"


# --- report -----------------------------------------------------------------
def _missing(expense: Expense, lines: list[LineItem], threshold: float) -> list[str]:
    """Substantiation still missing for an accepted expense (Reg. §1.274-5, IRS Pub. 463)."""
    out = []
    categories = {li.category for li in lines}
    needs_receipt = "lodging" in categories or expense.total >= threshold
    if expense.source == "upload" and needs_receipt and not expense.receipt_path:
        out.append("receipt")
    who_needed = any(li.rule_id == "meal_client" for li in lines) or categories & {"entertainment", "gift"}
    if who_needed and not (expense.attendees or "").strip():
        out.append("attendees and business relationship")
    if len((expense.purpose or "").split()) < 4:
        out.append("business purpose")
    return out


def build_report(session: Session, period: str = "ytd") -> dict:
    company = session.exec(select(Company)).first()
    start, end, label, value = period_range(period, company.tax_year)
    employees = {e.id: e for e in session.exec(select(Employee)).all()}
    threshold = engine.load_rule_pack()["global_substantiation"]["receipt_threshold_usd"]

    expenses = []
    for e in session.exec(select(Expense).where(Expense.status.in_(COUNTED)).order_by(Expense.purchased_at)).all():
        if e.purchased_at and start <= engine.to_local(e.purchased_at).date() < end:
            expenses.append(e)
    ids = {e.id for e in expenses}
    by_expense: dict[int, list[LineItem]] = {}
    for li in session.exec(select(LineItem)).all():
        if li.expense_id in ids:
            by_expense.setdefault(li.expense_id, []).append(li)
    auto: dict[int, Decision] = {}
    finance: dict[int, Decision] = {}
    for d in session.exec(select(Decision).order_by(Decision.id)).all():
        if d.expense_id in ids:
            (finance if d.engine == "finance" else auto)[d.expense_id] = d

    categories: dict[str, dict] = {}
    form_lines: dict[str, dict] = {}

    def add_form_line(key: str, text: str, spent: float, deductible: float) -> None:
        fl = form_lines.setdefault(key, {"line": key, "label": text, "spent": 0.0, "deductible": 0.0})
        fl["spent"] += spent
        fl["deductible"] += deductible

    lines, missing, decisions = [], [], []
    labels = engine.load_taxonomy()["categories"]
    for e in expenses:
        emp = employees.get(e.employee_id)
        local_date = engine.to_local(e.purchased_at).date().isoformat()
        items = by_expense.get(e.id, [])
        for li in items:
            rule = engine.get_tax_rule(li.rule_id, company.industry_id)
            nondeductible = round(li.amount - li.deductible_amount, 2)
            cat = categories.setdefault(li.category, {
                "category": li.category, "label": labels.get(li.category, {}).get("label", li.category),
                "count": 0, "spent": 0.0, "deductible": 0.0})
            cat["count"] += 1
            cat["spent"] += li.amount
            cat["deductible"] += li.deductible_amount
            # A nondeductible portion never lands on a deduction line: it is a Schedule M-1 adjustment.
            if li.form1120_line == "none":
                add_form_line("none", NONDEDUCTIBLE, li.amount, 0.0)
            else:
                if li.deductible_amount:
                    add_form_line(li.form1120_line, rule["form1120_label"].split(" (")[0], li.deductible_amount, li.deductible_amount)
                if nondeductible:
                    add_form_line("none", NONDEDUCTIBLE, nondeductible, 0.0)
            lines.append({
                "expense_id": e.id, "date": local_date, "employee": emp.name if emp else "", "vendor": e.vendor,
                "item": li.name, "category": li.category, "amount": li.amount, "deductible_pct": li.deductible_pct,
                "deductible_amount": li.deductible_amount, "nondeductible_amount": nondeductible,
                "form1120_line": li.form1120_line, "rule_id": li.rule_id, "citation": rule["citation"],
                "source": e.source, "status": e.status, "paypal_capture_id": e.paypal_capture_id or "",
                "purpose": e.purpose, "attendees": e.attendees,
            })
        gaps = _missing(e, items, threshold)
        if gaps:
            missing.append({"expense_id": e.id, "date": local_date, "employee": emp.name if emp else "",
                            "vendor": e.vendor, "total": e.total, "missing": gaps})
        d, f = auto.get(e.id), finance.get(e.id)
        decisions.append({
            "expense_id": e.id, "date": local_date, "employee": emp.name if emp else "", "vendor": e.vendor,
            "total": e.total, "status": e.status, "engine": d.engine if d else "",
            "decision": d.decision if d else "", "fit_score": d.fit_score if d else None,
            "reasons": json.loads(d.reasons_json) if d else [],
            "anomalies": [a["explanation"] for a in json.loads(d.anomaly_json)] if d else [],
            "finance": f"{f.decision}: {' '.join(json.loads(f.reasons_json))}" if f else "",
        })

    def finish(rows: list[dict]) -> list[dict]:
        for r in rows:
            r["spent"], r["deductible"] = round(r["spent"], 2), round(r["deductible"], 2)
            r["nondeductible"] = round(r["spent"] - r["deductible"], 2)
        return rows

    by_category = finish(sorted(categories.values(), key=lambda r: -r["spent"]))
    order = {"none": "zz"}
    by_line = finish(sorted(form_lines.values(), key=lambda r: order.get(r["line"], r["line"].zfill(3))))
    spent = round(sum(r["spent"] for r in by_category), 2)
    deductible = round(sum(r["deductible"] for r in by_category), 2)
    return {
        "company": company.name, "industry": engine.load_industries().get(company.industry_id, {}).get("label", company.industry_id),
        "tax_year": company.tax_year, "period": value, "period_label": label,
        "start": start.isoformat(), "end": end.isoformat(), "options": period_options(company.tax_year),
        "expense_count": len(expenses), "spent": spent, "deductible": deductible,
        "nondeductible": round(spent - deductible, 2), "missing_count": len(missing),
        "by_category": by_category, "by_line": by_line, "rows": by_category,
        "missing": missing, "lines": lines, "decisions": decisions, "disclaimer": config.TAX_DISCLAIMER,
    }


def tax_summary(session: Session, period: str = "ytd") -> dict:
    return build_report(session, period)


# --- exports ----------------------------------------------------------------
LINE_COLUMNS = [("Expense #", "expense_id"), ("Date", "date"), ("Employee", "employee"), ("Vendor", "vendor"),
                ("Item", "item"), ("Category", "category"), ("Amount", "amount"), ("Deductible %", "deductible_pct"),
                ("Deductible amount", "deductible_amount"), ("Nondeductible amount", "nondeductible_amount"),
                ("Form 1120 line", "form1120_line"), ("Tax rule", "rule_id"), ("Citation", "citation"),
                ("Business purpose", "purpose"), ("Attendees", "attendees"), ("Source", "source"),
                ("Status", "status"), ("PayPal capture id", "paypal_capture_id")]


def build_workbook(report: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    bold, head_fill = Font(bold=True), PatternFill("solid", fgColor="E0E7FF")
    money = '"$"#,##0.00'

    def table(ws, top: int, headers: list[str], rows: list[list], money_cols=(), wrap_cols=()) -> int:
        for c, h in enumerate(headers, 1):
            cell = ws.cell(row=top, column=c, value=h)
            cell.font, cell.fill = bold, head_fill
        for r, row in enumerate(rows, top + 1):
            for c, v in enumerate(row, 1):
                cell = ws.cell(row=r, column=c, value=v)
                if c in money_cols:
                    cell.number_format = money
                if c in wrap_cols:
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
        return top + len(rows) + 1

    def widths(ws, sizes: list[int]):
        for i, w in enumerate(sizes, 1):
            ws.column_dimensions[get_column_letter(i)].width = w

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws["A1"], ws["A1"].font = f"{config.APP_NAME} CPA package", Font(bold=True, size=14)
    info = [("Company", report["company"]), ("Entity type", "C corporation (Form 1120)"), ("Industry", report["industry"]),
            ("Tax year", report["tax_year"]), ("Period", report["period_label"]), ("Expenses included", report["expense_count"]),
            ("Total spent", report["spent"]), ("Deductible", report["deductible"]), ("Nondeductible", report["nondeductible"]),
            ("Expenses missing substantiation", report["missing_count"])]
    for r, (k, v) in enumerate(info, 3):
        ws.cell(row=r, column=1, value=k).font = bold
        cell = ws.cell(row=r, column=2, value=v)
        if k in ("Total spent", "Deductible", "Nondeductible"):
            cell.number_format = money
    row = len(info) + 4
    ws.cell(row=row, column=1, value="By category").font = bold
    row = table(ws, row + 1, ["Category", "Lines", "Spent", "Deductible", "Nondeductible"],
                [[c["label"], c["count"], c["spent"], c["deductible"], c["nondeductible"]] for c in report["by_category"]], money_cols=(3, 4, 5))
    ws.cell(row=row + 1, column=1, value="By Form 1120 line").font = bold
    row = table(ws, row + 2, ["Form 1120 line", "Description", "Spent", "Deductible", "Nondeductible"],
                [[f["line"], f["label"], f["spent"], f["deductible"], f["nondeductible"]] for f in report["by_line"]], money_cols=(3, 4, 5))
    ws.cell(row=row + 1, column=1, value=report["disclaimer"]).font = Font(italic=True)
    ws.cell(row=row + 2, column=1, value="Every tax treatment and Form 1120 line mapping in this package should be verified by your CPA.").font = Font(italic=True)
    widths(ws, [34, 44, 16, 16, 16])

    ws = wb.create_sheet("Lines")
    table(ws, 1, [h for h, _ in LINE_COLUMNS], [[ln[k] for _, k in LINE_COLUMNS] for ln in report["lines"]], money_cols=(7, 9, 10))
    widths(ws, [10, 12, 18, 22, 36, 18, 12, 12, 16, 18, 14, 18, 34, 44, 34, 10, 10, 22])
    ws.freeze_panes = "A2"

    ws = wb.create_sheet("Missing")
    table(ws, 1, ["Expense #", "Date", "Employee", "Vendor", "Total", "Missing"],
          [[m["expense_id"], m["date"], m["employee"], m["vendor"], m["total"], "; ".join(m["missing"])] for m in report["missing"]], money_cols=(5,))
    widths(ws, [10, 12, 18, 24, 12, 60])
    ws.freeze_panes = "A2"

    ws = wb.create_sheet("Decisions")
    table(ws, 1, ["Expense #", "Date", "Employee", "Vendor", "Total", "Status", "Engine", "Decision", "Fit score", "Reasons", "Anomalies", "Finance override"],
          [[d["expense_id"], d["date"], d["employee"], d["vendor"], d["total"], d["status"], d["engine"], d["decision"], d["fit_score"],
            "\n".join(d["reasons"]), "\n".join(d["anomalies"]), d["finance"]] for d in report["decisions"]],
          money_cols=(5,), wrap_cols=(10, 11, 12))
    widths(ws, [10, 12, 18, 22, 12, 10, 9, 12, 9, 90, 50, 40])
    ws.freeze_panes = "A2"

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _download(data: bytes, media_type: str, filename: str) -> Response:
    return Response(data, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/reports/tax")
def tax_report(request: Request, period: str = "ytd", session: Session = Depends(get_session)):
    return page(request, "report.html", report=build_report(session, period))


@router.get("/api/reports/tax")
def api_tax_report(period: str = "ytd", session: Session = Depends(get_session)):
    report = build_report(session, period)
    return {k: v for k, v in report.items() if k not in ("lines", "decisions")}


@router.get("/reports/tax/cpa_package.xlsx")
def export_xlsx(period: str = "ytd", session: Session = Depends(get_session)):
    return _download(build_workbook(build_report(session, period)), XLSX, "cpa_package.xlsx")


@router.get("/reports/tax/lines.csv")
def export_csv(period: str = "ytd", session: Session = Depends(get_session)):
    report = build_report(session, period)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([h for h, _ in LINE_COLUMNS])
    writer.writerows([[ln[k] for _, k in LINE_COLUMNS] for ln in report["lines"]])
    writer.writerow([])
    writer.writerow([report["disclaimer"]])
    return _download(out.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8", f"spendsight_lines_{report['period']}.csv")
