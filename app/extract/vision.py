"""LLM vision receipt extraction. TODO(step 5). Falls through to extract/ocr.py on any failure."""
from app.ai import provider as ai_provider
from app.extract import ocr


def extract_receipt(file_bytes: bytes, mime: str) -> dict:
    """Extraction order: LLM vision -> Tesseract + regex -> (caller shows the manual form)."""
    provider = ai_provider.get_provider()
    if provider is not None:
        try:
            # TODO(step 5): provider.vision_json(file_bytes, mime, prompts.VISION_RECEIPT, schema)
            raise ai_provider.ProviderError("other", "vision lands in build step 5")
        except Exception:
            pass
    return ocr.extract_receipt(file_bytes, mime)
