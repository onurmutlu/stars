"""Multi-tenant hub: onboarding, isolation, plans, forward mode, panel and billing."""
import json
import time

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.hub import PRO_ID, create_hub_app
from app.store import Store
from app.telegram import TelegramError
from app.tenants import HubSettings, Registry
from tests.conftest import make_init_data

HUB_TOKEN = "100000:" + "H" * 35
SELLER, SELLER2, BUYER = 501, 502, 7001
TOKEN_A = "200001:" + "a" * 35
TOKEN_B = "200002:" + "b" * 35
TOKEN_C = "200003:" + "c" * 35
HUB_SECRET = "h" * 40


class FakeBot:
    def __init__(self, token, world):
        self.token, self.world = token, world
        self.bot_id = int(token.split(":")[0])
        self.calls = []

    async def __call__(self, method, **payload):
        self.calls.append((method, payload))
        if self.token in self.world.invalid:
            raise TelegramError(method, 401, "Unauthorized")
        if self.world.fail.get((self.token, method)):
            self.world.fail[(self.token, method)] -= 1
            raise TelegramError(method, 400, "Bad Request: boom")
        if method == "getMe":
            return {"id": self.bot_id, "is_bot": True, "username": f"Shop{self.bot_id}Bot", "first_name": f"Shop {self.bot_id}"}
        if method == "getWebhookInfo":
            return self.world.webhooks.get(self.token, {"url": ""})
        if method == "createInvoiceLink":
            return f"https://t.me/$inv-{self.bot_id}-{payload['payload']}"
        if method in ("sendMessage", "forwardMessage", "copyMessage"):
            return {"message_id": 700 + len(self.calls)}
        if method == "getChatMember":
            return self.world.members.get((self.token, payload["chat_id"]), {"status": "left"})
        return True

    def of(self, method):
        return [p for m, p in self.calls if m == method]


class World:
    def __init__(self):
        self.bots, self.invalid, self.fail, self.webhooks, self.forwarded = {}, set(), {}, {}, []
        self.members = {}

    def factory(self, token):
        self.bots.setdefault(token, FakeBot(token, self))
        return self.bots[token]

    async def forward(self, url, secret, update):
        self.forwarded.append((url, secret, update))


def hub_settings(**overrides):
    values = dict(bot_token=HUB_TOKEN, webhook_secret=HUB_SECRET, base_url="https://hub.example.com",
                  master_key=Fernet.generate_key().decode(), support_text="destek@hub", terms_text="Hub koşulları",
                  bot_username="HubBot", pro_stars=500, trial_days=14, admin_ids=frozenset({1}),
                  free_shops=1, free_products=3, pro_shops=5, pro_products=100)
    values.update(overrides)
    return HubSettings(**values)


@pytest.fixture
def world():
    return World()


def build(world, **overrides):
    settings = hub_settings(**overrides)
    stores = {}
    app = create_hub_app(settings, Registry(settings, ":memory:"), world.factory, world.forward,
                         billing_store=Store(":memory:"),
                         store_factory=lambda tid: stores.setdefault(tid, Store(":memory:")), background=False)
    return app


@pytest.fixture
def hub_client(world):
    with TestClient(build(world)) as client:
        client.hub = client.app.state.hub
        yield client


def panel(user=SELLER):
    return {"X-Telegram-Init-Data": make_init_data(user, token=HUB_TOKEN)}


def buyer(token, user=BUYER):
    return {"X-Telegram-Init-Data": make_init_data(user, token=token)}


def connect(client, token=TOKEN_A, user=SELLER, **extra):
    return client.post("/panel/api/shops", json={"token": token, **extra}, headers=panel(user))


def ready_shop(client, token=TOKEN_A, user=SELLER, products=({"id": "guide", "stars": 50},)):
    shop = connect(client, token, user).json()
    for p in products:
        item = {"title": p["id"].title(), "description": "Açıklama", "content": f"secret-{p['id']}", **p}
        assert client.post(f"/panel/api/shops/{shop['id']}/products", json=item, headers=panel(user)).status_code == 200
    r = client.patch(f"/panel/api/shops/{shop['id']}", headers=panel(user),
                     json={"support_text": "@destek", "terms_text": "Koşullar", "sales_enabled": True})
    assert r.status_code == 200, r.text
    return r.json()


