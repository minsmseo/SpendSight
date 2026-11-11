"""Demo Vendor Store: buy through PayPal, receipt arrives automatically (CLAUDE.md 4.2 A).

quote -> create order (itemized, custom_id = expense draft id) -> buyer approves in the PayPal JS
SDK (or "Simulate" in mock mode) -> complete (capture, or authorize when above the pre-approval
threshold) -> receipt built from the order -> review pipeline. Finance then captures or voids a
held authorization.
"""
import json
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app import config
from app.ai import agent
from app.db import get_session
from app.extract.paypal_receipt import receipt_from_order
from app.models import Company, Employee, Expense, LineItem, utcnow
from app.paypal import get_paypal_client
from app.paypal.client import PayPalError
from app.rules import engine
from app.web import page

router = APIRouter()


class CartItem(BaseModel):
    sku: str
    quantity: int = Field(default=1, ge=1, le=20)


class Cart(BaseModel):
    employee_id: int = 1
    vendor_id: str
    items: list[CartItem]
    purpose: str = ""
    attendees: str = ""


def _policy(session: Session) -> dict:
    company = session.exec(select(Company)).first()
    return json.loads(company.policy_json or "{}") or engine.load_default_policy()


def _price_cart(cart: Cart, policy: dict) -> dict:
    """Server-side pricing from the catalog; the browser never sets prices or the intent."""
    vendor = next((v for v in engine.load_vendors() if v["id"] == cart.vendor_id), None)
    if not vendor:
        raise HTTPException(400, "Unknown vendor")
    if not cart.items:
        raise HTTPException(400, "Cart is empty")
    catalog = {i["sku"]: i for i in vendor["items"]}
    lines = []
    for ci in cart.items:
        item = catalog.get(ci.sku)
        if not item:
            raise HTTPException(400, f"Unknown item {ci.sku}")
        lines.append({
            "name": item["name"], "sku": item["sku"], "unit_amount": item["price"], "quantity": ci.quantity,
            "paypal_category": vendor["paypal_category"],
        })
    item_total = round(sum(ln["unit_amount"] * ln["quantity"] for ln in lines), 2)
    tax = round(item_total * vendor.get("tax_rate", 0.0), 2)
    total = round(item_total + tax, 2)
    hold = engine.needs_preapproval(policy, vendor["category"], item_total, total)
    return {"vendor": vendor, "lines": lines, "item_total": item_total, "tax": tax, "total": total,
            "intent": "AUTHORIZE" if hold else "CAPTURE"}


def _paypal_error(exc: PayPalError) -> HTTPException:
    name = exc.body.get("name") if isinstance(exc.body, dict) else None
    return HTTPException(502, f"PayPal error {exc.status}" + (f": {name}" if name else ""))


@router.get("/store")
def store_page(request: Request, session: Session = Depends(get_session)):
    return page(
        request,
        "store.html",
        vendors=engine.load_vendors(),
        employees=[{"id": e.id, "name": e.name} for e in session.exec(select(Employee)).all()],
        mock=config.PAYPAL_MOCK,
        paypal_client_id="" if config.PAYPAL_MOCK else config.PAYPAL_CLIENT_ID,
    )


@router.get("/api/vendors")
def api_vendors():
    return engine.load_vendors()


@router.post("/api/paypal/quote")
def quote(cart: Cart, session: Session = Depends(get_session)):
    """Total and intent for the current cart, so the page can show the pre-approval notice."""
    priced = _price_cart(cart, _policy(session))
    return {"total": priced["total"], "tax": priced["tax"], "intent": priced["intent"]}


