"""In-memory PayPal stand-in with the same interface as PayPalClient.

Selected automatically when PAYPAL_CLIENT_ID is empty so the app runs with one command.
Payout items turn SUCCESS three seconds after the batch is created.
"""
import copy
import secrets
import time
from datetime import datetime, timedelta, timezone

from app.paypal.client import PayPalError, build_order_body

PAYOUT_DELAY_SECONDS = 3


def _id(n: int = 17) -> str:
    return secrets.token_hex(n)[:n].upper()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MockPayPalClient:
    mode = "mock"

    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.authorizations: dict[str, str] = {}   # authorization id -> order id
        self.payouts: dict[str, dict] = {}

    # --- Orders v2 --------------------------------------------------------
    def create_order(self, intent, items, custom_id, invoice_id, description, tax_total=0.0) -> dict:
        order = build_order_body(intent, items, custom_id, invoice_id, description, tax_total)
        order.update({"id": _id(), "status": "CREATED", "create_time": _now()})
        self.orders[order["id"]] = order
        return copy.deepcopy(order)

    def _order(self, order_id: str) -> dict:
        if order_id not in self.orders:
            raise PayPalError(404, {"name": "RESOURCE_NOT_FOUND"})
        return self.orders[order_id]

    def get_order(self, order_id: str) -> dict:
        return copy.deepcopy(self._order(order_id))

    def capture_order(self, order_id: str) -> dict:
        order = self._order(order_id)
        if order["intent"] != "CAPTURE" or order["status"] == "COMPLETED":
            raise PayPalError(422, {"name": "UNPROCESSABLE_ENTITY"})
        unit = order["purchase_units"][0]
        unit["payments"] = {
            "captures": [{"id": _id(), "status": "COMPLETED", "amount": unit["amount"], "create_time": _now()}]
        }
        order["status"] = "COMPLETED"
        return copy.deepcopy(order)

    def authorize_order(self, order_id: str) -> dict:
        order = self._order(order_id)
        if order["intent"] != "AUTHORIZE" or order["status"] == "COMPLETED":
            raise PayPalError(422, {"name": "UNPROCESSABLE_ENTITY"})
        unit = order["purchase_units"][0]
        auth_id = _id()
        unit["payments"] = {
            "authorizations": [{"id": auth_id, "status": "CREATED", "amount": unit["amount"], "create_time": _now()}]
        }
        order["status"] = "COMPLETED"
        self.authorizations[auth_id] = order_id
        return copy.deepcopy(order)

    def _authorization(self, authorization_id: str) -> tuple[dict, dict]:
        order_id = self.authorizations.get(authorization_id)
        if not order_id:
            raise PayPalError(404, {"name": "RESOURCE_NOT_FOUND"})
        payments = self.orders[order_id]["purchase_units"][0]["payments"]
        auth = payments["authorizations"][0]
        if auth["status"] != "CREATED":
            raise PayPalError(422, {"name": "AUTHORIZATION_ALREADY_" + auth["status"]})
        return auth, payments

    def capture_authorization(self, authorization_id: str) -> dict:
        auth, payments = self._authorization(authorization_id)
        auth["status"] = "CAPTURED"
        capture = {"id": _id(), "status": "COMPLETED", "amount": auth["amount"], "create_time": _now()}
        payments.setdefault("captures", []).append(capture)
        return copy.deepcopy(capture)

    def void_authorization(self, authorization_id: str) -> dict:
        auth, _ = self._authorization(authorization_id)
        auth["status"] = "VOIDED"
        return {"id": authorization_id, "status": "VOIDED"}

    # --- Payouts v1 -------------------------------------------------------
    def create_payout(self, sender_batch_id: str, items: list[dict], subject: str = "Expense reimbursement") -> dict:
        if any(p["sender_batch_id"] == sender_batch_id for p in self.payouts.values()):
            raise PayPalError(400, {"name": "USER_BUSINESS_ERROR", "message": "duplicate sender_batch_id"})
        batch_id = _id(13)
        self.payouts[batch_id] = {
            "sender_batch_id": sender_batch_id,
            "created": time.time(),
            "time_created": _now(),
            "items": [
                {
                    "payout_item_id": _id(13),
                    "receiver": i["receiver_email"],
                    "amount": float(i["amount"]),
                    "sender_item_id": str(i["sender_item_id"]),
                }
                for i in items
            ],
        }
        return {
            "batch_header": {
                "payout_batch_id": batch_id,
                "batch_status": "PENDING",
                "sender_batch_header": {"sender_batch_id": sender_batch_id},
            }
        }

    def get_payout(self, batch_id: str) -> dict:
        p = self.payouts.get(batch_id)
        if not p:
            raise PayPalError(404, {"name": "RESOURCE_NOT_FOUND"})
        done = time.time() - p["created"] >= PAYOUT_DELAY_SECONDS
        total = sum(i["amount"] for i in p["items"])
        return {
            "batch_header": {
                "payout_batch_id": batch_id,
                "batch_status": "SUCCESS" if done else "PROCESSING",
                "time_created": p["time_created"],
                "sender_batch_header": {"sender_batch_id": p["sender_batch_id"]},
                "amount": {"currency": "USD", "value": f"{total:.2f}"},
            },
            "items": [
                {
                    "payout_item_id": i["payout_item_id"],
                    "transaction_status": "SUCCESS" if done else "PENDING",
                    "payout_batch_id": batch_id,
                    "payout_item": {
                        "receiver": i["receiver"],
                        "amount": {"currency": "USD", "value": f"{i['amount']:.2f}"},
                        "sender_item_id": i["sender_item_id"],
                    },
                }
                for i in p["items"]
            ],
        }

    # --- Transaction Search / Webhooks -------------------------------------
    def search_transactions(self, start_date: str, end_date: str, page: int = 1) -> dict:
        """Sample account history: purchases made outside the app, a payout, a pending payment, and
        the orders captured through this mock (so duplicate detection has something to find)."""
        start = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc).replace(hour=18, minute=5, second=0, microsecond=0)

        def tx(tx_id, days_ago, value, code="T0006", status="S", name="", subject="", items=()):
            when = now - timedelta(days=days_ago)
            return when, {
                "transaction_info": {
                    "transaction_id": tx_id, "transaction_event_code": code, "transaction_status": status,
                    "transaction_initiation_date": when.strftime("%Y-%m-%dT%H:%M:%S+0000"),
                    "transaction_amount": {"currency_code": "USD", "value": f"{value:.2f}"},
                    "transaction_subject": subject,
                },
                "payer_info": {"payer_name": {"alternate_full_name": name}},
                "cart_info": {"item_details": [
                    {"item_name": n, "item_quantity": str(q), "item_unit_price": {"currency_code": "USD", "value": f"{u:.2f}"},
                     "item_amount": {"currency_code": "USD", "value": f"{u * q:.2f}"}} for n, q, u in items]},
            }

        rows = [
            tx("MOCKTX0001", 2, -149.00, name="CloudStack SaaS", items=[("Analytics add-on subscription (monthly)", 1, 149.00)]),
            tx("MOCKTX0002", 5, -412.60, name="SkyAir", items=[("Economy airfare SFO to SEA (round trip)", 1, 412.60)]),
            tx("MOCKTX0003", 9, -23.40, name="Coffee & Co", subject="Coffee & Co order"),
            tx("MOCKTX0004", 3, -93.00, code="T0001", name="Alice Chen", subject="Expense reimbursement"),
            tx("MOCKTX0005", 1, -58.00, status="P", name="Office Depotish", items=[("Toner cartridge", 1, 58.00)]),
        ]
        for order in self.orders.values():
            unit = order["purchase_units"][0]
            for capture in unit.get("payments", {}).get("captures", []):
                when = datetime.strptime(capture["create_time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                rows.append((when, {
                    "transaction_info": {
                        "transaction_id": capture["id"], "transaction_event_code": "T0006", "transaction_status": "S",
                        "transaction_initiation_date": when.strftime("%Y-%m-%dT%H:%M:%S+0000"),
                        "transaction_amount": capture["amount"], "custom_field": unit.get("custom_id", ""),
                        "invoice_id": unit.get("invoice_id", ""),
                    },
                    "payer_info": {"payer_name": {"alternate_full_name": "Sandbox Buyer"}},
                    "cart_info": {"item_details": []},
                }))
        found = [detail for when, detail in rows if start <= when <= end]
        return {"transaction_details": found if page == 1 else [], "total_items": len(found), "total_pages": 1, "page": page,
                "last_refreshed_datetime": now.strftime("%Y-%m-%dT%H:%M:%S+0000")}

    def verify_webhook_signature(self, headers: dict, event: dict, webhook_id: str) -> bool:
        return True  # mock mode: nothing to verify against
