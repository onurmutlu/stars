import asyncio
import time

from app.config import SUBSCRIPTION_PERIOD
from tests.conftest import ADMIN, BUYER, OTHER, headers, hook, paid, pre_checkout, text


def order(client, product="guide", user=BUYER):
    response = client.post("/api/orders", json={"product_id": product}, headers=headers(user))
    assert response.status_code == 200, response.text
    return response.json()["order_id"]


def answers(api):
    return [p["ok"] for p in api.of("answerPreCheckoutQuery")]


# ------------------------------------------------------------------ catalog and auth
def test_catalog_never_exposes_content(client):
    products = client.get("/api/products").json()
    assert {p["id"] for p in products} == {"guide", "club"}
    assert all("content" not in p for p in products)


def test_order_requires_valid_telegram_session(client):
    assert client.post("/api/orders", json={"product_id": "guide"}).status_code == 401
    bad = {"X-Telegram-Init-Data": "auth_date=1&user=%7B%22id%22%3A1%7D&hash=deadbeef"}
    assert client.post("/api/orders", json={"product_id": "guide"}, headers=bad).status_code == 401


def test_unknown_product(client):
    assert client.post("/api/orders", json={"product_id": "nope"}, headers=headers()).status_code == 404


def test_invoice_link_is_xtr_with_order_payload(client, api):
    order_id = order(client)
    (invoice,) = api.of("createInvoiceLink")
    assert invoice["currency"] == "XTR" and invoice["payload"] == order_id
    assert invoice["prices"] == [{"label": "Rehber", "amount": 50}]
    assert "subscription_period" not in invoice and "provider_token" not in invoice


# ------------------------------------------------------------------ happy path
def test_full_purchase_flow(client, api):
    order_id = order(client)
    assert client.get(f"/api/orders/{order_id}", headers=headers()).json()["status"] == "pending"
    assert hook(client, pre_checkout(order_id)).status_code == 200
    assert answers(api) == [True]
    assert hook(client, paid(order_id)).status_code == 200

    status = client.get(f"/api/orders/{order_id}", headers=headers()).json()
    assert status["status"] == "paid" and status["content"] == "https://example.com/guide.pdf"
    (dm,) = api.of("sendMessage")
    assert dm["chat_id"] == BUYER and dm["protect_content"] and "guide.pdf" in dm["text"]
    assert [p["order_id"] for p in client.get("/api/purchases", headers=headers()).json()] == [order_id]


def test_owned_product_cannot_be_bought_twice(client):
    order_id = order(client)
    hook(client, paid(order_id))
    assert client.post("/api/orders", json={"product_id": "guide"}, headers=headers()).status_code == 409


def test_order_is_private_to_its_buyer(client):
    order_id = order(client)
    hook(client, paid(order_id))
    assert client.get(f"/api/orders/{order_id}", headers=headers(OTHER)).status_code == 404
    assert client.get("/api/purchases", headers=headers(OTHER)).json() == []


# ------------------------------------------------------------------ pre-checkout rules
def test_pre_checkout_rejections(client, api):
    order_id = order(client)
    hook(client, pre_checkout(order_id, amount=49, qid="a"))
    hook(client, pre_checkout(order_id, user_id=OTHER, qid="b"))
    hook(client, pre_checkout(order_id, currency="USD", qid="c"))
    hook(client, pre_checkout("missing", qid="d"))
    assert answers(api) == [False, False, False, False]
    assert all(p.get("error_message") for p in api.of("answerPreCheckoutQuery"))


def test_pre_checkout_rejects_expired_and_paid_orders(client, api, store):
    expired = order(client)
    with store.db:
        store.db.execute("UPDATE orders SET created=? WHERE id=?", (time.time() - 7200, expired))
    done = order(client)
    hook(client, paid(done))
    hook(client, pre_checkout(expired, qid="a"))
    hook(client, pre_checkout(done, qid="b"))
    assert answers(api) == [False, False]


def test_sales_closed(api, store):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import PRODUCTS, make_settings

    app = create_app(make_settings(sales_enabled=False), PRODUCTS, store, api, background=False)
    with TestClient(app) as c:
        assert c.get("/api/products").json() == []
        assert c.post("/api/orders", json={"product_id": "guide"}, headers=headers()).status_code == 403


# ------------------------------------------------------------------ payment safety
def test_duplicate_payment_update_is_idempotent(client, api, store):
    order_id = order(client)
    hook(client, paid(order_id))
    hook(client, paid(order_id))
    assert len(api.of("sendMessage")) == 1
    assert store.db.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1


def test_second_charge_for_one_time_order_goes_to_operator(client, store):
    order_id = order(client)
    hook(client, paid(order_id, charge="ch-1"))
    hook(client, paid(order_id, charge="ch-2", update_id=9))
    assert [(i["charge"], i["reason"]) for i in store.open_issues()] == [("ch-2", "order_already_paid")]


def test_unmatched_payments_are_kept_not_dropped(client, store):
    order_id = order(client)
    hook(client, paid("unknown", charge="x1"))
    hook(client, paid(order_id, amount=1, charge="x2", update_id=7))
    hook(client, paid(order_id, user_id=OTHER, charge="x3", update_id=8))
    assert {i["charge"] for i in store.open_issues()} == {"x1", "x2", "x3"}
    assert client.get(f"/api/orders/{order_id}", headers=headers()).json()["status"] == "pending"