@router.post("/api/paypal/orders")
def create_order(cart: Cart, session: Session = Depends(get_session)):
    if not cart.purpose.strip():
        raise HTTPException(400, "Business purpose is required")
    if not session.get(Employee, cart.employee_id):
        raise HTTPException(400, "Unknown employee")
    priced = _price_cart(cart, _policy(session))
    expense = Expense(
        employee_id=cart.employee_id, source="paypal", status="draft", vendor=priced["vendor"]["name"],
        total=priced["total"], tax_amount=priced["tax"], purpose=cart.purpose.strip(),
        attendees=cart.attendees.strip(), intent=priced["intent"],
    )
    session.add(expense)
    session.flush()
    try:
        order = get_paypal_client().create_order(
            intent=priced["intent"], items=priced["lines"], custom_id=str(expense.id),
            invoice_id=f"SS-{expense.id}-{secrets.token_hex(3)}", description=priced["vendor"]["name"],
            tax_total=priced["tax"],
        )
    except PayPalError as exc:
        session.rollback()
        raise _paypal_error(exc)
    expense.paypal_order_id = order["id"]
    session.add(expense)
    session.commit()
    return {"order_id": order["id"], "expense_id": expense.id, "intent": priced["intent"], "total": priced["total"]}


@router.post("/api/paypal/orders/{order_id}/complete")
def complete_order(order_id: str, session: Session = Depends(get_session)):
    """Called after the buyer approves: capture or authorize, build the receipt, run the review."""
    expense = session.exec(select(Expense).where(Expense.paypal_order_id == order_id)).first()
    if not expense:
        raise HTTPException(404, "Order not found")
    if expense.status != "draft":                      # already completed: idempotent
        return {"expense_id": expense.id, "status": expense.status, "already_completed": True}
    client = get_paypal_client()
    try:
        if expense.intent == "AUTHORIZE":
            client.authorize_order(order_id)
        else:
            client.capture_order(order_id)
        order = client.get_order(order_id)             # full representation: items + payments
    except PayPalError as exc:
        raise _paypal_error(exc)

    result = finalize_order(session, expense, order)
    return {"expense_id": expense.id, "status": result["status"], "decision": result["decision"],
            "engine": result["engine"], "question": result["question"]}


def finalize_order(session: Session, expense: Expense, order: dict) -> dict:
    """Turn a paid (captured or authorized) PayPal order into a reviewed expense. Also used by the
    webhook receiver when the browser never came back after the buyer approved."""
    receipt = receipt_from_order(order)
    expense.vendor = receipt["vendor"] or expense.vendor
    expense.purchased_at = receipt["purchased_at"] or utcnow()
    expense.total, expense.tax_amount = receipt["total"], receipt["tax_amount"]
    expense.paypal_capture_id = receipt["paypal_capture_id"]
    expense.paypal_authorization_id = receipt["paypal_authorization_id"]
    session.add(expense)
    for it in receipt["items"]:
        session.add(LineItem(expense_id=expense.id, name=it["name"], quantity=it["quantity"],
                             unit_amount=it["unit_amount"], amount=it["amount"]))
    session.commit()
    return agent.review(session, expense.id)


def _held(session: Session, expense_id: int) -> Expense:
    expense = session.get(Expense, expense_id)
    if not expense:
        raise HTTPException(404, "Expense not found")
    if expense.status != "held" or not expense.paypal_authorization_id:
        raise HTTPException(409, "Expense has no held PayPal authorization")
    return expense


@router.post("/api/expenses/{expense_id}/capture-auth")
def capture_authorization(expense_id: int, session: Session = Depends(get_session)):
    """Finance approves a held purchase: the authorization is captured."""
    expense = _held(session, expense_id)
    try:
        capture = get_paypal_client().capture_authorization(expense.paypal_authorization_id)
    except PayPalError as exc:
        raise _paypal_error(exc)
    expense.paypal_capture_id = capture.get("id")
    expense.status = "approved"
    expense.substantiated_at = expense.substantiated_at or utcnow()
    session.add(expense)
    session.commit()
    return {"expense_id": expense.id, "status": expense.status, "paypal_capture_id": expense.paypal_capture_id}


@router.post("/api/expenses/{expense_id}/void-auth")
def void_authorization(expense_id: int, session: Session = Depends(get_session)):
    """Finance rejects a held purchase: the authorization is voided and no money moves."""
    expense = _held(session, expense_id)
    try:
        get_paypal_client().void_authorization(expense.paypal_authorization_id)
    except PayPalError as exc:
        raise _paypal_error(exc)
    expense.status = "rejected"
    session.add(expense)
    session.commit()
    return {"expense_id": expense.id, "status": expense.status}
