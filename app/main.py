from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import config
from app.db import init_db
from app.routers import (
    expenses,
    onboarding,
    paypal_checkout,
    reimburse,
    reports,
    settings,
    webhooks,
)
from app.web import ROLES


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    from seed import seed_if_empty

    seed_if_empty()
    config.UPLOAD_DIR.mkdir(exist_ok=True)
    yield


app = FastAPI(title=config.APP_NAME, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "static")), name="static")
config.UPLOAD_DIR.mkdir(exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(config.UPLOAD_DIR)), name="uploads")

for module in (onboarding, expenses, paypal_checkout, webhooks, reimburse, reports, settings):
    app.include_router(module.router)


@app.get("/healthz")
def healthz():
    return {"ok": True, "app": config.APP_NAME}


@app.get("/config")
def public_config():
    """Public, non-secret config for the frontend (PayPal JS SDK client id etc.)."""
    return {
        "app_name": config.APP_NAME,
        "paypal_mode": config.PAYPAL_MODE_LABEL,
        "paypal_client_id": "" if config.PAYPAL_MOCK else config.PAYPAL_CLIENT_ID,
        "currency": "USD",
    }


@app.get("/role/{role}")
def switch_role(role: str):
    role = role if role in ROLES else "finance"
    resp = RedirectResponse("/employee" if role == "employee" else "/finance/queue", status_code=303)
    resp.set_cookie("role", role, samesite="lax")
    return resp
