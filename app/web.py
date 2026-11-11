"""Shared template helper: injects app name, role and engine badges into every page."""
from fastapi import Request
from fastapi.templating import Jinja2Templates

from app import config
from app.ai import provider as ai_provider

templates = Jinja2Templates(directory=str(config.BASE_DIR / "templates"))



def usd(value) -> str:
    """$1,234.50 (negative as -$5.00); empty for None."""
    if value is None or value == "":
        return ""
    value = float(value)
    return f"{'-' if value < 0 else ''}${abs(value):,.2f}"


templates.env.filters["usd"] = usd

ROLES = ("employee", "finance")


def current_role(request: Request) -> str:
    role = request.cookies.get("role", "finance")
    return role if role in ROLES else "finance"


def page(request: Request, name: str, **ctx):
    base = {
        "app_name": config.APP_NAME,
        "author": config.AUTHOR,
        "role": current_role(request),
        "path": request.url.path,
        "paypal_mode": config.PAYPAL_MODE_LABEL,
        "ai_status": ai_provider.status(),
        "disclaimer": config.TAX_DISCLAIMER,
    }
    base.update(ctx)
    return templates.TemplateResponse(request, name, base)