def tenant_hook(client, tenant_id, update):
    tenant = client.hub.registry.tenant(tenant_id)
    return client.post(f"/t/{tenant_id}/webhook", json=update,
                       headers={"X-Telegram-Bot-Api-Secret-Token": tenant["webhook_secret"]})


def hub_hook(client, update):
    return client.post("/hub/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": HUB_SECRET})


def paid_update(order_id, user, amount, charge, **extra):
    return {"update_id": 9, "message": {"message_id": 1, "chat": {"id": user, "type": "private"}, "from": {"id": user},
                                        "successful_payment": {"currency": "XTR", "total_amount": amount,
                                                               "invoice_payload": order_id,
                                                               "telegram_payment_charge_id": charge, **extra}}}


def text_update(text, user, message_id=3):
    return {"update_id": 11, "message": {"message_id": message_id, "chat": {"id": user, "type": "private"},
                                         "from": {"id": user}, "text": text}}


# ------------------------------------------------------------------ onboarding
def test_connect_sets_webhook_menu_and_never_leaks_token(hub_client, world):
    r = connect(hub_client)
    assert r.status_code == 200, r.text
    shop = r.json()
    assert shop["username"] == "shop200001bot" and shop["mode"] == "standalone"
    assert TOKEN_A not in r.text and "webhook_secret" not in r.text
    bot = world.bots[TOKEN_A]
    (hook,) = bot.of("setWebhook")
    assert hook["url"] == f"https://hub.example.com/t/{shop['id']}/webhook" and len(hook["secret_token"]) >= 32
    assert hook["allowed_updates"] == ["message", "pre_checkout_query", "my_chat_member"]
    assert bot.of("setChatMenuButton")[0]["menu_button"]["web_app"]["url"] == "https://hub.example.com/s/shop200001bot"
    row = hub_client.hub.registry.tenant(shop["id"])
    assert TOKEN_A.encode() not in row["token_enc"] and hub_client.hub.registry.token(row) == TOKEN_A
    me = hub_client.get("/panel/api/me", headers=panel()).json()
    assert TOKEN_A not in json.dumps(me) and [s["id"] for s in me["shops"]] == [shop["id"]]


def test_connect_rejects_bad_tokens(hub_client, world):
    assert connect(hub_client, "1234:short-token-here").status_code == 400
    world.invalid.add(TOKEN_B)
    assert connect(hub_client, TOKEN_B).status_code == 400
    assert connect(hub_client, HUB_TOKEN).status_code == 400


def test_bot_cannot_be_claimed_by_another_owner(hub_client):
    assert connect(hub_client).status_code == 200
    assert connect(hub_client, user=SELLER2).status_code == 409


def test_same_owner_can_reconnect_with_a_new_token(hub_client, world):
    first = connect(hub_client).json()
    new_token = "200001:" + "z" * 35
    again = connect(hub_client, new_token).json()
    assert again["id"] == first["id"]
    assert hub_client.hub.registry.token(hub_client.hub.registry.tenant(first["id"])) == new_token


def test_shop_limit_follows_plan(world):
    with TestClient(build(world, trial_days=0)) as client:
        assert connect(client).status_code == 200
        r = connect(client, TOKEN_B)
        assert r.status_code == 402 and "Pro" in r.json()["detail"]


def test_sync_failure_is_stored_and_retriable(hub_client, world):
    world.fail[(TOKEN_A, "setWebhook")] = 1
    shop = connect(hub_client).json()
    assert "boom" in shop["last_sync_error"]
    fixed = hub_client.post(f"/panel/api/shops/{shop['id']}/sync", headers=panel()).json()
    assert fixed["last_sync_error"] is None


def test_token_pasted_in_hub_chat_connects_and_is_deleted(hub_client, world):
    assert hub_hook(hub_client, text_update(TOKEN_A, SELLER, message_id=77)).status_code == 200
    hub_bot = world.bots[HUB_TOKEN]
    assert hub_bot.of("deleteMessage") == [{"chat_id": SELLER, "message_id": 77}]
    assert "bağlandı" in hub_bot.of("sendMessage")[-1]["text"]
    assert TOKEN_A not in hub_bot.of("sendMessage")[-1]["text"]
    assert len(hub_client.hub.registry.tenants_of(SELLER)) == 1


