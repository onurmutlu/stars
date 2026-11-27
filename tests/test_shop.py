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


def test_start_deep_link_opens_the_product(client, api):
    hook(client, text("/start p_guide"))
    message = api.of("sendMessage")[-1]
    assert message["text"].startswith("Rehber")
    assert message["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"] == "https://shop.example.com/app?p=guide"
    hook(client, text("/start p_nope", update_id=4))
    assert api.of("sendMessage")[-1]["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"] == "https://shop.example.com/app"


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


def test_config_admin_link_only_for_admins_and_buy_link_for_all(api, store):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from tests.conftest import PRODUCTS, make_settings
    settings = make_settings(admin_url="https://t.me/HubBot?start=panel", buy_url="https://t.me/HubBot?start=lisans",
                             buy_text="Sen de aç")
    with TestClient(create_app(settings, PRODUCTS, store, api, background=False)) as client:
        assert client.get("/api/config", headers=headers(ADMIN)).json()["admin_url"].endswith("start=panel")
        public = client.get("/api/config", headers=headers(BUYER)).json()
        assert "admin_url" not in public and public["buy"] == {"text": "Sen de aç", "url": "https://t.me/HubBot?start=lisans"}
        assert "admin_url" not in client.get("/api/config").json()


def test_admin_command_lists_admin_links_only_for_admins(api, store):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from tests.conftest import PRODUCTS, make_settings
    settings = make_settings(admin_url="https://t.me/HubBot?start=panel", buy_url="https://t.me/HubBot?start=lisans")
    with TestClient(create_app(settings, PRODUCTS, store, api, background=False)) as client:
        hook(client, text("/admin", user_id=ADMIN))
        rows = api.of("sendMessage")[-1]["reply_markup"]["inline_keyboard"]
        assert [r[0]["text"] for r in rows][:2] == ["⚙️ Yönetim paneli", "🔑 Lisans yönetimi"]
        assert client.get("/api/config", headers=headers(ADMIN)).json()["license_url"].endswith("start=lisans")
        hook(client, text("/admin", update_id=9))
        assert "Yönetim" not in api.of("sendMessage")[-1]["text"]


def test_mini_app_assets_are_cache_busted(client):
    html = client.get("/app").text
    assert '/static/app.js?v=' in html and '/static/style.css?v=' in html



# ------------------------------------------------------------------ channel access, notifications, summary
def channel_client(api, store, **overrides):
    from fastapi.testclient import TestClient
    from app.config import Product
    from app.main import create_app
    from tests.conftest import make_settings
    products = {"vip": Product("vip", "VIP", "Kanal", 100, "Hoş geldin", kind="subscription", channel_id=-1005),
                "arsiv": Product("arsiv", "Arşiv", "Kalıcı", 40, "Arşiv", channel_id=-1006)}
    return TestClient(create_app(make_settings(**overrides), products, store, api, background=False))


def test_channel_subscription_invite_removal_and_renewal(api, store):
    with channel_client(api, store) as client:
        order_id = order(client, "vip")
        now = int(time.time())
        hook(client, paid(order_id, amount=100, charge="c1", subscription_expiration_date=now + 100))
        (invite,) = api.of("createChatInviteLink")
        assert invite["chat_id"] == -1005 and invite["member_limit"] == 1
        assert "https://t.me/+inv0" in api.of("sendMessage")[-1]["text"]
        view = client.get(f"/api/orders/{order_id}", headers=headers()).json()
        assert "https://t.me/+inv0" in view["content"]
        # still active: nobody is removed
        asyncio.run(client.app.state.shop.sweep_channels(now))
        assert not api.of("banChatMember")
        # expired: removed from the channel (ban + unban = kick) and told how to come back
        asyncio.run(client.app.state.shop.sweep_channels(now + 200))
        assert api.of("banChatMember")[0] == {"chat_id": -1005, "user_id": BUYER, "until_date": now + 260}
        assert api.of("unbanChatMember")[0]["only_if_banned"] is True
        assert "sona erdi" in api.of("sendMessage")[-1]["text"]
        asyncio.run(client.app.state.shop.sweep_channels(now + 300))
        assert len(api.of("banChatMember")) == 1
        # renewal after removal: a fresh invite
        hook(client, paid(order_id, amount=100, charge="c2", update_id=7, is_recurring=True,
                          subscription_expiration_date=int(time.time()) + 2592000))
        assert len(api.of("createChatInviteLink")) == 2 and "+inv1" in api.of("sendMessage")[-1]["text"]


