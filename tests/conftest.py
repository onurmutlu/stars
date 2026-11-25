import json
import time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from app import initdata
from app.config import Product, Settings
from app.main import create_app
from app.store import Store

TOKEN = "123456:TEST-token"
SECRET = "s" * 40
BUYER = 1001
OTHER = 2002
ADMIN = 9009


class FakeAPI:
    """Records Bot API calls; set .fail[method] = n to make the next n calls raise."""

    def __init__(self):
        self.calls = []
        self.fail = {}

    async def __call__(self, method, **payload):
        self.calls.append((method, payload))
        if self.fail.get(method):
            self.fail[method] -= 1
            raise RuntimeError(f"{method} failed")
        if method == "createInvoiceLink":
            return f"https://t.me/$invoice-{payload['payload']}"
        return True

    def of(self, method):
        return [p for m, p in self.calls if m == method]


def make_init_data(user_id=BUYER, auth_date=None, token=TOKEN):
    fields = {
        "auth_date": str(int(auth_date if auth_date is not None else time.time())),
        "query_id": "AAH",
        "user": json.dumps({"id": user_id, "first_name": "Test", "language_code": "tr"}, separators=(",", ":")),
    }
    fields["hash"] = initdata.sign(fields, token)
    return urlencode(fields)


PRODUCTS = {
    "guide": Product("guide", "Rehber", "PDF rehber", 50, "https://example.com/guide.pdf"),
    "club": Product("club", "Kulüp", "Aylık kulüp", 100, "https://t.me/+club", kind="subscription"),
}


def make_settings(**overrides):
    values = dict(bot_token=TOKEN, webhook_secret=SECRET, webapp_url="https://shop.example.com",
                  support_text="destek@example.com", terms_text="Dijital ürün, iade 7 gün.",
                  sales_enabled=True, admin_ids=frozenset({ADMIN}))
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def api():
    return FakeAPI()


@pytest.fixture
def store():
    return Store(":memory:")


@pytest.fixture
def client(api, store):
    app = create_app(make_settings(), PRODUCTS, store, api, background=False)
    with TestClient(app) as test_client:
        test_client.shop = app.state.shop
        yield test_client


def headers(user_id=BUYER):
    return {"X-Telegram-Init-Data": make_init_data(user_id)}


def hook(client, update):
    return client.post("/telegram/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})


def pre_checkout(order_id, user_id=BUYER, amount=50, currency="XTR", qid="q1"):
    return {"update_id": 1, "pre_checkout_query": {
        "id": qid, "from": {"id": user_id}, "currency": currency, "total_amount": amount, "invoice_payload": order_id}}


def paid(order_id, user_id=BUYER, amount=50, charge="ch-1", update_id=2, **extra):
    payment = {"currency": "XTR", "total_amount": amount, "invoice_payload": order_id,
               "telegram_payment_charge_id": charge, **extra}
    return {"update_id": update_id, "message": {
        "message_id": 5, "chat": {"id": user_id, "type": "private"}, "from": {"id": user_id},
        "successful_payment": payment}}


def text(message, user_id=BUYER, update_id=3):
    return {"update_id": update_id, "message": {
        "message_id": 6, "chat": {"id": user_id, "type": "private"}, "from": {"id": user_id}, "text": message}}