# ------------------------------------------------------------------ settings and products
def test_sales_need_support_terms_and_a_product(hub_client):
    shop = connect(hub_client).json()
    url = f"/panel/api/shops/{shop['id']}"
    assert hub_client.patch(url, json={"sales_enabled": True}, headers=panel()).status_code == 400
    hub_client.patch(url, json={"support_text": "@d", "terms_text": "k"}, headers=panel())
    assert hub_client.patch(url, json={"sales_enabled": True}, headers=panel()).status_code == 400
    hub_client.post(f"{url}/products", headers=panel(), json={
        "id": "x", "title": "X", "description": "d", "stars": 1, "content": "c"})
    assert hub_client.patch(url, json={"sales_enabled": True}, headers=panel()).json()["sales_enabled"] is True


def test_owner_isolation_in_panel(hub_client):
    shop = ready_shop(hub_client)
    url = f"/panel/api/shops/{shop['id']}"
    assert hub_client.get(url, headers=panel(SELLER2)).status_code == 404
    assert hub_client.patch(url, json={"shop_name": "hack"}, headers=panel(SELLER2)).status_code == 404
    assert hub_client.post(f"{url}/refund", json={"charge": "x"}, headers=panel(SELLER2)).status_code == 404
    assert hub_client.get(url, headers=panel(BUYER)).status_code == 404
    assert hub_client.get(url).status_code == 401


def test_product_validation_and_limits(world):
    with TestClient(build(world, trial_days=0)) as client:
        shop = connect(client).json()
        url = f"/panel/api/shops/{shop['id']}/products"
        bad = client.post(url, headers=panel(), json={"id": "Bad Id", "title": "t", "description": "d", "stars": 1, "content": "c"})
        assert bad.status_code == 400
        for i in range(3):
            assert client.post(url, headers=panel(), json={
                "id": f"p{i}", "title": "t", "description": "d", "stars": 1, "content": "c"}).status_code == 200
        over = client.post(url, headers=panel(), json={"id": "p9", "title": "t", "description": "d", "stars": 1, "content": "c"})
        assert over.status_code == 400 and "sınır" in over.json()["detail"]
        assert client.delete(f"{url}/p0", headers=panel()).status_code == 200
        assert client.post(url, headers=panel(), json={
            "id": "p9", "title": "t", "description": "d", "stars": 1, "content": "c"}).status_code == 200


def test_category_and_badge_reach_the_buyer_and_old_databases_migrate(world, tmp_path):
    import sqlite3
    from app.tenants import Registry
    with TestClient(build(world)) as client:
        shop = connect(client).json()
        url = f"/panel/api/shops/{shop['id']}/products"
        assert client.post(url, headers=panel(), json={"id": "g", "title": "G", "description": "d", "stars": 1,
                                                      "content": "c", "category": "Rehber", "badge": "Yeni"}).status_code == 200
        saved = client.get(f"/panel/api/shops/{shop['id']}", headers=panel()).json()["products"][0]
        assert saved["category"] == "Rehber" and saved["badge"] == "Yeni"
        assert client.get(f"/s/{shop['username']}/api/config").json()["bot_username"] == shop["username"]
    # a database from before these columns existed gets them on open
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE products(tenant TEXT, id TEXT, title TEXT, description TEXT, stars INTEGER, content TEXT, "
               "kind TEXT, photo_url TEXT, position INTEGER DEFAULT 0, archived INTEGER DEFAULT 0, updated REAL, "
               "PRIMARY KEY(tenant,id))")
    db.commit(); db.close()
    registry = Registry(hub_settings(), path)
    assert {"category", "badge"} <= {r[1] for r in registry.db.execute("PRAGMA table_info(products)")}
    registry.close()


