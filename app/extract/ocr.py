"""Tesseract OCR + regex receipt parser (no-AI fallback). TODO(step 6).

Plan: Pillow preprocess (grayscale, threshold, upscale small images); PDF first page via
pypdfium2; pytesseract.image_to_string; regex for total (TOTAL|AMOUNT DUE|BALANCE, largest
money value nearby), tax, date (several US formats), vendor (first uppercase-ish line) and
items (`name ... $12.34`). Returns per-field confidence so the form can highlight weak fields.
"""


def extract_receipt(file_bytes: bytes, mime: str) -> dict:
    """Returns {"fields": {...}, "confidence": {...}, "engine": "ocr"}; empty fields if OCR is unavailable."""
    # TODO(step 6)
    return {"fields": {}, "confidence": {}, "engine": "ocr"}