def test_refunded_channel_purchase_loses_access(api, store):
    with channel_client(api, store) as client:
        order_id = order(client, "arsiv")
        hook(client, paid(order_id, amount=40, charge="c9"))
        asyncio.run(client.app.state.shop.sweep_channels())
        assert not api.of("banChatMember")
        store.mark_refunded("c9")
        asyncio.run(client.app.state.shop.sweep_channels())
        assert api.of("banChatMember")[0]["chat_id"] == -1006


def test_invite_failure_warns_admin_once_and_retries(api, store):
    with channel_client(api, store) as client:
        api.fail["createChatInviteLink"] = 2
        order_id = order(client, "arsiv")
        hook(client, paid(order_id, amount=40, charge="c3"))
        warnings = [m for m in api.of("sendMessage") if m["chat_id"] == ADMIN]
        assert len(warnings) == 1 and "yönetici" in warnings[0]["text"]
        asyncio.run(client.app.state.shop.redeliver(time.time() + 400))
        asyncio.run(client.app.state.shop.redeliver(time.time() + 800))
        assert "+inv" in api.of("sendMessage")[-1]["text"]
        assert len([m for m in api.of("sendMessage") if m["chat_id"] == ADMIN]) == 1


def test_sale_and_renewal_notifications(api, store):
    with channel_client(api, store, notify_sales=True) as client:
        order_id = order(client, "vip")
        update = paid(order_id, amount=100, charge="n1", subscription_expiration_date=int(time.time()) + 999)
        update["message"]["from"] |= {"first_name": "Ayşe", "username": "ayse"}
        hook(client, update)
        notice = [m for m in api.of("sendMessage") if m["chat_id"] == ADMIN][-1]["text"]
        assert notice.startswith("💰 Yeni satış: VIP · ⭐100") and "@ayse" in notice
        hook(client, paid(order_id, amount=100, charge="n2", update_id=8, is_recurring=True,
                          subscription_expiration_date=int(time.time()) + 2592000))
        assert [m for m in api.of("sendMessage") if m["chat_id"] == ADMIN][-1]["text"].startswith("🔁")


def test_bot_added_to_channel_tells_admin_the_id(api, store):
    with channel_client(api, store) as client:
        hook(client, {"update_id": 40, "my_chat_member": {
            "chat": {"id": -100777, "type": "channel", "title": "VIP Kanal"}, "from": {"id": ADMIN},
            "new_chat_member": {"status": "administrator", "can_invite_users": True, "can_restrict_members": False}}})
        notice = api.of("sendMessage")[-1]
        assert notice["chat_id"] == ADMIN and "-100777" in notice["text"] and "Üyeleri yasakla" in notice["text"]
        assert store.channels()[0]["id"] == -100777


def test_daily_summary_once_a_day_and_only_with_activity(api, store):
    at = time.time() + 3600
    hour = time.localtime(at).tm_hour
    with channel_client(api, store, summary_hour=hour) as client:
        shop = client.app.state.shop
        order_id = order(client, "arsiv")
        hook(client, paid(order_id, amount=40, charge="d1"))
        if hour > 0:
            before = time.mktime(time.localtime(at)[:3] + (hour - 1, 30, 0, 0, 0, -1))
            assert asyncio.run(shop.daily_summary(before)) is False  # before the summary hour
        assert asyncio.run(shop.daily_summary(at)) is True
        text = api.of("sendMessage")[-1]["text"]
        assert "Satış: 1 · ⭐40" in text and "Arşiv" in text
        assert asyncio.run(shop.daily_summary(at + 60)) is False  # once a day


def test_daily_summary_is_silent_without_activity(api, store):
    with channel_client(api, store, summary_hour=0) as client:
        assert asyncio.run(client.app.state.shop.daily_summary()) is False
        assert not api.of("sendMessage")


def test_products_json_edits_apply_without_restart(tmp_path, api, store, monkeypatch):
    import json
    import os
    from fastapi.testclient import TestClient
    from app.main import create_app
    from tests.conftest import make_settings
    path = tmp_path / "products.json"
    item = {"id": "a", "title": "A", "description": "d", "stars": 1, "content": "c"}
    path.write_text(json.dumps([item]))
    with TestClient(create_app(make_settings(products_path=str(path)), None, store, api, background=False)) as client:
        assert [p["id"] for p in client.get("/api/products").json()] == ["a"]
        path.write_text(json.dumps([item, item | {"id": "b", "channel_id": -1001}]))
        os.utime(path, (time.time() + 5, time.time() + 5))
        assert [p["id"] for p in client.get("/api/products").json()] == ["a", "b"]
        path.write_text("{broken")
        os.utime(path, (time.time() + 10, time.time() + 10))
        assert [p["id"] for p in client.get("/api/products").json()] == ["a", "b"]



