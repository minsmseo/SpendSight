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