# ------------------------------------------------------------------ buyer flow per tenant
def test_full_tenant_purchase_uses_tenant_bot(hub_client, world):
    shop = ready_shop(hub_client)
    slug = shop["username"]
    assert hub_client.get(f"/s/{slug}/").status_code == 200
    products = hub_client.get(f"/s/{slug}/api/products").json()
    assert [p["id"] for p in products] == ["guide"] and "content" not in products[0]

    # initData signed by another bot (e.g. the hub) is rejected for this shop
    assert hub_client.post(f"/s/{slug}/api/orders", json={"product_id": "guide"}, headers=buyer(HUB_TOKEN)).status_code == 401
    r = hub_client.post(f"/s/{slug}/api/orders", json={"product_id": "guide"}, headers=buyer(TOKEN_A))
    order_id = r.json()["order_id"]
    bot = world.bots[TOKEN_A]
    assert bot.of("createInvoiceLink")[0]["currency"] == "XTR"
    assert not world.bots[HUB_TOKEN].of("createInvoiceLink")

    tenant_hook(hub_client, shop["id"], {"update_id": 1, "pre_checkout_query": {
        "id": "q", "from": {"id": BUYER}, "currency": "XTR", "total_amount": 50, "invoice_payload": order_id}})
    assert bot.of("answerPreCheckoutQuery")[0]["ok"] is True
    tenant_hook(hub_client, shop["id"], paid_update(order_id, BUYER, 50, "ch-a"))
    status = hub_client.get(f"/s/{slug}/api/orders/{order_id}", headers=buyer(TOKEN_A)).json()
    assert status["status"] == "paid" and status["content"] == "secret-guide"
    assert any(m["chat_id"] == BUYER for m in bot.of("sendMessage"))

    stats = hub_client.get(f"/panel/api/shops/{shop['id']}", headers=panel()).json()["stats"]
    assert stats["stars"] == 50 and stats["buyers"] == 1 and stats["recent"][0]["charge"] == "ch-a"
    assert hub_client.post(f"/panel/api/shops/{shop['id']}/refund", json={"charge": "ch-a"}, headers=panel()).json() == {"refunded": True}
    assert bot.of("refundStarPayment") == [{"user_id": BUYER, "telegram_payment_charge_id": "ch-a"}]


def test_tenants_are_isolated(hub_client, world):
    a = ready_shop(hub_client, TOKEN_A, SELLER)
    b = ready_shop(hub_client, TOKEN_B, SELLER2, products=({"id": "other", "stars": 70},))
    order_a = hub_client.post(f"/s/{a['username']}/api/orders", json={"product_id": "guide"}, headers=buyer(TOKEN_A)).json()["order_id"]
    # tenant B's webhook cannot see tenant A's order
    tenant_hook(hub_client, b["id"], {"update_id": 1, "pre_checkout_query": {
        "id": "q", "from": {"id": BUYER}, "currency": "XTR", "total_amount": 50, "invoice_payload": order_a}})
    assert world.bots[TOKEN_B].of("answerPreCheckoutQuery")[0]["ok"] is False
    assert hub_client.get(f"/s/{b['username']}/api/orders/{order_a}", headers=buyer(TOKEN_B)).status_code == 404
    # wrong secret on a tenant webhook
    assert hub_client.post(f"/t/{a['id']}/webhook", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "x" * 40}).status_code == 403
    tb = hub_client.hub.registry.tenant(b["id"])
    assert hub_client.post(f"/t/{a['id']}/webhook", json={},
                           headers={"X-Telegram-Bot-Api-Secret-Token": tb["webhook_secret"]}).status_code == 403


def test_free_plan_limits_catalog_and_shows_branding(world):
    with TestClient(build(world, trial_days=0, free_products=1)) as client:
        client.hub = client.app.state.hub
        shop = connect(client).json()
        client.hub.registry.save_product(shop["id"], {"id": "a", "title": "A", "description": "d", "stars": 1, "content": "c"}, 9, True)
        client.hub.registry.save_product(shop["id"], {"id": "b", "title": "B", "description": "d", "stars": 1, "content": "c"}, 9, True)
        client.patch(f"/panel/api/shops/{shop['id']}", headers=panel(),
                     json={"support_text": "s", "terms_text": "t", "sales_enabled": True})
        assert [p["id"] for p in client.get(f"/s/{shop['username']}/api/products").json()] == ["a"]
        assert client.get(f"/s/{shop['username']}/api/config").json()["powered_by"]["url"] == "https://t.me/HubBot"
        view = client.get(f"/panel/api/shops/{shop['id']}", headers=panel()).json()
        assert [p["over_limit"] for p in view["products"]] == [False, True]