# ------------------------------------------------------------------ CRM
def test_customer_message_goes_to_admin_and_reply_comes_back(client, api, store):
    update = text("Merhaba, kulüp ne zaman açılıyor?")
    update["message"]["from"] |= {"first_name": "Ali", "username": "ali"}
    hook(client, update)
    to_admin = [m for m in api.of("sendMessage") if m["chat_id"] == ADMIN][-1]
    assert "@ali" in to_admin["text"] and "kulüp ne zaman" in to_admin["text"]
    assert "iletildi" in [m for m in api.of("sendMessage") if m["chat_id"] == BUYER][-1]["text"]
    admin_message_id = store.db.execute("SELECT admin_message FROM inbox").fetchone()[0]
    reply = {"update_id": 20, "message": {"message_id": 30, "chat": {"id": ADMIN, "type": "private"},
             "from": {"id": ADMIN}, "text": "Pazartesi açılıyor!",
             "reply_to_message": {"message_id": admin_message_id}}}
    hook(client, reply)
    assert {"chat_id": BUYER, "text": "Pazartesi açılıyor!"} in api.of("sendMessage")
    assert "iletildi" in api.of("sendMessage")[-1]["text"]


def test_contacts_track_source_and_stage(client, api, store):
    hook(client, text("/start instagram"))
    assert store.customers()[0]["source"] == "instagram" and store.customers()[0]["stage"] == "ziyaretçi"
    order_id = order(client)
    assert store.customers()[0]["stage"] == "sepette"
    hook(client, paid(order_id, update_id=8))
    assert store.customers()[0]["stage"] == "müşteri" and store.customers()[0]["spent"] == 50
    hook(client, text("/start baska", update_id=9))
    assert store.customers()[0]["source"] == "instagram"  # the first source is kept
    hook(client, text("/musteriler", user_id=ADMIN, update_id=10))
    assert "müşteri: 1" in api.of("sendMessage")[-1]["text"]


def test_abandoned_cart_gets_one_reminder(client, api, store):
    order_id = order(client)
    shop = client.app.state.shop
    created = store.order(order_id)["created"]
    assert asyncio.run(shop.remind_carts(created + 600)) == 0      # too early
    assert asyncio.run(shop.remind_carts(created + 3700)) == 1
    reminder = api.of("sendMessage")[-1]
    assert reminder["chat_id"] == BUYER and "yarım kaldı" in reminder["text"]
    assert asyncio.run(shop.remind_carts(created + 7200)) == 0     # never twice
    order(client)  # a new unpaid order for the same product: still no second reminder
    assert asyncio.run(shop.remind_carts(time.time() + 3700)) == 0


# ------------------------------------------------------------------ file delivery
def test_admin_attaches_file_and_buyer_receives_it(client, api, store):
    upload = {"update_id": 30, "message": {"message_id": 7, "chat": {"id": ADMIN, "type": "private"}, "from": {"id": ADMIN},
              "caption": "/dosya guide", "document": {"file_id": "FILE123", "file_name": "rehber.pdf"}}}
    hook(client, upload)
    assert "rehber.pdf" in api.of("sendMessage")[-1]["text"]
    assert [p for p in client.get("/api/products").json() if p["id"] == "guide"][0]["file"] == "document"
    order_id = order(client)
    hook(client, paid(order_id, update_id=31))
    (doc,) = api.of("sendDocument")
    assert doc == {"chat_id": BUYER, "document": "FILE123", "protect_content": True, "caption": "Rehber"}
    assert "Dosyan yukarıda" in api.of("sendMessage")[-1]["text"]
    assert "bot sohbetine" in client.get(f"/api/orders/{order_id}", headers=headers()).json()["content"]
    hook(client, text("/dosya guide sil", user_id=ADMIN, update_id=32))
    assert store.product_file("guide") is None


