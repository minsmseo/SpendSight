from app import config


_client = None


def get_paypal_client():
    """Real sandbox client when credentials are set, otherwise the in-memory mock."""
    global _client
    if _client is None:
        if config.PAYPAL_MOCK:
            from app.paypal.mock import MockPayPalClient

            _client = MockPayPalClient()
        else:
            from app.paypal.client import PayPalClient

            _client = PayPalClient(
                config.PAYPAL_CLIENT_ID, config.PAYPAL_CLIENT_SECRET, config.PAYPAL_BASE_URL
            )
    return _client
