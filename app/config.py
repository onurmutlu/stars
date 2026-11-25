"""Settings from the environment and the product catalog from JSON."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

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

    def public(self) -> dict:
        """What the Mini App may see before payment: never the content."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "stars": self.stars,
            "kind": self.kind,
            "photo_url": self.photo_url,
        }


def load_products(path: str | Path) -> dict[str, Product]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ConfigError("products.json bir liste olmalı")
    products: dict[str, Product] = {}
    for item in raw:
        product = Product(
            id=item.get("id", ""),
            title=item.get("title", ""),
            description=item.get("description", ""),
            stars=item.get("stars"),
            content=item.get("content", ""),
            kind=item.get("kind", "one_time"),
            photo_url=item.get("photo_url"),
        )
        if not PRODUCT_ID.match(product.id):
            raise ConfigError(f"Geçersiz ürün id: {product.id!r} (a-z, 0-9, _ ve -; en fazla 32)")
        if product.id in products:
            raise ConfigError(f"Tekrarlanan ürün id: {product.id}")
        if not 1 <= len(product.title) <= 32:
            raise ConfigError(f"{product.id}: başlık 1–32 karakter olmalı")
        if not 1 <= len(product.description) <= 255:
            raise ConfigError(f"{product.id}: açıklama 1–255 karakter olmalı")
        if not isinstance(product.stars, int) or isinstance(product.stars, bool) or product.stars < 1:
            raise ConfigError(f"{product.id}: stars pozitif bir tam sayı olmalı")
        if not 1 <= len(product.content) <= 4096:
            raise ConfigError(f"{product.id}: teslim içeriği 1–4096 karakter olmalı")
        if product.kind not in KINDS:
            raise ConfigError(f"{product.id}: kind {KINDS} değerlerinden biri olmalı")
        if product.photo_url and not product.photo_url.startswith("https://"):
            raise ConfigError(f"{product.id}: photo_url https:// ile başlamalı")
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
    sales_enabled: bool = False
    db_path: str = "data/shop.sqlite3"
    products_path: str = "products.json"
    admin_ids: frozenset[int] = frozenset()
    init_data_max_age: int = 86400
    order_ttl: int = 3600

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
            sales_enabled=_bool(os.getenv("SALES_ENABLED")),
            db_path=os.getenv("DB_PATH", "data/shop.sqlite3"),
            products_path=os.getenv("PRODUCTS_PATH", "products.json"),
            admin_ids=frozenset(int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x),
            init_data_max_age=int(os.getenv("INIT_DATA_MAX_AGE", "86400")),
            order_ttl=int(os.getenv("ORDER_TTL", "3600")),
        )
        settings.check()
        return settings

    def check(self) -> None:
        if len(self.webhook_secret) < 32 or not re.fullmatch(r"[A-Za-z0-9_-]+", self.webhook_secret):
            raise ConfigError("WEBHOOK_SECRET en az 32 karakter olmalı (A-Z, a-z, 0-9, _ ve -)")
        if not self.webapp_url.startswith("https://"):
            raise ConfigError("WEBAPP_URL https:// ile başlamalı (Telegram Mini App şartı)")
        if self.contact_url and not self.contact_url.startswith("https://"):
            raise ConfigError("CONTACT_URL https:// ile başlamalı (ör. https://t.me/kullaniciadi)")
        if self.sales_enabled and (not self.support_text or not self.terms_text):
            raise ConfigError("Satış açıkken SUPPORT_TEXT ve TERMS_TEXT zorunlu (/paysupport ve /terms)")