def test_failed_file_send_retries_whole_delivery(client, api, store):
    store.set_product_file("guide", "F1", "video", None)
    api.fail["sendVideo"] = 1
    order_id = order(client)
    hook(client, paid(order_id, update_id=33))
    assert not [m for m in api.of("sendMessage") if m["chat_id"] == BUYER and "✅" in m["text"]]
    asyncio.run(client.app.state.shop.redeliver(time.time() + 400))
    assert len(api.of("sendVideo")) == 2 and "✅ Rehber" in api.of("sendMessage")[-1]["text"]


def test_only_admins_can_attach_files(client, api, store):
    upload = {"update_id": 34, "message": {"message_id": 8, "chat": {"id": BUYER, "type": "private"}, "from": {"id": BUYER},
              "caption": "/dosya guide", "document": {"file_id": "X"}}}
    hook(client, upload)
    assert store.product_file("guide") is None


# ------------------------------------------------------------------ timed access
def test_timed_one_time_access_expires_and_can_be_bought_again(api, store):
    from fastapi.testclient import TestClient
    from app.config import Product
    from app.main import create_app
    from tests.conftest import make_settings
    products = {"kamp": Product("kamp", "30 Günlük Kamp", "Kamp", 90, "Kamp linki", access_days=30, channel_id=-1007),
                "pdf": Product("pdf", "PDF", "Süresiz", 10, "pdf")}
    with TestClient(create_app(make_settings(), products, store, api, background=False)) as client:
        shop = client.app.state.shop
        order_id = order(client, "kamp")
        hook(client, paid(order_id, amount=90, charge="t1"))
        view = client.get(f"/api/orders/{order_id}", headers=headers()).json()
        assert view["status"] == "paid" and abs(view["access_until"] - (time.time() + 30 * 86400)) < 60
        assert client.post("/api/orders", json={"product_id": "kamp"}, headers=headers()).status_code == 409
        later = time.time() + 31 * 86400
        asyncio.run(shop.sweep_channels(later))
        assert api.of("banChatMember")[0]["chat_id"] == -1007
        assert not store.has_access(store.order(order_id), later)
        assert [o for o in store.expired_timed_orders(later)][0]["id"] == order_id
        # a product without days never expires
        pdf_order = order(client, "pdf")
        hook(client, paid(pdf_order, amount=10, charge="t2", update_id=5))
        assert store.has_access(store.order(pdf_order), later)


def test_timed_product_without_channel_tells_buyer_when_it_ends(api, store):
    from fastapi.testclient import TestClient
    from app.config import Product
    from app.main import create_app
    from tests.conftest import make_settings
    products = {"deneme": Product("deneme", "7 Gün Deneme", "d", 5, "link", access_days=7)}
    with TestClient(create_app(make_settings(), products, store, api, background=False)) as client:
        order_id = order(client, "deneme")
        hook(client, paid(order_id, amount=5, charge="t3"))
        shop = client.app.state.shop
        assert asyncio.run(shop.notify_expired(time.time() + 86400)) == 0
        assert asyncio.run(shop.notify_expired(time.time() + 8 * 86400)) == 1
        assert "doldu" in api.of("sendMessage")[-1]["text"]
        assert asyncio.run(shop.notify_expired(time.time() + 9 * 86400)) == 0


# ------------------------------------------------------------------ promo codes
def test_promo_code_lowers_price_counts_use_and_respects_limits(client, api, store):
    hook(client, text("/kod YAZ20 20 2x", user_id=ADMIN))
    assert api.of("sendMessage")[-1]["text"].startswith("✅ YAZ20")
    assert client.get("/api/quote", params={"product_id": "guide", "code": "yaz20"}).json()["stars"] == 40
    r = client.post("/api/orders", json={"product_id": "guide", "code": "YAZ20"}, headers=headers())
    order_id = r.json()["order_id"]
    invoice = api.of("createInvoiceLink")[-1]
    assert invoice["prices"][0]["amount"] == 40 and "YAZ20" in invoice["prices"][0]["label"]
    hook(client, pre_checkout(order_id, amount=40))
    assert api.of("answerPreCheckoutQuery")[-1]["ok"] is True
    hook(client, paid(order_id, amount=40, update_id=40))
    assert store.promo("YAZ20")["uses"] == 1
    # subscriptions, unknown codes and closed codes are refused with a reason
    assert client.post("/api/orders", json={"product_id": "club", "code": "YAZ20"}, headers=headers(OTHER)).status_code == 400
    assert "geçerli değil" in client.get("/api/quote", params={"product_id": "guide", "code": "NOPE"}).json()["detail"]
    hook(client, text("/kod YAZ20 sil", user_id=ADMIN, update_id=41))
    assert client.get("/api/quote", params={"product_id": "guide", "code": "YAZ20"}).status_code == 400


