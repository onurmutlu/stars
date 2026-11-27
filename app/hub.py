"""Hub: one server that runs ready Stars shops for many Telegram bots.

    uvicorn app.hub:app --factory --port 8000

Routes
  /s/{slug}/                 buyer Mini App of a tenant shop (same UI as the single-shop kit)
  /s/{slug}/api/*            its API, authenticated with initData signed by THAT tenant's bot
  /t/{tenant_id}/webhook     Telegram webhook of a tenant bot (per-tenant secret)
  /panel/                    seller panel Mini App, opened from the hub bot
  /panel/api/*               panel API, authenticated with initData signed by the hub bot
  /hub/webhook               webhook of the hub bot: onboarding commands and plan billing

Two connection modes for a tenant bot
  standalone  the bot is used only as a shop; the hub sets its webhook, menu button and commands.
  forward     the bot already has a backend. The hub takes over the webhook, keeps the shop's own
              updates (its invoices, payments, refunds and /magaza, /orders, /paysupport, /terms)
              and forwards every other update unchanged to the previous webhook URL. Menu button
              and commands are left alone. Deleting the shop restores the previous webhook.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Awaitable, Callable

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import initdata, assets
from .config import ConfigError, Product, Settings
from .shop import ShopError, StarsShop, process_update
from .store import Store
from .telegram import BotAPI, TelegramError
from .tenants import TOKEN_RE, HubSettings, Registry, public_tenant

log = logging.getLogger("stars_hub")
ROOT = Path(__file__).resolve().parents[1]
WEBAPP_DIR = ROOT / "webapp"
PANEL_DIR = ROOT / "panel"

SHOP_UPDATES = ["message", "pre_checkout_query"]
SHOP_COMMANDS = [
    {"command": "start", "description": "Mağazayı aç"},
    {"command": "orders", "description": "Satın aldıklarımı yeniden gönder"},
    {"command": "paysupport", "description": "Ödeme desteği"},
    {"command": "terms", "description": "Satış koşulları"},
]
HUB_COMMANDS = [
    {"command": "start", "description": "Paneli aç"},
    {"command": "yenimagaza", "description": "Botuna mağaza bağla"},
    {"command": "magazalarim", "description": "Mağazalarım"},
    {"command": "plan", "description": "Planım"},
    {"command": "paysupport", "description": "Ödeme desteği"},
    {"command": "terms", "description": "Koşullar"},
]
# In forward mode these commands stay with the shop; everything else goes to the bot's own backend.
FORWARD_MODE_SHOP_COMMANDS = {"/magaza", "/shop", "/orders", "/paysupport", "/terms"}
PRO_ID = "pro"

ApiFactory = Callable[[str], Callable[..., Awaitable]]
Forwarder = Callable[[str, str | None, dict], Awaitable[None]]


class NeedsConfirmation(ShopError):
    def __init__(self, webhook_url: str):
        super().__init__("Bu botun zaten bir sunucusu (webhook) var.", 409)
        self.webhook_url = webhook_url


def _command(text: str) -> tuple[str, str]:
    command, _, argument = (text or "").strip().partition(" ")
    return command.split("@")[0].lower(), argument.strip()


class Hub:
    def __init__(self, settings: HubSettings, registry: Registry, api_factory: ApiFactory,
                 forwarder: Forwarder, billing_store: Store | None = None, store_factory=None):
        self.settings, self.registry = settings, registry
        self.api_factory, self.forwarder = api_factory, forwarder
        self.store_factory = store_factory or (lambda tenant_id: Store(registry.store_path(tenant_id)))
        self.hub_api = api_factory(settings.bot_token)
        self._apis: dict[str, tuple[str, Callable]] = {}
        self._stores: dict[str, Store] = {}
        self.failures: dict[str, dict[int, int]] = {}
        self.hub_bot_id = int(settings.bot_token.split(":")[0])
        billing_settings = Settings(
            bot_token=settings.bot_token, webhook_secret=settings.webhook_secret,
            webapp_url=f"{settings.base_url}/panel", support_text=settings.support_text,
            terms_text=settings.terms_text, shop_name=settings.name, sales_enabled=settings.billing_enabled,
            admin_ids=settings.admin_ids, init_data_max_age=settings.init_data_max_age)
        pro = Product(PRO_ID, "Mağaza Lisansı (Pro)", f"{settings.pro_shops} mağaza, mağaza başına {settings.pro_products} ürün, "
                      "markasız vitrin. 30 günde bir otomatik uzar.", settings.pro_stars,
                      f"Lisansın aktif 🎉 Panel: {settings.base_url}/panel", kind="subscription")
        self.billing = StarsShop(self.hub_api, billing_settings, {PRO_ID: pro},
                                 billing_store or Store(Path(settings.data_dir) / "billing.sqlite3"))

    # ------------------------------------------------------------------ plans
    def pro_until(self, owner: int) -> float | None:
        store = self.billing.store
        until = [store.access_until(o) for o in store.paid_orders(owner)
                 if o["product"] == PRO_ID and store.has_access(o)]
        return max((u for u in until if u), default=None)

    def plan(self, owner: int):
        return self.registry.plan(owner, self.pro_until(owner))

    # ------------------------------------------------------------------ tenant runtime
    def api_for(self, tenant) -> Callable:
        token = self.registry.token(tenant)
        cached = self._apis.get(tenant["id"])
        if not cached or cached[0] != token:
            self._apis[tenant["id"]] = (token, self.api_factory(token))
        return self._apis[tenant["id"]][1]

    def store_for(self, tenant_id: str) -> Store:
        if tenant_id not in self._stores:
            self._stores[tenant_id] = self.store_factory(tenant_id)
        return self._stores[tenant_id]

    def shop(self, tenant) -> StarsShop:
        """A StarsShop for the tenant's current settings and plan. Cheap: no network, two queries."""
        products = self.registry.catalog(tenant["id"], self.plan(tenant["owner"]).max_products)
        return StarsShop(self.api_for(tenant), self.registry.shop_settings(tenant), products,
                         self.store_for(tenant["id"]))

    def owned_tenant(self, owner: int, tenant_id: str):
        tenant = self.registry.tenant(tenant_id)
        if not tenant or tenant["owner"] != owner or tenant["status"] == "deleted":
            raise ShopError("Mağaza bulunamadı.", 404)
        return tenant

    # ------------------------------------------------------------------ onboarding
    async def connect(self, owner: int, token: str, confirm_forward: bool = False,
                      forward_secret: str | None = None):
        token = (token or "").strip()
        if not TOKEN_RE.match(token):
            raise ShopError("Bu bir bot token'ına benzemiyor. @BotFather'dan aldığın token'ı olduğu gibi yapıştır.")
        if token.split(":")[0] == str(self.hub_bot_id):
            raise ShopError("Bu, platformun kendi botu. Kendi botunun token'ını gönder.")
        api = self.api_factory(token)
        try:
            bot = await api("getMe")
        except TelegramError:
            raise ShopError("Telegram bu token'ı kabul etmedi. @BotFather'dan güncel token'ı al.") from None
        self.registry.ensure_owner(owner)
        existing = self.registry.by_bot(bot["id"])
        if existing and existing["owner"] != owner:
            raise ShopError("Bu bot başka bir hesaba bağlı. Botun sahibiysen destekle iletişime geç.", 409)
        if not existing or existing["status"] == "deleted":
            plan = self.plan(owner)
            if len(self.registry.tenants_of(owner)) >= plan.max_shops:
                raise ShopError(f"Planın en fazla {plan.max_shops} mağazaya izin veriyor. Daha fazlası için Pro'ya geç.", 402)

        forward = None
        if not existing or not existing["forward_url"]:
            info = await api("getWebhookInfo")
            url = info.get("url") or ""
            if url and not url.startswith(f"{self.settings.base_url}/t/"):
                if not confirm_forward:
                    raise NeedsConfirmation(url)
                forward = {"forward_url": url, "forward_secret": forward_secret,
                           "forward_updates": info.get("allowed_updates") or []}

        tenant = self.registry.reconnect(existing, token, bot) if existing else self.registry.create(owner, token, bot)
        if forward:
            tenant = self.registry.update(tenant["id"], **forward)
        self._apis.pop(tenant["id"], None)
        await self.sync(tenant)
        return self.registry.tenant(tenant["id"])

    async def sync(self, tenant) -> bool:
        """Point the tenant bot at this hub. Errors are stored for the panel, not raised."""
        api = self.api_for(tenant)
        try:
            if tenant["forward_url"]:
                previous = json.loads(tenant["forward_updates"] or "[]")
                allowed = sorted(set(previous) | set(SHOP_UPDATES)) if previous else []
                await api("setWebhook", url=self.settings.webhook_url(tenant["id"]),
                          secret_token=tenant["webhook_secret"], allowed_updates=allowed)
            else:
                await api("setWebhook", url=self.settings.webhook_url(tenant["id"]),
                          secret_token=tenant["webhook_secret"], allowed_updates=SHOP_UPDATES)
                await api("setChatMenuButton", menu_button={
                    "type": "web_app", "text": "Mağaza", "web_app": {"url": self.settings.shop_url(tenant["username"])}})
                await api("setMyCommands", commands=SHOP_COMMANDS)
        except TelegramError as exc:
            self.registry.update(tenant["id"], last_sync=time.time(), last_sync_error=exc.description[:300])
            log.warning("Kiracı %s Telegram'a bağlanamadı: %s", tenant["id"], exc.description)
            return False
        self.registry.update(tenant["id"], last_sync=time.time(), last_sync_error=None)
        return True

    async def remove(self, tenant) -> None:
        """Stop the shop. Payment records are kept for refunds; a forwarded bot gets its old webhook back."""
        api = self.api_for(tenant)
        with contextlib.suppress(TelegramError):
            if tenant["forward_url"]:
                kwargs = {"url": tenant["forward_url"],
                          "allowed_updates": json.loads(tenant["forward_updates"] or "[]")}
                if tenant["forward_secret"]:
                    kwargs["secret_token"] = tenant["forward_secret"]
                await api("setWebhook", **kwargs)
            else:
                await api("deleteWebhook")
                await api("setChatMenuButton", menu_button={"type": "default"})
        self.registry.update(tenant["id"], status="deleted", sales_enabled=False)

    # ------------------------------------------------------------------ tenant updates
    def _belongs_to_shop(self, tenant, store: Store, update: dict) -> bool:
        query = update.get("pre_checkout_query")
        if query:
            return store.order(query.get("invoice_payload", "")) is not None
        message = update.get("message")
        if not message:
            return False
        if message.get("successful_payment"):
            return store.order(message["successful_payment"].get("invoice_payload", "")) is not None
        if message.get("refunded_payment"):
            return store.payment(message["refunded_payment"].get("telegram_payment_charge_id", "")) is not None
        if message.get("chat", {}).get("type") != "private" or not message.get("text"):
            return False
        command, argument = _command(message["text"])
        if command in FORWARD_MODE_SHOP_COMMANDS:
            return True
        if (message.get("from") or {}).get("id") == tenant["owner"]:
            return command == "/issues" or (command == "/refund" and store.payment(argument) is not None)
        return False

    async def tenant_update(self, tenant, update: dict) -> None:
        shop = self.shop(tenant)
        failures = self.failures.setdefault(tenant["id"], {})
        if not tenant["forward_url"] or self._belongs_to_shop(tenant, shop.store, update):
            await process_update(shop, update, failures)
        else:
            await self.forwarder(tenant["forward_url"], tenant["forward_secret"], update)

    # ------------------------------------------------------------------ hub bot
    async def hub_update(self, update: dict) -> None:
        message = update.get("message") or {}
        sender = message.get("from") or {}
        text = message.get("text") or ""
        private = message.get("chat", {}).get("type") == "private" and not sender.get("is_bot")
        if private and text and not text.startswith("/") and TOKEN_RE.match(text.strip()):
            await self._token_message(sender["id"], message)
            return
        command, argument = _command(text)
        if private and text and command not in ("/paysupport", "/terms", "/orders", "/refund", "/issues"):
            await self._hub_command(sender["id"], command, argument)
            return
        await process_update(self.billing, update, self.failures.setdefault("__hub__", {}))

    async def _say(self, chat: int, text: str, panel_button: bool = True) -> None:
        extra = {"reply_markup": {"inline_keyboard": [[{"text": "🧰 Paneli aç",
                                                         "web_app": {"url": f"{self.settings.base_url}/panel"}}]]}} \
            if panel_button else {}
        await self.hub_api("sendMessage", chat_id=chat, text=text, **extra)

    async def _token_message(self, owner: int, message: dict) -> None:
        # The token should not stay in the chat history.
        with contextlib.suppress(Exception):
            await self.hub_api("deleteMessage", chat_id=message["chat"]["id"], message_id=message["message_id"])
        try:
            tenant = await self.connect(owner, message["text"])
        except NeedsConfirmation as exc:
            await self._say(owner, f"Bu botun zaten bir sunucusu var ({exc.webhook_url}).\n"
                                   "Mevcut botunu bozmamak için bağlantıyı panelden yap: mağaza güncellemelerini biz "
                                   "işleriz, geri kalanını kendi sunucuna aynen iletiriz.")
            return
        except ShopError as exc:
            await self._say(owner, f"❌ {exc}", panel_button=False)
            return
        warning = f"\n⚠️ Telegram ayarı tamamlanamadı: {tenant['last_sync_error']}" if tenant["last_sync_error"] else ""
        await self._say(owner, f"✅ @{tenant['username']} bağlandı (token mesajını sildim).\n"
                               f"Şimdi panelden ürün ekle, destek ve koşul metnini yaz, satışı aç.{warning}")

    def buy_link(self) -> str:
        """Deep link that sells the license: the hub bot answers with a Stars invoice."""
        return f"https://t.me/{self.settings.bot_username}?start=lisans" if self.settings.bot_username else ""

    def panel_link(self) -> str:
        return f"https://t.me/{self.settings.bot_username}?start=panel" if self.settings.bot_username \
            else f"{self.settings.base_url}/panel"

    async def _license(self, owner: int) -> None:
        self.registry.ensure_owner(owner)
        until = self.pro_until(owner)
        if until:
            day = time.strftime("%d.%m.%Y", time.localtime(until))
            await self._say(owner, f"Lisansın aktif: {day} tarihine kadar. Her 30 günde {self.settings.pro_stars}⭐ ile "
                                   "otomatik uzar; Telegram ayarlarından iptal edebilirsin.")
            return
        if not self.settings.billing_enabled:
            await self._say(owner, "Lisans satışı şu an kapalı.")
            return
        invoice = await self.billing.create_invoice(owner, PRO_ID)
        await self.hub_api(
            "sendMessage", chat_id=owner,
            text=f"Mağaza Lisansı (Pro): {self.settings.pro_shops} mağaza, mağaza başına {self.settings.pro_products} "
                 f"ürün, markasız vitrin.\n⭐ {self.settings.pro_stars} / 30 gün, otomatik uzar.",
            reply_markup={"inline_keyboard": [
                [{"text": f"⭐ {self.settings.pro_stars} ile lisans al", "url": invoice["invoice_link"]}],
                [{"text": "🧰 Paneli aç", "web_app": {"url": f"{self.settings.base_url}/panel"}}],
            ]},
        )

    async def _hub_command(self, owner: int, command: str, argument: str = "") -> None:
        if command in ("/lisans", "/license", "/satinal") or (command == "/start" and argument == "lisans"):
            await self._license(owner)
            return
        if command in ("/yenimagaza", "/newshop"):
            await self._say(owner, "1) @BotFather → /newbot ile bir bot aç (ya da mevcut botunu kullan).\n"
                                   "2) Token'ı buraya yapıştır ya da panelde 'Mağaza bağla'ya gir.\n"
                                   "Stars doğrudan senin botunun bakiyesine yatar; Fragment'ten sen çekersin.")
        elif command in ("/magazalarim", "/myshops"):
            tenants = self.registry.tenants_of(owner)
            lines = [f"• @{t['username']}: {'satış açık' if t['sales_enabled'] else 'satış kapalı'}"
                     + ("" if t["status"] == "active" else f" ({t['status']})") for t in tenants]
            await self._say(owner, "\n".join(lines) if lines else "Henüz mağazan yok. /yenimagaza")
        elif command == "/plan":
            self.registry.ensure_owner(owner)
            plan = self.plan(owner)
            until = time.strftime("%d.%m.%Y", time.localtime(plan.until)) if plan.until else ""
            names = {"pro": "Pro", "trial": "Deneme (Pro özellikleri)", "free": "Ücretsiz"}
            await self._say(owner, f"Planın: {names[plan.name]}{' · ' + until + ' tarihine kadar' if until else ''}\n"
                                   f"{plan.max_shops} mağaza, mağaza başına {plan.max_products} ürün."
                                   + (f"\nLisans: {self.settings.pro_stars}⭐ / 30 gün. Satın al: /lisans" if plan.name != "pro" else ""))
        else:
            self.registry.ensure_owner(owner)
            await self._say(owner, f"{self.settings.name}: Telegram botun için hazır Stars mağazası.\n\n"
                                   "• Ürünlerini ekle, Stars ile tek dokunuşla sat; ödeme, teslimat, abonelik ve iade otomatik.\n"
                                   "• Para doğrudan senin botuna yatar, biz dokunmayız.\n"
                                   "• Mevcut bir botun varsa onu da bağlayabilirsin, diğer işlevleri bozulmaz.\n\n"
                                   "Başlamak için botunun token'ını buraya yapıştır ya da paneli aç.")

    # ------------------------------------------------------------------ stats and background
    def stats(self, tenant) -> dict:
        db = self.store_for(tenant["id"]).db
        live = "NOT EXISTS (SELECT 1 FROM refunds r WHERE r.charge=p.charge)"
        total = db.execute(f"SELECT COUNT(*), COALESCE(SUM(stars),0) FROM payments p WHERE {live}").fetchone()
        month = db.execute(f"SELECT COUNT(*), COALESCE(SUM(stars),0) FROM payments p WHERE {live} AND created>=?",
                           (time.time() - 30 * 86400,)).fetchone()
        refunds = db.execute("SELECT COUNT(*) FROM refunds").fetchone()[0]
        buyers = db.execute(f"SELECT COUNT(DISTINCT user) FROM payments p WHERE {live}").fetchone()[0]
        recent = db.execute(
            "SELECT p.charge, p.user, p.stars, p.created, p.expires, o.title, o.kind, "
            "EXISTS (SELECT 1 FROM refunds r WHERE r.charge=p.charge) AS refunded "
            "FROM payments p JOIN orders o ON o.id=p.order_id ORDER BY p.created DESC LIMIT 30").fetchall()
        issues = self.store_for(tenant["id"]).open_issues()
        return {
            "payments": total[0], "stars": total[1], "payments_30d": month[0], "stars_30d": month[1],
            "refunds": refunds, "buyers": buyers,
            "recent": [dict(r) | {"refunded": bool(r["refunded"])} for r in recent],
            "issues": [dict(r) for r in issues],
        }

    async def redeliver_all(self) -> None:
        with contextlib.suppress(Exception):
            await self.billing.redeliver()
        for tenant in self.registry.all():
            try:
                await self.shop(tenant).redeliver()
            except Exception as exc:  # noqa: BLE001 - one tenant must not stop the others
                log.error("Kiracı %s yeniden teslim hatası: %s", tenant["id"], type(exc).__name__)

    def close(self) -> None:
        for store in self._stores.values():
            store.close()
        self.billing.store.close()
        self.registry.close()