def test_trial_hides_branding(hub_client):
    shop = ready_shop(hub_client)
    assert "powered_by" not in hub_client.get(f"/s/{shop['username']}/api/config").json()
    assert hub_client.get("/panel/api/me", headers=panel()).json()["plan"]["name"] == "trial"


def test_suspended_shop_stops_selling_but_records_payments(hub_client, world):
    shop = ready_shop(hub_client)
    order_id = hub_client.post(f"/s/{shop['username']}/api/orders", json={"product_id": "guide"}, headers=buyer(TOKEN_A)).json()["order_id"]
    hub_client.hub.registry.update(shop["id"], status="suspended", sales_enabled=False)
    assert hub_client.get(f"/s/{shop['username']}/api/products").json() == []
    tenant_hook(hub_client, shop["id"], paid_update(order_id, BUYER, 50, "late"))
    assert hub_client.hub.store_for(shop["id"]).payment("late") is not None


# ------------------------------------------------------------------ forward mode
def test_existing_backend_requires_confirmation_then_forwards(hub_client, world):
    world.webhooks[TOKEN_A] = {"url": "https://owner.example.com/hook", "allowed_updates": ["message", "callback_query"]}
    r = connect(hub_client)
    assert r.status_code == 409 and r.json()["needs_confirm"] and r.json()["webhook_url"] == "https://owner.example.com/hook"
    assert not world.bots[TOKEN_A].of("setWebhook")

    shop = connect(hub_client, confirm_forward=True, forward_secret="ownersecret").json()
    assert shop["mode"] == "forward"
    bot = world.bots[TOKEN_A]
    assert bot.of("setWebhook")[0]["allowed_updates"] == ["callback_query", "message", "pre_checkout_query"]
    assert not bot.of("setChatMenuButton") and not bot.of("setMyCommands")

    shop = ready_shop(hub_client)  # reconnect keeps forward settings, adds products
    tenant_hook(hub_client, shop["id"], text_update("/start", BUYER))
    tenant_hook(hub_client, shop["id"], {"update_id": 5, "callback_query": {"id": "c", "from": {"id": BUYER}, "data": "x"}})
    tenant_hook(hub_client, shop["id"], {"update_id": 6, "pre_checkout_query": {
        "id": "q", "from": {"id": BUYER}, "currency": "XTR", "total_amount": 10, "invoice_payload": "their-own-invoice"}})
    assert [u["update_id"] for _url, _s, u in world.forwarded] == [11, 5, 6]
    assert all(url == "https://owner.example.com/hook" and s == "ownersecret" for url, s, _u in world.forwarded)
    assert not bot.of("answerPreCheckoutQuery")

    world.forwarded.clear()
    tenant_hook(hub_client, shop["id"], text_update("/magaza", BUYER))
    assert not world.forwarded and bot.of("sendMessage")[-1]["reply_markup"]["inline_keyboard"][0][0]["web_app"]
    order_id = hub_client.post(f"/s/{shop['username']}/api/orders", json={"product_id": "guide"}, headers=buyer(TOKEN_A)).json()["order_id"]
    tenant_hook(hub_client, shop["id"], {"update_id": 7, "pre_checkout_query": {
        "id": "q2", "from": {"id": BUYER}, "currency": "XTR", "total_amount": 50, "invoice_payload": order_id}})
    tenant_hook(hub_client, shop["id"], paid_update(order_id, BUYER, 50, "ch-f"))
    assert not world.forwarded and bot.of("answerPreCheckoutQuery")[-1]["ok"] is True

    # removing the shop gives the bot its old webhook back
    assert hub_client.delete(f"/panel/api/shops/{shop['id']}", headers=panel()).status_code == 200
    restored = bot.of("setWebhook")[-1]
    assert restored == {"url": "https://owner.example.com/hook", "allowed_updates": ["callback_query", "message"],
                        "secret_token": "ownersecret"}
    assert hub_client.get(f"/s/{shop['username']}/api/products").status_code == 404


def test_hub_chat_refuses_token_of_bot_with_backend(hub_client, world):
    world.webhooks[TOKEN_A] = {"url": "https://owner.example.com/hook"}
    hub_hook(hub_client, text_update(TOKEN_A, SELLER))
    assert "panelden" in world.bots[HUB_TOKEN].of("sendMessage")[-1]["text"]
    assert not hub_client.hub.registry.tenants_of(SELLER)


