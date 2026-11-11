"""Thin PayPal REST client (sandbox). Never logs credentials or Authorization headers.

MockPayPalClient (mock.py) implements the same public methods.
"""
import time
import uuid

import httpx


class PayPalError(Exception):
    def __init__(self, status: int, body):
        super().__init__(f"PayPal API error {status}: {body}")
        self.status = status
        self.body = body


def money(value: float, currency: str = "USD") -> dict:
    return {"currency_code": currency, "value": f"{value:.2f}"}


def build_order_body(
    intent: str,
    items: list[dict],
    custom_id: str,
    invoice_id: str,
    description: str,
    tax_total: float = 0.0,
    currency: str = "USD",
) -> dict:
    """items: [{name, unit_amount, quantity, sku?, paypal_category?}].

    PayPal's item `category` only accepts DIGITAL_GOODS / PHYSICAL_GOODS / DONATION, so the
    expense taxonomy category travels in `sku` and is resolved again on capture.
    """
    item_total = round(sum(i["unit_amount"] * i.get("quantity", 1) for i in items), 2)
    return {
        "intent": intent,
        "purchase_units": [
            {
                "custom_id": str(custom_id),
                "invoice_id": invoice_id,
                "description": description[:127],
                "amount": {
                    **money(item_total + tax_total, currency),
                    "breakdown": {
                        "item_total": money(item_total, currency),
                        "tax_total": money(tax_total, currency),
                    },
                },
                "items": [
                    {
                        "name": i["name"][:127],
                        "quantity": str(int(i.get("quantity", 1))),
                        "unit_amount": money(i["unit_amount"], currency),
                        "sku": i.get("sku", "")[:127],
                        "category": i.get("paypal_category", "PHYSICAL_GOODS"),
                    }
                    for i in items
                ],
            }
        ],
    }


class PayPalClient:
    mode = "sandbox"

    def __init__(self, client_id: str, client_secret: str, base_url: str):
        self._id = client_id
        self._secret = client_secret
        self._http = httpx.Client(base_url=base_url, timeout=30)
        self._token: str | None = None
        self._token_exp = 0.0

    # --- auth -------------------------------------------------------------
    def _access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = self._http.post(
            "/v1/oauth2/token",
            auth=(self._id, self._secret),
            data={"grant_type": "client_credentials"},
        )
        if r.status_code >= 400:
            raise PayPalError(r.status_code, "token request failed")
        data = r.json()
        self._token = data["access_token"]
        self._token_exp = time.time() + int(data.get("expires_in", 3000))
        return self._token

    def _request(self, method: str, path: str, *, json=None, params=None, idempotent=False) -> dict:
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        if idempotent:
            headers["PayPal-Request-Id"] = uuid.uuid4().hex
        r = self._http.request(method, path, json=json, params=params, headers=headers)
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = r.text
            raise PayPalError(r.status_code, body)
        return r.json() if r.content else {}

    # --- Orders v2 --------------------------------------------------------
    def create_order(self, intent, items, custom_id, invoice_id, description, tax_total=0.0) -> dict:
        body = build_order_body(intent, items, custom_id, invoice_id, description, tax_total)
        return self._request("POST", "/v2/checkout/orders", json=body, idempotent=True)

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/v2/checkout/orders/{order_id}")

    def capture_order(self, order_id: str) -> dict:
        return self._request("POST", f"/v2/checkout/orders/{order_id}/capture", json={}, idempotent=True)

    def authorize_order(self, order_id: str) -> dict:
        return self._request("POST", f"/v2/checkout/orders/{order_id}/authorize", json={}, idempotent=True)

    def capture_authorization(self, authorization_id: str) -> dict:
        return self._request(
            "POST", f"/v2/payments/authorizations/{authorization_id}/capture", json={}, idempotent=True
        )

    def void_authorization(self, authorization_id: str) -> dict:
        return self._request("POST", f"/v2/payments/authorizations/{authorization_id}/void", json={})

    # --- Payouts v1 -------------------------------------------------------
    def create_payout(self, sender_batch_id: str, items: list[dict], subject: str = "Expense reimbursement") -> dict:
        """items: [{receiver_email, amount, sender_item_id, note?}]"""
        body = {
            "sender_batch_header": {"sender_batch_id": sender_batch_id, "email_subject": subject},
            "items": [
                {
                    "recipient_type": "EMAIL",
                    "receiver": i["receiver_email"],
                    "amount": {"currency": "USD", "value": f"{i['amount']:.2f}"},
                    "sender_item_id": str(i["sender_item_id"])[:63],
                    "note": i.get("note", "Expense reimbursement"),
                }
                for i in items
            ],
        }
        return self._request("POST", "/v1/payments/payouts", json=body)

    def get_payout(self, batch_id: str) -> dict:
        return self._request("GET", f"/v1/payments/payouts/{batch_id}")

    # --- Transaction Search v1 (backfill only; max 31 days per call) -------
    def search_transactions(self, start_date: str, end_date: str, page: int = 1) -> dict:
        """One page of account history. Sandbox data can lag by three hours or more."""
        return self._request(
            "GET",
            "/v1/reporting/transactions",
            params={"start_date": start_date, "end_date": end_date, "fields": "all", "page_size": 100, "page": page},
        )

    # --- Webhooks ---------------------------------------------------------
    def verify_webhook_signature(self, headers: dict, event: dict, webhook_id: str) -> bool:
        h = {k.lower(): v for k, v in headers.items()}
        body = {
            "auth_algo": h.get("paypal-auth-algo"),
            "cert_url": h.get("paypal-cert-url"),
            "transmission_id": h.get("paypal-transmission-id"),
            "transmission_sig": h.get("paypal-transmission-sig"),
            "transmission_time": h.get("paypal-transmission-time"),
            "webhook_id": webhook_id,
            "webhook_event": event,
        }
        try:
            res = self._request("POST", "/v1/notifications/verify-webhook-signature", json=body)
        except PayPalError:
            return False
        return res.get("verification_status") == "SUCCESS"
