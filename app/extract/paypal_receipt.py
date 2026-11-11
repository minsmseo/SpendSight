"""Orders v2 order (after capture/authorize) -> normalized receipt dict. No OCR needed."""
from datetime import datetime, timezone


def _f(money: dict | None) -> float:
    return float((money or {}).get("value", 0) or 0)


def receipt_from_order(order: dict) -> dict:
    unit = order["purchase_units"][0]
    payments = unit.get("payments", {})
    captures = payments.get("captures") or []
    auths = payments.get("authorizations") or []
    when = (captures or auths or [{}])[0].get("create_time") or order.get("create_time")
    purchased_at = (
        datetime.strptime(when, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if when else None
    )
    breakdown = unit.get("amount", {}).get("breakdown", {})
    return {
        "source": "paypal",
        "vendor": unit.get("description", ""),
        "purchased_at": purchased_at,
        "currency": unit.get("amount", {}).get("currency_code", "USD"),
        "total": _f(unit.get("amount")),
        "tax_amount": _f(breakdown.get("tax_total")),
        "custom_id": unit.get("custom_id"),
        "paypal_order_id": order.get("id"),
        "paypal_capture_id": captures[0]["id"] if captures else None,
        "paypal_authorization_id": auths[0]["id"] if auths else None,
        "items": [
            {
                "name": i["name"],
                "quantity": float(i.get("quantity", 1)),
                "unit_amount": _f(i.get("unit_amount")),
                "amount": round(_f(i.get("unit_amount")) * float(i.get("quantity", 1)), 2),
                "sku": i.get("sku", ""),
            }
            for i in unit.get("items", [])
        ],
    }


# Transaction Search event codes: T00xx are account-to-account payments; T0001 is a mass payout.
PAYOUT_CODE = "T0001"


def receipt_from_transaction(detail: dict) -> dict:
    """One Transaction Search `transaction_details` entry -> normalized receipt dict, plus the
    fields the importer filters on (`kind`, `completed`, `outgoing`)."""
    info = detail.get("transaction_info") or {}
    code = str(info.get("transaction_event_code") or "")
    value = _f(info.get("transaction_amount"))
    when = info.get("transaction_initiation_date")
    try:
        purchased_at = datetime.strptime(when, "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc) if when else None
    except ValueError:
        purchased_at = None
    payer = detail.get("payer_info") or {}
    counterparty = ((payer.get("payer_name") or {}).get("alternate_full_name") or "").strip()
    subject = (info.get("transaction_subject") or info.get("transaction_note") or "").strip()
    items = []
    for it in (detail.get("cart_info") or {}).get("item_details") or []:
        quantity = float(it.get("item_quantity") or 1)
        unit = _f(it.get("item_unit_price"))
        amount = _f(it.get("item_amount")) or round(unit * quantity, 2)
        if it.get("item_name") and amount:
            items.append({"name": it["item_name"], "quantity": quantity, "unit_amount": unit or amount, "amount": amount})
    total = round(abs(value), 2)
    vendor = counterparty or subject or payer.get("email_address") or "PayPal transaction"
    if not items:
        items = [{"name": subject or f"{vendor} purchase", "quantity": 1.0, "unit_amount": total, "amount": total}]
    return {
        "source": "import",
        "transaction_id": str(info.get("transaction_id") or ""),
        "kind": "payout" if code == PAYOUT_CODE else ("payment" if code.startswith("T00") else "other"),
        "completed": info.get("transaction_status") == "S",
        "outgoing": value < 0,
        "vendor": vendor,
        "purchased_at": purchased_at,
        "currency": (info.get("transaction_amount") or {}).get("currency_code", "USD"),
        "total": total,
        "custom_id": str(info.get("custom_field") or ""),
        "invoice_id": str(info.get("invoice_id") or ""),
        "items": items,
    }
