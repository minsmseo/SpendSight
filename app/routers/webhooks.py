"""PayPal webhook receiver: signature verification + idempotent storage (CLAUDE.md section 10).

TODO(step 7): dispatch stored events (capture completed, authorization voided, payout item
status) to update Expense / PayoutItem rows.
"""
import json

from fastapi import APIRouter, Depends, Request
from sqlmodel import Session, select

from app import config
from app.db import get_session
from app.models import WebhookEvent
from app.paypal import get_paypal_client

router = APIRouter()


@router.post("/webhooks/paypal")
async def paypal_webhook(request: Request, session: Session = Depends(get_session)):
    try:
        event = json.loads(await request.body())
    except ValueError:
        return {"ok": False, "error": "invalid json"}
    event_id = str(event.get("id", ""))
    if not event_id:
        return {"ok": False, "error": "missing event id"}
    if session.exec(select(WebhookEvent).where(WebhookEvent.event_id == event_id)).first():
        return {"ok": True, "duplicate": True}

    verified = get_paypal_client().verify_webhook_signature(
        dict(request.headers), event, config.PAYPAL_WEBHOOK_ID
    )
    session.add(
        WebhookEvent(
            event_id=event_id,
            event_type=str(event.get("event_type", "")),
            verified=verified,
            payload_json=json.dumps(event),
        )
    )
    session.commit()
    # Unverified events are stored for audit but never acted on.
    return {"ok": True, "verified": verified}
