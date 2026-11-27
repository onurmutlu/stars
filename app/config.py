"""Settings from the environment and the product catalog from JSON."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

# Telegram only accepts 30-day periods for Star subscriptions.
SUBSCRIPTION_PERIOD = 2592000
PRODUCT_ID = re.compile(r"^[a-z0-9_-]{1,32}$")
KINDS = ("one_time", "subscription")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Product:
    id: str
    title: str
    description: str
    stars: int
    content: str
    kind: str = "one_time"
    photo_url: str | None = None
    category: str | None = None   # Mini App filter chip, e.g. "Rehberler"
    badge: str | None = None      # short label on the card, e.g. "Yeni", "Popüler"
    channel_id: int | None = None # private channel/group: buyers get a one-time invite, removed when access ends

    def public(self) -> dict:
        """What the Mini App may see before payment: never the content."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "stars": self.stars,
            "kind": self.kind,
            "photo_url": self.photo_url,
            "category": self.category,
            "badge": self.badge,
            "channel": self.channel_id is not None,
        }

def _optional_text(value):
    """Blank strings become None; non-strings pass through so validation can reject them."""
    if isinstance(value, str):
        return value.strip() or None
    return value


def _channel_id(value):
    """-1001234567890 as a number or a string; blank means no channel. Anything else fails validation."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str) and re.fullmatch(r"\s*-\d{5,20}\s*", value):
        return int(value)
    return value


def validate_product(item: dict) -> Product:
    """Build a Product from a JSON object, or raise ConfigError with a message for the seller."""
    if not isinstance(item, dict):
        raise ConfigError("Ürün bir JSON nesnesi olmalı")
    product = Product(
        id=item.get("id", ""),
        title=item.get("title", ""),
        description=item.get("description", ""),
        stars=item.get("stars"),
        content=item.get("content", ""),
        kind=item.get("kind") or "one_time",
        photo_url=item.get("photo_url") or None,
        category=_optional_text(item.get("category")),
        badge=_optional_text(item.get("badge")),
        channel_id=_channel_id(item.get("channel_id")),
    )
    if not isinstance(product.id, str) or not PRODUCT_ID.match(product.id):
        raise ConfigError(f"Geçersiz ürün id: {product.id!r} (a-z, 0-9, _ ve -; en fazla 32)")
    if not isinstance(product.title, str) or not 1 <= len(product.title) <= 32:
        raise ConfigError(f"{product.id}: başlık 1–32 karakter olmalı")
    if not isinstance(product.description, str) or not 1 <= len(product.description) <= 255:
        raise ConfigError(f"{product.id}: açıklama 1–255 karakter olmalı")
    if not isinstance(product.stars, int) or isinstance(product.stars, bool) or not 1 <= product.stars <= 100000:
        raise ConfigError(f"{product.id}: stars 1–100000 arası bir tam sayı olmalı")
    if not isinstance(product.content, str) or not 1 <= len(product.content) <= 4096:
        raise ConfigError(f"{product.id}: teslim içeriği 1–4096 karakter olmalı")
    if product.kind not in KINDS:
        raise ConfigError(f"{product.id}: kind {KINDS} değerlerinden biri olmalı")
    if product.photo_url and (not isinstance(product.photo_url, str) or not product.photo_url.startswith("https://")
                              or len(product.photo_url) > 1024):
        raise ConfigError(f"{product.id}: photo_url https:// ile başlamalı")
    if product.category is not None and (not isinstance(product.category, str) or len(product.category) > 24):
        raise ConfigError(f"{product.id}: kategori en fazla 24 karakter olmalı")
    if product.badge is not None and (not isinstance(product.badge, str) or len(product.badge) > 16):
        raise ConfigError(f"{product.id}: etiket en fazla 16 karakter olmalı")
    if product.channel_id is not None and (not isinstance(product.channel_id, int) or isinstance(product.channel_id, bool)
                                           or product.channel_id >= 0):
        raise ConfigError(f"{product.id}: kanal ID'si -100 ile başlayan bir sayı olmalı (bot kanala eklenince sana yazar)")
    return product



def load_products(path: str | Path) -> dict[str, Product]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ConfigError("products.json bir liste olmalı")
    products: dict[str, Product] = {}
    for item in raw:
        product = validate_product(item)
        if product.id in products:
            raise ConfigError(f"Tekrarlanan ürün id: {product.id}")
        products[product.id] = product
    return products


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    bot_token: str
    webhook_secret: str
    webapp_url: str
    support_text: str
    terms_text: str
    shop_name: str = "Stars Mağaza"
    bot_username: str = ""
    contact_url: str = ""               # where the marketing site's "talk to us" buttons go
    admin_url: str = ""                 # shown to ADMIN_IDS in the Mini App (e.g. the Hub panel)
    buy_url: str = ""                   # "get your own shop / license" link in the Mini App and site
    buy_text: str = ""
    sales_enabled: bool = False
    db_path: str = "data/shop.sqlite3"
    products_path: str = "products.json"
    admin_ids: frozenset[int] = frozenset()
    init_data_max_age: int = 86400
    order_ttl: int = 3600
    checkout_urls: tuple[tuple[str, str], ...] = ()
    notify_sales: bool = True           # DM admins on every sale, renewal and refund
    summary_hour: int = 9               # local hour of the daily summary to admins; -1 turns it off
    cart_reminder: bool = True          # one reminder to a buyer who left an order unpaid for an hour

    @classmethod
    def from_env(cls) -> "Settings":
        missing = [k for k in ("BOT_TOKEN", "WEBHOOK_SECRET", "WEBAPP_URL") if not os.getenv(k)]
        if missing:
            raise ConfigError("Eksik ortam değişkeni: " + ", ".join(missing))
        settings = cls(
            bot_token=os.environ["BOT_TOKEN"],
            webhook_secret=os.environ["WEBHOOK_SECRET"],
            webapp_url=os.environ["WEBAPP_URL"].rstrip("/"),
            support_text=os.getenv("SUPPORT_TEXT", ""),
            terms_text=os.getenv("TERMS_TEXT", ""),
            shop_name=os.getenv("SHOP_NAME", "Stars Mağaza"),
            bot_username=os.getenv("BOT_USERNAME", "").lstrip("@"),
            contact_url=os.getenv("CONTACT_URL", ""),
            admin_url=os.getenv("ADMIN_URL", "").strip(),
            buy_url=os.getenv("BUY_URL", "").strip(),
            buy_text=os.getenv("BUY_TEXT", "").strip(),
            sales_enabled=_bool(os.getenv("SALES_ENABLED")),
            db_path=os.getenv("DB_PATH", "data/shop.sqlite3"),
            products_path=os.getenv("PRODUCTS_PATH", "products.json"),
            admin_ids=frozenset(int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x),
            init_data_max_age=int(os.getenv("INIT_DATA_MAX_AGE", "86400")),
            order_ttl=int(os.getenv("ORDER_TTL", "3600")),
            notify_sales=os.getenv("NOTIFY_SALES", "true").strip().lower() not in ("0", "false", "no", "off"),
            summary_hour=int(os.getenv("DAILY_SUMMARY_HOUR", "9")),
            cart_reminder=os.getenv("CART_REMINDER", "true").strip().lower() not in ("0", "false", "no", "off"),
            checkout_urls=tuple(
                (plan, url.strip())
                for plan, key in (
                    ("hizli-gecis", "CHECKOUT_HIZLI_GECIS_URL"),
                    ("tam-gecis", "CHECKOUT_TAM_GECIS_URL"),
                    ("bakim", "CHECKOUT_BAKIM_URL"),
                )
                if (url := os.getenv(key, "")).strip()
            ),
        )
        settings.check()
        return settings

    def check(self) -> None:
        if len(self.webhook_secret) < 32 or not re.fullmatch(r"[A-Za-z0-9_-]+", self.webhook_secret):
            raise ConfigError("WEBHOOK_SECRET en az 32 karakter olmalı (A-Z, a-z, 0-9, _ ve -)")
        if not self.webapp_url.startswith("https://"):
            raise ConfigError("WEBAPP_URL https:// ile başlamalı (Telegram Mini App şartı)")
        for name, url in (("ADMIN_URL", self.admin_url), ("BUY_URL", self.buy_url)):
            if url and not url.startswith("https://"):
                raise ConfigError(f"{name} https:// ile başlamalı")
        if self.contact_url and not self.contact_url.startswith("https://"):
            raise ConfigError("CONTACT_URL https:// ile başlamalı (ör. https://t.me/kullaniciadi)")
        for plan, url in self.checkout_urls:
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ConfigError(f"{plan}: ödeme bağlantısı geçerli bir HTTPS adresi olmalı")
        if self.sales_enabled and (not self.support_text or not self.terms_text):
            raise ConfigError("Satış açıkken SUPPORT_TEXT ve TERMS_TEXT zorunlu (/paysupport ve /terms)")