# ---------------------------------------------------------------------- HTTP layer
class OrderRequest(BaseModel):
    product_id: str = Field(min_length=1, max_length=32)


class ConnectRequest(BaseModel):
    token: str = Field(min_length=10, max_length=128)
    confirm_forward: bool = False
    forward_secret: str | None = Field(None, max_length=256)


class ShopPatch(BaseModel):
    shop_name: str | None = Field(None, max_length=64)
    support_text: str | None = Field(None, max_length=1024)
    terms_text: str | None = Field(None, max_length=2048)
    sales_enabled: bool | None = None
    forward_secret: str | None = Field(None, max_length=256)


class RefundRequest(BaseModel):
    charge: str = Field(min_length=1, max_length=256)


def create_hub_app(settings: HubSettings | None = None, registry: Registry | None = None,
                   api_factory: ApiFactory | None = None, forwarder: Forwarder | None = None,
                   billing_store: Store | None = None, store_factory=None, background: bool = True) -> FastAPI:
    settings = settings or HubSettings.from_env()
    registry = registry or Registry(settings)
    client = httpx.AsyncClient(timeout=httpx.Timeout(40.0))
    api_factory = api_factory or (lambda token: BotAPI(token, client=client))

    async def http_forwarder(url: str, secret: str | None, update: dict) -> None:
        headers = {"X-Telegram-Bot-Api-Secret-Token": secret} if secret else {}
        for attempt in range(2):
            try:
                response = await client.post(url, json=update, headers=headers, timeout=10.0)
                if response.status_code < 500:
                    if response.status_code >= 400:
                        log.warning("Yönlendirilen güncelleme reddedildi (%s): gizli anahtarı kontrol et", response.status_code)
                    return
            except httpx.HTTPError as exc:
                log.warning("Yönlendirme başarısız (%s), deneme %s", type(exc).__name__, attempt + 1)
            await asyncio.sleep(0.5)

    hub = Hub(settings, registry, api_factory, forwarder or http_forwarder, billing_store, store_factory)

    async def loop():
        while True:
            await hub.redeliver_all()
            await asyncio.sleep(60)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = asyncio.create_task(loop()) if background else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await client.aclose()

    app = FastAPI(title=settings.name, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.hub = hub

    @app.exception_handler(ShopError)
    async def shop_error(_request: Request, exc: ShopError):
        body = {"detail": str(exc)}
        if isinstance(exc, NeedsConfirmation):
            body |= {"needs_confirm": True, "webhook_url": exc.webhook_url}
        return JSONResponse(body, status_code=exc.status)

    @app.exception_handler(ConfigError)
    async def config_error(_request: Request, exc: ConfigError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/health")
    async def health():
        return {"ok": True}

    # ----------------------------------------------------------- buyer Mini App per tenant
    def tenant_by_slug(slug: str):
        tenant = registry.by_slug(slug)
        if not tenant:
            raise HTTPException(404, "Mağaza bulunamadı.")
        return tenant

    def buyer(tenant, init_data: str | None) -> int:
        try:
            return initdata.validate(init_data or "", registry.token(tenant), settings.init_data_max_age)["id"]
        except initdata.InvalidInitData:
            raise HTTPException(401, "Telegram oturumu doğrulanamadı. Mağazayı bot üzerinden yeniden aç.") from None

    @app.get("/s/{slug}")
    @app.get("/s/{slug}/")
    @app.get("/s/{slug}/app")
    async def shop_index(slug: str):
        tenant_by_slug(slug)
        return assets.page(WEBAPP_DIR / "index.html")

    @app.get("/s/{slug}/api/config")
    async def shop_config(slug: str, x_telegram_init_data: str | None = Header(None)):
        tenant = tenant_by_slug(slug)
        shop_settings = registry.shop_settings(tenant)
        body = {"shop_name": shop_settings.shop_name, "sales_enabled": shop_settings.sales_enabled,
                "support": shop_settings.support_text, "terms": shop_settings.terms_text,
                "bot_username": tenant["username"]}
        if hub.plan(tenant["owner"]).branding:
            link = f"https://t.me/{settings.bot_username}" if settings.bot_username else settings.base_url
            body["powered_by"] = {"text": f"{settings.name} ile yapıldı", "url": link}
            if hub.buy_link():
                body["buy"] = {"text": f"Sen de Stars mağazası aç · ⭐{settings.pro_stars}/30 gün", "url": hub.buy_link()}
        try:
            viewer = initdata.validate(x_telegram_init_data or "", shop_settings.bot_token,
                                       settings.init_data_max_age)["id"]
        except initdata.InvalidInitData:
            viewer = None
        if viewer == tenant["owner"]:
            body["admin_url"] = hub.panel_link()  # only the shop owner sees the way to the panel
            if hub.buy_link():
                body["license_url"] = hub.buy_link()
        return body

    @app.get("/s/{slug}/api/products")
    async def shop_products(slug: str):
        return hub.shop(tenant_by_slug(slug)).catalog()

    @app.post("/s/{slug}/api/orders")
    async def shop_order(slug: str, body: OrderRequest, x_telegram_init_data: str | None = Header(None)):
        tenant = tenant_by_slug(slug)
        user = buyer(tenant, x_telegram_init_data)
        try:
            return await hub.shop(tenant).create_invoice(user, body.product_id)
        except TelegramError as exc:
            log.error("Kiracı %s fatura oluşturamadı: %s", tenant["id"], exc.description)
            raise HTTPException(502, "Fatura oluşturulamadı, biraz sonra tekrar dene.") from None

    @app.get("/s/{slug}/api/orders/{order_id}")
    async def shop_order_status(slug: str, order_id: str, x_telegram_init_data: str | None = Header(None)):
        tenant = tenant_by_slug(slug)
        return hub.shop(tenant).order_status(buyer(tenant, x_telegram_init_data), order_id)

    @app.get("/s/{slug}/api/purchases")
    async def shop_purchases(slug: str, x_telegram_init_data: str | None = Header(None)):
        tenant = tenant_by_slug(slug)
        return hub.shop(tenant).purchases(buyer(tenant, x_telegram_init_data))

    # ----------------------------------------------------------- webhooks
    @app.post("/t/{tenant_id}/webhook")
    async def tenant_webhook(tenant_id: str, request: Request,
                             x_telegram_bot_api_secret_token: str | None = Header(None)):
        tenant = registry.tenant(tenant_id)
        # Deleted tenants still get their late payments recorded, as long as the secret matches.
        if not tenant or not hmac.compare_digest(x_telegram_bot_api_secret_token or "", tenant["webhook_secret"]):
            raise HTTPException(403)
        update = await request.json()
        try:
            await hub.tenant_update(tenant, update)
        except Exception:
            raise HTTPException(500) from None
        return {"ok": True}

    @app.post("/hub/webhook")
    async def hub_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(None)):
        if not hmac.compare_digest(x_telegram_bot_api_secret_token or "", settings.webhook_secret):
            raise HTTPException(403)
        update = await request.json()
        try:
            await hub.hub_update(update)
        except Exception:
            raise HTTPException(500) from None
        return {"ok": True}

    # ----------------------------------------------------------- seller panel
    def owner_id(init_data: str | None) -> int:
        try:
            owner = initdata.validate(init_data or "", settings.bot_token, settings.init_data_max_age)["id"]
        except initdata.InvalidInitData:
            raise HTTPException(401, "Telegram oturumu doğrulanamadı. Paneli bot üzerinden yeniden aç.") from None
        registry.ensure_owner(owner)
        return owner

    def tenant_view(tenant) -> dict:
        products = [dict(p) for p in registry.products(tenant["id"])]
        limit = hub.plan(tenant["owner"]).max_products
        for index, product in enumerate(products):
            product["over_limit"] = index >= limit
            product.pop("tenant", None)
        return public_tenant(tenant, settings) | {"products": products}

    @app.get("/panel")
    @app.get("/panel/")
    async def panel_index():
        return assets.page(PANEL_DIR / "index.html")

    @app.get("/panel/api/me")
    async def panel_me(x_telegram_init_data: str | None = Header(None)):
        owner = owner_id(x_telegram_init_data)
        plan = hub.plan(owner)
        return {"owner": owner, "plan": plan.public(), "pro_stars": settings.pro_stars,
                "billing_enabled": settings.billing_enabled, "hub_name": settings.name,
                "shops": [public_tenant(t, settings) for t in registry.tenants_of(owner)]}

    @app.post("/panel/api/shops")
    async def panel_connect(body: ConnectRequest, x_telegram_init_data: str | None = Header(None)):
        owner = owner_id(x_telegram_init_data)
        tenant = await hub.connect(owner, body.token, body.confirm_forward, body.forward_secret)
        return tenant_view(tenant)

    @app.get("/panel/api/shops/{tenant_id}")
    async def panel_shop(tenant_id: str, x_telegram_init_data: str | None = Header(None)):
        tenant = hub.owned_tenant(owner_id(x_telegram_init_data), tenant_id)
        return tenant_view(tenant) | {"stats": hub.stats(tenant)}

    @app.patch("/panel/api/shops/{tenant_id}")
    async def panel_update(tenant_id: str, body: ShopPatch, x_telegram_init_data: str | None = Header(None)):
        tenant = hub.owned_tenant(owner_id(x_telegram_init_data), tenant_id)
        fields = body.model_dump(exclude_none=True)
        if "forward_secret" in fields and not tenant["forward_url"]:
            raise ShopError("Yönlendirme yalnızca mevcut sunucusu olan botlarda kullanılır.")
        merged = {k: fields.get(k, tenant[k]) for k in ("support_text", "terms_text")}
        if fields.get("sales_enabled") and (not merged["support_text"].strip() or not merged["terms_text"].strip()):
            raise ShopError("Satışı açmadan önce destek ve koşul metnini yaz (Telegram /paysupport ve /terms ister).")
        if fields.get("sales_enabled") and tenant["status"] != "active":
            raise ShopError("Mağaza askıya alınmış; satış açılamaz. Destekle iletişime geç.", 403)
        if fields.get("sales_enabled") and not registry.product_count(tenant_id):
            raise ShopError("Satışı açmadan önce en az bir ürün ekle.")
        return tenant_view(registry.update(tenant_id, **fields))

    @app.post("/panel/api/shops/{tenant_id}/sync")
    async def panel_sync(tenant_id: str, x_telegram_init_data: str | None = Header(None)):
        tenant = hub.owned_tenant(owner_id(x_telegram_init_data), tenant_id)
        await hub.sync(tenant)
        return tenant_view(registry.tenant(tenant_id))

    @app.delete("/panel/api/shops/{tenant_id}")
    async def panel_delete(tenant_id: str, x_telegram_init_data: str | None = Header(None)):
        tenant = hub.owned_tenant(owner_id(x_telegram_init_data), tenant_id)
        await hub.remove(tenant)
        return {"ok": True}

    @app.post("/panel/api/shops/{tenant_id}/products")
    async def panel_add_product(tenant_id: str, request: Request, x_telegram_init_data: str | None = Header(None)):
        tenant = hub.owned_tenant(owner_id(x_telegram_init_data), tenant_id)
        registry.save_product(tenant_id, await request.json(), hub.plan(tenant["owner"]).max_products, create=True)
        return tenant_view(tenant)

    @app.put("/panel/api/shops/{tenant_id}/products/{product_id}")
    async def panel_edit_product(tenant_id: str, product_id: str, request: Request,
                                 x_telegram_init_data: str | None = Header(None)):
        tenant = hub.owned_tenant(owner_id(x_telegram_init_data), tenant_id)
        item = await request.json()
        if not isinstance(item, dict):
            raise ShopError("Geçersiz ürün")
        item["id"] = product_id
        registry.save_product(tenant_id, item, hub.plan(tenant["owner"]).max_products, create=False)
        return tenant_view(tenant)

    @app.delete("/panel/api/shops/{tenant_id}/products/{product_id}")
    async def panel_archive_product(tenant_id: str, product_id: str, x_telegram_init_data: str | None = Header(None)):
        tenant = hub.owned_tenant(owner_id(x_telegram_init_data), tenant_id)
        if not registry.archive_product(tenant_id, product_id):
            raise ShopError("Ürün bulunamadı.", 404)
        return tenant_view(tenant)

    @app.post("/panel/api/shops/{tenant_id}/refund")
    async def panel_refund(tenant_id: str, body: RefundRequest, x_telegram_init_data: str | None = Header(None)):
        tenant = hub.owned_tenant(owner_id(x_telegram_init_data), tenant_id)
        try:
            refunded = await hub.shop(tenant).refund(body.charge)
        except TelegramError as exc:
            raise ShopError(f"Telegram iadeyi reddetti: {exc.description}", 502) from None
        return {"refunded": refunded}

    @app.post("/panel/api/plan/invoice")
    async def panel_plan_invoice(x_telegram_init_data: str | None = Header(None)):
        owner = owner_id(x_telegram_init_data)
        try:
            return await hub.billing.create_invoice(owner, PRO_ID)
        except TelegramError as exc:
            log.error("Plan faturası oluşturulamadı: %s", exc.description)
            raise HTTPException(502, "Fatura oluşturulamadı, biraz sonra tekrar dene.") from None

    @app.get("/panel/api/plan/orders/{order_id}")
    async def panel_plan_order(order_id: str, x_telegram_init_data: str | None = Header(None)):
        return hub.billing.order_status(owner_id(x_telegram_init_data), order_id)

    app.mount("/panel/static", StaticFiles(directory=PANEL_DIR), name="panel")
    app.mount("/static", StaticFiles(directory=WEBAPP_DIR), name="static")
    return app


def app() -> FastAPI:  # factory for `uvicorn app.hub:app --factory`
    logging.basicConfig(level=logging.INFO)
    return create_hub_app()