def test_failed_dm_does_not_lose_payment_and_is_redelivered(client, api, store):
    order_id = order(client)
    api.fail["sendMessage"] = 1
    assert hook(client, paid(order_id)).status_code == 200
    assert client.get(f"/api/orders/{order_id}", headers=headers()).json()["status"] == "paid"
    asyncio.run(client.shop.redeliver(now=time.time() + 600))
    assert len(api.of("sendMessage")) == 2
    assert store.order(order_id)["delivered"] == 1


def test_delivery_gives_up_after_max_attempts(client, api, store):
    order_id = order(client)
    api.fail["sendMessage"] = 100
    hook(client, paid(order_id))
    for step in range(1, 6):
        asyncio.run(client.shop.redeliver(now=time.time() + 600 * step))
    assert [i["reason"] for i in store.open_issues()] == ["delivery_failed"]


# ------------------------------------------------------------------ webhook
def test_webhook_requires_secret(client):
    assert client.post("/telegram/webhook", json={"update_id": 1}).status_code == 403
    bad = {"X-Telegram-Bot-Api-Secret-Token": "x" * 40}
    assert client.post("/telegram/webhook", json={"update_id": 1}, headers=bad).status_code == 403


def test_webhook_retries_then_records_failing_payment(client, store, monkeypatch):
    order_id = order(client)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(client.shop, "_successful_payment", boom)
    update = paid(order_id, charge="ch-err", update_id=77)
    assert hook(client, update).status_code == 500
    assert hook(client, update).status_code == 500
    assert hook(client, update).status_code == 200
    assert [i["reason"] for i in store.open_issues()] == ["update_failed:RuntimeError"]


def test_group_messages_are_ignored(client, api):
    update = text("/start")
    update["message"]["chat"]["type"] = "group"
    hook(client, update)
    assert api.calls == []


# ------------------------------------------------------------------ refunds
def test_refund_revokes_access(client, api):
    order_id = order(client)
    hook(client, paid(order_id, charge="ch-r"))
    assert asyncio.run(client.shop.refund("ch-r")) is True
    assert asyncio.run(client.shop.refund("ch-r")) is False
    assert api.of("refundStarPayment") == [{"user_id": BUYER, "telegram_payment_charge_id": "ch-r"}]
    status = client.get(f"/api/orders/{order_id}", headers=headers()).json()
    assert status["status"] == "refunded" and "content" not in status


def test_refund_made_elsewhere_is_respected(client):
    order_id = order(client)
    hook(client, paid(order_id, charge="ch-t"))
    refund = text("", update_id=11)
    refund["message"].pop("text")
    refund["message"]["refunded_payment"] = {"currency": "XTR", "total_amount": 50,
                                            "invoice_payload": order_id, "telegram_payment_charge_id": "ch-t"}
    hook(client, refund)
    assert client.get(f"/api/orders/{order_id}", headers=headers()).json()["status"] == "refunded"


def test_admin_refund_command_only_for_admins(client, api):
    order_id = order(client)
    hook(client, paid(order_id, charge="ch-a"))
    hook(client, text("/refund ch-a", user_id=BUYER))
    assert api.of("refundStarPayment") == []
    hook(client, text("/refund ch-a", user_id=ADMIN, update_id=12))
    assert len(api.of("refundStarPayment")) == 1
    assert api.of("sendMessage")[-1]["text"] == "İade edildi."


# ------------------------------------------------------------------ subscriptions
def test_subscription_invoice_and_expiry(client, api, store):
    order_id = order(client, "club")
    assert api.of("createInvoiceLink")[0]["subscription_period"] == SUBSCRIPTION_PERIOD
    now = time.time()
    hook(client, paid(order_id, amount=100, charge="s1", is_recurring=True, is_first_recurring=True,
                      subscription_expiration_date=int(now + SUBSCRIPTION_PERIOD)))
    status = client.get(f"/api/orders/{order_id}", headers=headers()).json()
    assert status["status"] == "paid" and status["access_until"] > now
    # a second subscription while one is active is refused
    assert client.post("/api/orders", json={"product_id": "club"}, headers=headers()).status_code == 409
    # renewal extends access
    hook(client, paid(order_id, amount=100, charge="s2", update_id=20, is_recurring=True,
                      subscription_expiration_date=int(now + 2 * SUBSCRIPTION_PERIOD)))
    assert store.access_until(store.order(order_id)) == int(now + 2 * SUBSCRIPTION_PERIOD)
    # once every period has passed, access ends
    assert not store.has_access(store.order(order_id), now=now + 3 * SUBSCRIPTION_PERIOD)


def test_subscription_payment_without_expiry_goes_to_operator(client, store):
    order_id = order(client, "club")
    hook(client, paid(order_id, amount=100, charge="s-bad"))
    assert [i["reason"] for i in store.open_issues()] == ["subscription_without_expiration"]


# ------------------------------------------------------------------ commands
def test_commands(client, api):
    hook(client, text("/start"))
    start = api.of("sendMessage")[-1]
    assert start["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"] == "https://shop.example.com/app"
    hook(client, text("/paysupport", update_id=4))
    assert api.of("sendMessage")[-1]["text"] == "destek@example.com"
    hook(client, text("/terms", update_id=5))
    assert "iade" in api.of("sendMessage")[-1]["text"]


def test_orders_command_resends_active_content(client, api):
    order_id = order(client)
    hook(client, paid(order_id))
    hook(client, text("/orders", update_id=30))
    assert sum("guide.pdf" in p["text"] for p in api.of("sendMessage")) == 2


def test_marketing_site_and_app_are_served(client):
    assert "Stars" in client.get("/").text
    assert "telegram-web-app.js" in client.get("/app").text
    cfg = client.get("/api/config").json()
    assert {"bot_username", "contact_url"} <= set(cfg) and "bot_token" not in cfg