# ------------------------------------------------------------------ plan billing through the hub bot
def test_pro_plan_is_a_star_subscription_on_the_hub_bot(world):
    with TestClient(build(world, trial_days=0)) as client:
        hub = client.app.state.hub
        assert client.get("/panel/api/me", headers=panel()).json()["plan"]["name"] == "free"
        r = client.post("/panel/api/plan/invoice", headers=panel())
        order_id = r.json()["order_id"]
        (invoice,) = world.bots[HUB_TOKEN].of("createInvoiceLink")
        assert invoice["subscription_period"] == 2592000 and invoice["prices"][0]["amount"] == 500
        hub_hook(client, {"update_id": 1, "pre_checkout_query": {
            "id": "q", "from": {"id": SELLER}, "currency": "XTR", "total_amount": 500, "invoice_payload": order_id}})
        assert world.bots[HUB_TOKEN].of("answerPreCheckoutQuery")[0]["ok"] is True
        expires = int(time.time()) + 2592000
        hub_hook(client, paid_update(order_id, SELLER, 500, "pro-1", subscription_expiration_date=expires, is_recurring=True))
        me = client.get("/panel/api/me", headers=panel()).json()
        assert me["plan"]["name"] == "pro" and me["plan"]["until"] == expires and me["plan"]["max_products"] == 100
        assert client.post("/panel/api/plan/invoice", headers=panel()).status_code == 409
        # after a refund the plan falls back to free
        hub.billing.store.mark_refunded("pro-1")
        assert client.get("/panel/api/me", headers=panel()).json()["plan"]["name"] == "free"
        assert hub.pro_until(SELLER2) is None and PRO_ID == "pro"


def test_hub_commands(hub_client, world):
    hub_hook(hub_client, text_update("/start", SELLER))
    hub_hook(hub_client, text_update("/plan", SELLER))
    hub_hook(hub_client, text_update("/paysupport", SELLER))
    texts = [m["text"] for m in world.bots[HUB_TOKEN].of("sendMessage")]
    assert "token" in texts[0] and "Deneme" in texts[1] and texts[2] == "destek@hub"
    assert hub_client.post("/hub/webhook", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "no"}).status_code == 403


def test_hub_settings_validation():
    with pytest.raises(Exception):
        hub_settings(master_key="nope").check()
    with pytest.raises(Exception):
        hub_settings(base_url="http://x").check()
    hub_settings().check()


def test_license_link_sells_pro_in_the_hub_chat(world):
    with TestClient(build(world, trial_days=0)) as client:
        hub_hook(client, text_update("/start lisans", SELLER))
        bot = world.bots[HUB_TOKEN]
        (invoice,) = bot.of("createInvoiceLink")
        assert invoice["subscription_period"] == 2592000
        button = bot.of("sendMessage")[-1]["reply_markup"]["inline_keyboard"][0][0]
        assert "lisans al" in button["text"] and button["url"]
        order_id = invoice["payload"]
        expires = int(time.time()) + 2592000
        hub_hook(client, paid_update(order_id, SELLER, invoice["prices"][0]["amount"], "pro-9",
                                     subscription_expiration_date=expires, is_recurring=True))
        hub_hook(client, text_update("/lisans", SELLER))
        assert "aktif" in bot.of("sendMessage")[-1]["text"] and len(bot.of("createInvoiceLink")) == 1


def test_shop_shows_admin_link_only_to_its_owner_and_buy_link_on_free_plan(world):
    with TestClient(build(world, trial_days=0)) as client:
        shop = ready_shop(client)
        url = f"/s/{shop['username']}/api/config"
        assert client.get(url, headers=buyer(TOKEN_A, SELLER)).json()["admin_url"] == "https://t.me/HubBot?start=panel"
        stranger = client.get(url, headers=buyer(TOKEN_A)).json()
        assert "admin_url" not in stranger and stranger["buy"]["url"] == "https://t.me/HubBot?start=lisans"
        # a session signed by another bot is not the owner of this shop
        assert "admin_url" not in client.get(url, headers=buyer(TOKEN_B, SELLER)).json()



