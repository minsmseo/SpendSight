"""AI key (server memory only) and provider / PayPal status (CLAUDE.md 4.1, screen 8)."""
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app import config
from app.ai import provider as ai_provider
from app.web import page

router = APIRouter()


@router.get("/settings")
def settings_page(request: Request):
    return page(
        request,
        "settings.html",
        local_summarizer=config.ENABLE_LOCAL_SUMMARIZER,
        anthropic_model=config.ANTHROPIC_MODEL,
        openai_model=config.OPENAI_MODEL,
    )


@router.post("/settings/ai-key")
def set_ai_key(provider: str = Form(...), key: str = Form("")):
    """The key is kept in process memory for this server session only. Never logged or stored."""
    if provider in ("anthropic", "openai"):
        ai_provider.set_session_key(provider, key)
    return RedirectResponse("/settings", status_code=303)


@router.get("/api/status")
def api_status():
    return {"ai": ai_provider.status(), "paypal": config.PAYPAL_MODE_LABEL}

# TODO(step 5): POST /api/settings/ai-test (one tiny completion to validate the key).
