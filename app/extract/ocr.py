"""Receipt reading without AI: Tesseract OCR (or a PDF's own text layer) + a regex parser.

Returns the same shape as the vision extractor, with a confidence per field so the form can
highlight what the user should check. If Tesseract is not installed the fields come back empty
and the user fills the form by hand.
"""
import io
import re
from datetime import datetime

MONEY = re.compile(r"\$?\s?(\d{1,3}(?:,\d{3})+\.\d{2}|\d+\.\d{2})(?!\d)")
TOTAL_WORDS = re.compile(r"\b(GRAND TOTAL|TOTAL DUE|AMOUNT DUE|BALANCE DUE|TOTAL|BALANCE)\b", re.I)
TAX_WORDS = re.compile(r"\b(SALES TAX|TAX|VAT|GST)\b", re.I)
NOT_ITEM = re.compile(r"\b(SUB\s?TOTAL|TOTAL|TAX|VAT|GST|BALANCE|AMOUNT DUE|CHANGE|CASH|VISA|MASTERCARD|AMEX|DEBIT|CREDIT|TIP|GRATUITY|TENDER|PAYMENT)\b", re.I)
MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"), "ymd"),
    (re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b"), "mdy"),
    (re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2})\b"), "mdy2"),
    (re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b"), "mon_d_y"),
    (re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b"), "d_mon_y"),
]


def _amount(s: str) -> float:
    return float(s.replace(",", ""))


def _month(name: str) -> int | None:
    key = name[:3].lower()
    return MONTHS.index(key) + 1 if key in MONTHS else None


def find_date(text: str) -> str | None:
    """First plausible purchase date as YYYY-MM-DD (US formats: month before day)."""
    for pattern, kind in DATE_PATTERNS:
        for m in pattern.finditer(text):
            a, b, c = m.groups()
            try:
                if kind == "ymd":
                    y, mo, d = int(a), int(b), int(c)
                elif kind == "mdy":
                    mo, d, y = int(a), int(b), int(c)
                elif kind == "mdy2":
                    mo, d, y = int(a), int(b), 2000 + int(c)
                elif kind == "mon_d_y":
                    mo, d, y = _month(a), int(b), int(c)
                else:
                    d, mo, y = int(a), _month(b), int(c)
                if mo and 2000 <= y <= 2100:
                    return datetime(y, mo, d).date().isoformat()
            except ValueError:
                continue
    return None


def parse_receipt_text(text: str) -> dict:
    """Regex parser for receipt text. Returns {"fields", "confidence", "engine": "ocr"}."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    fields: dict = {"vendor": "", "date": "", "total": None, "tax": 0, "items": []}
    conf: dict = {"vendor": 0.0, "date": 0.0, "total": 0.0, "tax": 0.3, "items": 0.0}
    if not lines:
        return {"fields": {}, "confidence": {}, "engine": "ocr"}

    # Vendor: first line that is mostly letters (receipts print the merchant name on top).
    for ln in lines[:6]:
        letters = sum(c.isalpha() for c in ln)
        if letters >= 3 and letters >= 0.6 * len(ln.replace(" ", "")) and not MONEY.search(ln):
            fields["vendor"] = ln
            conf["vendor"] = 0.75 if ln.upper() == ln else 0.55
            break

    date = find_date(text)
    if date:
        fields["date"], conf["date"] = date, 0.8

    all_money = [_amount(m) for m in MONEY.findall(text)]
    near_total = [_amount(m) for ln in lines if TOTAL_WORDS.search(ln) and not re.search(r"SUB\s?TOTAL", ln, re.I)
                  for m in MONEY.findall(ln)]
    if near_total:
        fields["total"], conf["total"] = max(near_total), 0.85
    elif all_money:
        fields["total"], conf["total"] = max(all_money), 0.4     # no TOTAL line: guess the largest amount

    for ln in lines:
        if TAX_WORDS.search(ln) and not TOTAL_WORDS.search(ln):
            found = MONEY.findall(ln)
            if found:
                fields["tax"], conf["tax"] = _amount(found[-1]), 0.8
                break

    for ln in lines:
        m = None
        for m in MONEY.finditer(ln):
            pass                                             # last amount on the line is the line total
        if not m or NOT_ITEM.search(ln):
            continue
        name = ln[: m.start()].strip(" .:-$\t")
        if sum(c.isalpha() for c in name) < 3 or find_date(name):
            continue
        amount = _amount(m.group(1))
        qty = re.match(r"^(\d{1,2})\s*[xX@]?\s+(.*)$", name)
        quantity = float(qty.group(1)) if qty else 1.0
        fields["items"].append({"name": qty.group(2) if qty else name, "quantity": quantity,
                                "unit_amount": round(amount / quantity, 2), "amount": amount})
    if fields["items"]:
        conf["items"] = 0.7
        subtotal = sum(i["amount"] for i in fields["items"])
        if fields["total"] and abs(subtotal + (fields["tax"] or 0) - fields["total"]) <= 0.05:
            conf["items"] = 0.85                             # lines add up to the total
            conf["total"] = max(conf["total"], 0.9)
    return {"fields": fields, "confidence": conf, "engine": "ocr"}


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages[:2])


def _image(data: bytes, mime: str):
    from PIL import Image

    if mime == "application/pdf":
        import pypdfium2

        return pypdfium2.PdfDocument(data)[0].render(scale=2.5).to_pil()
    return Image.open(io.BytesIO(data))


def ocr_text(data: bytes, mime: str) -> str:
    """Tesseract on a cleaned-up image. Raises if Tesseract is not installed."""
    import pytesseract
    from PIL import ImageOps

    img = ImageOps.grayscale(_image(data, mime))
    if img.width < 1200:                                     # small photos OCR much better upscaled
        scale = 1200 / img.width
        img = img.resize((1200, int(img.height * scale)))
    img = ImageOps.autocontrast(img).point(lambda p: 255 if p > 150 else 0)
    return pytesseract.image_to_string(img, config="--psm 6")


def extract_receipt(file_bytes: bytes, mime: str) -> dict:
    """Returns {"fields": {...}, "confidence": {...}, "engine": "ocr"}; empty fields if nothing can be read."""
    text = ""
    if mime == "application/pdf":
        try:
            text = _pdf_text(file_bytes)                     # digital PDFs need no OCR at all
        except Exception:
            text = ""
    if len(text.strip()) < 20:
        try:
            text = ocr_text(file_bytes, mime)
        except Exception:                                    # Tesseract missing or unreadable file
            return {"fields": {}, "confidence": {}, "engine": "ocr", "unavailable": True}
    return parse_receipt_text(text)