def test_channel_product_needs_the_bot_as_channel_admin(hub_client, world):
    shop = ready_shop(hub_client)
    url = f"/panel/api/shops/{shop['id']}/products"
    item = {"id": "vip", "title": "VIP", "description": "d", "stars": 10, "content": "Hoş geldin",
            "kind": "subscription", "channel_id": "-1001234567890"}
    assert "kanal" in hub_client.post(url, headers=panel(), json=item).json()["detail"].lower()
    world.members[(TOKEN_A, -1001234567890)] = {"status": "administrator", "can_invite_users": True,
                                                "can_restrict_members": False}
    assert "yasakla" in hub_client.post(url, headers=panel(), json=item).json()["detail"]
    world.members[(TOKEN_A, -1001234567890)]["can_restrict_members"] = True
    assert hub_client.post(url, headers=panel(), json=item).status_code == 200
    products = hub_client.get(f"/s/{shop['username']}/api/products").json()
    assert any(p["id"] == "vip" and p["channel"] is True and "channel_id" not in p for p in products)
    # the bot being made admin of a channel shows up in the panel
    tenant_hook(hub_client, shop["id"], {"update_id": 50, "my_chat_member": {
        "chat": {"id": -1009, "type": "channel", "title": "Kanalım"}, "from": {"id": SELLER},
        "new_chat_member": {"status": "administrator", "can_invite_users": True, "can_restrict_members": True}}})
    view = hub_client.get(f"/panel/api/shops/{shop['id']}", headers=panel()).json()
    assert view["channels"] == [{"id": -1009, "title": "Kanalım", "type": "channel", "ready": True}]
    assert "-1009" in world.bots[TOKEN_A].of("sendMessage")[-1]["text"]



def test_panel_crm_lists_shop_contacts_and_messages_them(hub_client, world):
    shop = ready_shop(hub_client)
    slug = shop["username"]
    # a buyer opens the shop and writes to the bot
    hub_client.get(f"/s/{slug}/api/purchases", headers=buyer(TOKEN_A))
    tenant_hook(hub_client, shop["id"], {"update_id": 60, "message": {
        "message_id": 9, "chat": {"id": BUYER, "type": "private"}, "from": {"id": BUYER, "first_name": "Ayşe"},
        "text": "Merhaba, VIP ne kadar?"}})
    bot = world.bots[TOKEN_A]
    assert any(m["chat_id"] == SELLER and "VIP ne kadar" in m["text"] for m in bot.of("sendMessage"))
    url = f"/panel/api/shops/{shop['id']}/customers"
    data = hub_client.get(url, headers=panel()).json()
    assert data["funnel"]["ziyaretçi"] == 1 and data["customers"][0]["name"] == "Ayşe"
    assert hub_client.get(url, headers=panel(SELLER2)).status_code == 404
    sent = hub_client.post(f"{url}/{BUYER}/message", headers=panel(), json={"text": "Merhaba Ayşe, 250⭐"})
    assert sent.status_code == 200 and bot.of("sendMessage")[-1] == {"chat_id": BUYER, "text": "Merhaba Ayşe, 250⭐"}
    assert hub_client.post(f"{url}/12345/message", headers=panel(), json={"text": "x"}).status_code == 404
    assert hub_client.put(f"{url}/{BUYER}/note", headers=panel(), json={"note": "pazartesi ara"}).status_code == 200
    assert hub_client.get(url, headers=panel()).json()["customers"][0]["note"] == "pazartesi ara"


def test_panel_promo_codes_per_shop(hub_client, world):
    shop = ready_shop(hub_client)
    url = f"/panel/api/shops/{shop['id']}/promos"
    r = hub_client.post(url, headers=panel(), json={"code": "ilk10", "percent": 10, "days": 7})
    assert r.status_code == 200 and r.json()["promos"][0]["code"] == "ILK10"
    assert hub_client.post(url, headers=panel(), json={"code": "x y", "percent": 10}).status_code == 400
    assert hub_client.get(url, headers=panel(SELLER2)).status_code == 404
    quote = hub_client.get(f"/s/{shop['username']}/api/quote", params={"product_id": "guide", "code": "ILK10"}).json()
    assert quote["stars"] == 45 and quote["original"] == 50
    assert hub_client.delete(f"{url}/ILK10", headers=panel()).json()["promos"][0]["active"] == 0
