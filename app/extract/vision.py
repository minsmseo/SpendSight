"""Receipt extraction: LLM vision -> Tesseract + regex -> (caller shows the manual form)."""
from pydantic import BaseModel, Field, ValidationError

from app.ai import prompts
from app.ai import provider as ai_provider
from app.ai.provider import ProviderError
from app.extract import ocr

_num = {"type": "number"}
_str = {"type": "string"}
RECEIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "vendor": _str, "date": _str, "currency": _str, "total": _num, "tax": _num,
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": _str, "quantity": _num, "unit_amount": _num, "amount": _num},
            "required": ["name", "quantity", "unit_amount", "amount"], "additionalProperties": False,
        }},
    },
    "required": ["vendor", "date", "currency", "total", "tax", "items"], "additionalProperties": False,
}


class ReceiptItem(BaseModel):
    name: str
    quantity: float = 1
    unit_amount: float = 0
    amount: float = 0


class Receipt(BaseModel):
    vendor: str = ""
    date: str = ""
    currency: str = "USD"
    total: float = 0
    tax: float = 0
    items: list[ReceiptItem] = Field(default_factory=list)


def _from_llm(data: dict) -> dict:
    r = Receipt.model_validate(data)
    items = [i.model_dump() for i in r.items if i.name.strip() and i.amount > 0]
    fields = {"vendor": r.vendor.strip(), "date": r.date.strip()[:10], "total": r.total or None,
              "tax": r.tax or 0, "items": items}
    # The model returns "" / 0 for anything it could not read; those fields are marked low confidence.
    confidence = {"vendor": 0.9 if fields["vendor"] else 0.2, "date": 0.9 if len(fields["date"]) == 10 else 0.2,
                  "total": 0.9 if r.total > 0 else 0.2, "tax": 0.8, "items": 0.9 if items else 0.3}
    if items and r.total > 0 and abs(sum(i["amount"] for i in items) + r.tax - r.total) > 0.05:
        confidence["items"] = confidence["total"] = 0.5      # lines do not add up to the total: ask the user to check
    return {"fields": fields, "confidence": confidence, "engine": "llm"}


def extract_receipt(file_bytes: bytes, mime: str) -> dict:
    """Returns {"fields": {...}, "confidence": {...}, "engine": "llm" | "ocr"}. Never raises."""
    provider = ai_provider.get_provider()
    if provider is not None:
        try:
            return _from_llm(provider.vision_json(file_bytes, mime, prompts.VISION_RECEIPT, RECEIPT_SCHEMA))
        except ProviderError as exc:
            ai_provider.report_failure(exc)
        except (ValidationError, Exception):
            pass
    try:
        return ocr.extract_receipt(file_bytes, mime)
    except Exception:
        return {"fields": {}, "confidence": {}, "engine": "ocr"}
