"""PayPal webhook receiver (CLAUDE.md section 10).

Every event is verified with PayPal (verify-webhook-signature), stored once (idempotent on the
event id) and then dispatched. Unverified events are stored for audit and never acted on.
Everything here is also reachable without webhooks (synchronous capture responses, payout
polling), so a missed or delayed event never blocks a flow.
"""
import json
import logging

from fastapi import APIRouter, Depends, Request
from sqlmodel import Session, select

from app import config
from app.db import get_session
from app.models import Expense, PayoutBatch, PayoutItem, WebhookEvent
from app.paypal import get_paypal_client

router = APIRouter()
log = logging.getLogger("spendsight.webhooks")

SUBSCRIBED = (
    "CHECKOUT.ORDER.APPROVED", "PAYMENT.CAPTURE.COMPLETED", "PAYMENT.AUTHORIZATION.CREATED",
    "PAYMENT.AUTHORIZATION.VOIDED", "PAYMENT.PAYOUTSBATCH.SUCCESS", "PAYMENT.PAYOUTSBATCH.DENIED",
    "PAYMENT.PAYOUTS-ITEM.SUCCEEDED", "PAYMENT.PAYOUTS-ITEM.FAILED", "PAYMENT.PAYOUTS-ITEM.UNCLAIMED",
)


def _order_id(resource: dict) -> str | None:
    return ((resource.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id")


def _expense_for_payment(session: Session, resource: dict) -> Expense | None:
    order_id = _order_id(resource)
    if order_id:
        found = session.exec(select(Expense).where(Expense.paypal_order_id == order_id)).first()
        if found:
            return found
    custom_id = str(resource.get("custom_id") or "")
    return session.get(Expense, int(custom_id)) if custom_id.isdigit() else None


def _payment_completed(session: Session, resource: dict, field: str) -> str:
    """Capture completed / authorization created. If the browser never came back after the buyer
    approved, the expense is still a draft: build it now from the order."""
    from app.routers.paypal_checkout import finalize_order

    expense = _expense_for_payment(session, resource)
    if not expense:
        return "no matching expense"
    if expense.status == "draft" and expense.paypal_order_id:
        finalize_order(session, expense, get_paypal_client().get_order(expense.paypal_order_id))
        return f"expense {expense.id} created from webhook"
    if not getattr(expense, field) and resource.get("id"):
        setattr(expense, field, resource["id"])
        session.add(expense)
        session.commit()
    return f"expense {expense.id} already recorded"


def _authorization_voided(session: Session, resource: dict) -> str:
    expense = session.exec(select(Expense).where(Expense.paypal_authorization_id == resource.get("id"))).first()
    if expense and expense.status == "held":             # voided or expired at PayPal: nothing left to capture
        expense.status = "rejected"
        session.add(expense)
        session.commit()
        return f"expense {expense.id} rejected (authorization voided)"
    return "no held expense for that authorization"


def _payout_batch(session: Session, resource: dict) -> str:
    from app.routers.reimburse import sync_batch

    header = resource.get("batch_header") or {}
    batch = session.exec(select(PayoutBatch).where(PayoutBatch.paypal_batch_id == header.get("payout_batch_id"))).first()
    if not batch:
        return "unknown payout batch"
    sync_batch(session, batch, {"batch_header": header, "items": resource.get("items") or []})
    return f"batch {batch.id} is {batch.status}"


def _payout_item(session: Session, resource: dict) -> str:
    from app.routers.reimburse import apply_item_status, find_item

    batch = session.exec(select(PayoutBatch).where(PayoutBatch.paypal_batch_id == resource.get("payout_batch_id"))).first()
    if not batch:
        return "unknown payout batch"
    sender_id = (resource.get("payout_item") or {}).get("sender_item_id")
    item = find_item(session, batch, resource.get("payout_item_id"), sender_id)
    if not item:
        return "unknown payout item"
    apply_item_status(session, item, resource.get("transaction_status"), resource.get("payout_item_id"))
    session.commit()
    return f"payout item {item.id} is {item.status}"


def dispatch(session: Session, event_type: str, resource: dict) -> str:
    if event_type == "PAYMENT.CAPTURE.COMPLETED":
        return _payment_completed(session, resource, "paypal_capture_id")
    if event_type == "PAYMENT.AUTHORIZATION.CREATED":
        return _payment_completed(session, resource, "paypal_authorization_id")
    if event_type == "PAYMENT.AUTHORIZATION.VOIDED":
        return _authorization_voided(session, resource)
    if event_type.startswith("PAYMENT.PAYOUTSBATCH."):
        return _payout_batch(session, resource)
    if event_type.startswith("PAYMENT.PAYOUTS-ITEM."):
        return _payout_item(session, resource)
    return "stored"                                      # e.g. CHECKOUT.ORDER.APPROVED: the browser completes the order


@router.post("/webhooks/paypal")
async def paypal_webhook(request: Request, session: Session = Depends(get_session)):
    try:
        event = json.loads(await request.body())
    except ValueError:
        return {"ok": False, "error": "invalid json"}
    if not isinstance(event, dict) or not str(event.get("id", "")):
        return {"ok": False, "error": "missing event id"}
    event_id = str(event["id"])
    if session.exec(select(WebhookEvent).where(WebhookEvent.event_id == event_id)).first():
        return {"ok": True, "duplicate": True}

    verified = get_paypal_client().verify_webhook_signature(dict(request.headers), event, config.PAYPAL_WEBHOOK_ID)
    event_type = str(event.get("event_type", ""))
    session.add(WebhookEvent(event_id=event_id, event_type=event_type, verified=verified, payload_json=json.dumps(event)))
    session.commit()
    if not verified:
        return {"ok": True, "verified": False}
    try:
        action = dispatch(session, event_type, event.get("resource") or {})
    except Exception as exc:                             # never make PayPal retry forever on our bug
        session.rollback()
        log.warning("webhook %s failed: %s", event_type, type(exc).__name__)
        action = "error"
    return {"ok": True, "verified": True, "action": action}
