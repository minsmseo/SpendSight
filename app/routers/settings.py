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



@router.post("/api/settings/ai-test")
def ai_test():
    """One tiny completion to check the configured key and model. Never returns the key."""
    name = ai_provider.configured_provider()
    if not name:
        return {"ok": False, "error": "No AI key is set. The rule engine handles everything."}
    provider = ai_provider.build_provider(name)
    if provider is None:
        return {"ok": False, "provider": name, "error": "Could not initialise the client."}
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
    try:
        provider.complete_json("You are a connectivity check.", 'Reply with {"ok": true}.', schema, timeout=30)
    except ai_provider.ProviderError as exc:
        kind = ai_provider.report_failure(exc)
        return {"ok": False, "provider": name, "model": provider.model,
                "error": f"{ai_provider.REASON_LABELS[kind]}: {str(exc)[:200]}"}
    except Exception as exc:
        return {"ok": False, "provider": name, "model": provider.model, "error": type(exc).__name__}
    ai_provider.mark_degraded("", seconds=0)          # a working key clears an earlier degraded state
    return {"ok": True, "provider": name, "model": provider.model}
