"""Demo Vendor Store: create / capture / authorize / void (CLAUDE.md 4.2 A).

Done: store page + vendor catalog API.
TODO(step 3): POST /api/paypal/orders (create itemized order, AUTHORIZE above the pre-approval
threshold, custom_id = expense draft id), POST /api/paypal/orders/{id}/capture|authorize
(-> extract.paypal_receipt -> Expense + review), POST /api/expenses/{id}/capture-auth|void-auth.
"""
from fastapi import APIRouter, Request

from app import config
from app.rules import engine
from app.web import page

router = APIRouter()


@router.get("/store")
def store_page(request: Request):
    return page(
        request,
        "store.html",
        vendors=engine.load_vendors(),
        preapproval_threshold=engine.load_default_policy()["preapproval_threshold"],
        mock=config.PAYPAL_MOCK,
    )


@router.get("/api/vendors")
def api_vendors():
    return engine.load_vendors()