def test_promo_rules_round_up_expire_and_limit_to_a_product(client, api, store):
    store.save_promo("DOKSAN", 90, product="guide", max_uses=1)
    assert client.get("/api/quote", params={"product_id": "guide", "code": "doksan"}).json()["stars"] == 5
    store.save_promo("ESKI", 10, expires=time.time() - 1)
    assert client.get("/api/quote", params={"product_id": "guide", "code": "ESKI"}).status_code == 400
    store.save_promo("TEKURUN", 10, product="club")
    assert "bu ürün" in client.get("/api/quote", params={"product_id": "guide", "code": "TEKURUN"}).json()["detail"]
    store.save_promo("BIRKEZ", 10, max_uses=1)
    store.count_promo_use("BIRKEZ")
    assert "hakkı bitti" in client.get("/api/quote", params={"product_id": "guide", "code": "BIRKEZ"}).json()["detail"]


def test_campaign_link_opens_the_shop_with_the_code(client, api, store):
    store.save_promo("KAMPANYA", 15, product="guide")
    hook(client, text("/start kod_KAMPANYA"))
    message = api.of("sendMessage")[-1]
    assert "%15" in message["text"]
    assert message["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"] == \
        "https://shop.example.com/app?code=KAMPANYA&p=guide"
    hook(client, text("/start kod_YOK", update_id=12))
    assert "geçerli değil" in api.of("sendMessage")[-1]["text"]


def test_only_admins_manage_codes(client, api, store):
    hook(client, text("/kod HACK 90"))
    assert store.promo("HACK") is None


# ------------------------------------------------------------------ announcements
def test_broadcast_preview_confirm_send_opt_out_and_daily_limit(client, api, store):
    from app.telegram import TelegramError
    shop = client.app.state.shop
    for uid, uname in ((BUYER, "a"), (OTHER, "b"), (3003, "c")):
        hook(client, text("/start", user_id=uid, update_id=uid))
    hook(client, text("/durdur", user_id=OTHER, update_id=50))
    assert "artık duyuru almayacaksın" in api.of("sendMessage")[-1]["text"]
    hook(client, text("/duyuru Yeni deneme paketi çıktı!", user_id=ADMIN, update_id=51))
    preview, info = api.of("sendMessage")[-2:]
    assert preview["chat_id"] == ADMIN and "/durdur" in preview["text"] and "2 kişiye" in info["text"]
    hook(client, text("/duyuru_onay", user_id=ADMIN, update_id=52))
    assert "2 kişiye gönderiliyor" in api.of("sendMessage")[-1]["text"]

    real = api.__call__

    async def blocked_for_3003(method, **payload):
        if method == "sendMessage" and payload.get("chat_id") == 3003:
            raise TelegramError(method, 403, "Forbidden: bot was blocked by the user")
        return await real(method, **payload)
    shop.api = blocked_for_3003
    assert asyncio.run(shop.send_broadcasts()) == 1
    shop.api = api
    received = [m["chat_id"] for m in api.of("sendMessage") if m["text"].startswith("Yeni deneme paketi")]
    assert received == [ADMIN, BUYER]  # the preview to the admin, then the one customer who can receive it
    assert "1 kişiye ulaştı" in api.of("sendMessage")[-1]["text"]
    assert store.db.execute("SELECT blocked FROM contacts WHERE user=3003").fetchone()[0] == 1
    # one announcement a day
    hook(client, text("/duyuru İkinci", user_id=ADMIN, update_id=53))
    assert "Günde en fazla bir duyuru" in api.of("sendMessage")[-1]["text"]
    assert shop.broadcast_state(time.time() + 86401)["next_allowed"] is None


def test_customers_cannot_broadcast_and_cancel_works(client, api, store):
    hook(client, text("/start", update_id=60))
    hook(client, text("/duyuru spam", update_id=61))
    assert store.pending_broadcast() is None
    hook(client, text("/duyuru Merhaba", user_id=ADMIN, update_id=62))
    hook(client, text("/duyuru_iptal", user_id=ADMIN, update_id=63))
    assert store.pending_broadcast() is None and "iptal" in api.of("sendMessage")[-1]["text"]
