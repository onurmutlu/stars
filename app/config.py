"""Settings from the environment and the product catalog from JSON."""
from __future__ import annotations

import json
import os
import re
import time
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
    access_days: int | None = None # one-time purchase that gives N days of access (e.g. 7, 90, 365)
    includes: tuple[str, ...] = () # bundle: buying this also gives these one-time products
    stock: int | None = None       # at most this many sales ("ilk 50 kişi")
    sale_until: float | None = None # sales close at this moment (server time); "2026-12-05" means end of that day
    compare_at: int | None = None  # old price shown struck through (must be above stars)
    upsell: str | None = None      # after paying, the buyer is offered this product...
    upsell_percent: int | None = None  # ...with this discount (a one-use code, 48 hours)
    group: str | None = None       # products with the same group are options of one card ("Kurs")
    option: str | None = None      # this product's option label in the group ("7 gün", "30 gün")
    ask: str | None = None         # a question the buyer answers before paying ("E-posta adresin?")
    flexible: bool = False         # pay what you want: stars is the minimum, the buyer may pay more

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
            "access_days": self.access_days,
            "includes": list(self.includes),
            "stock": self.stock,
            "sale_until": self.sale_until,
            "compare_at": self.compare_at,
            "group": self.group,
            "option": self.option,
            "ask": self.ask,
            "flexible": self.flexible,
        }

def _optional_text(value):
    """Blank strings become None; non-strings pass through so validation can reject them."""
    if isinstance(value, str):
        return value.strip() or None
    return value


def _moment(value):
    """Epoch seconds, or a local date/time: 2026-12-05 (end of that day), 2026-12-05 18:00, 2026-12-05T18:00."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace("T", " ")
        for fmt, end_of_day in (("%Y-%m-%d %H:%M", False), ("%Y-%m-%d %H:%M:%S", False), ("%Y-%m-%d", True)):
            try:
                parsed = time.strptime(text, fmt)
            except ValueError:
                continue
            return time.mktime(parsed) + (86399 if end_of_day else 0)
    return value  # validation reports it


def _id_list(value):
    if value is None or value == "" or value == []:
        return ()
    if isinstance(value, str):
        value = [part for part in re.split(r"[\s,]+", value) if part]
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return tuple(v.strip().lower() for v in value)
    return value


def _optional_int(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
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
        access_days=_optional_int(item.get("access_days")),
        includes=_id_list(item.get("includes")),
        stock=_optional_int(item.get("stock")),
        sale_until=_moment(item.get("sale_until")),
        compare_at=_optional_int(item.get("compare_at")),
        upsell=(_optional_text(item.get("upsell")) or None) and str(item.get("upsell")).strip().lower(),
        upsell_percent=_optional_int(item.get("upsell_percent")),
        group=_optional_text(item.get("group")),
        option=_optional_text(item.get("option")),
        ask=_optional_text(item.get("ask")),
        flexible=item.get("flexible") in (True, 1, "1", "true", "on"),
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
    if product.access_days is not None:
        if not isinstance(product.access_days, int) or isinstance(product.access_days, bool) \
                or not 1 <= product.access_days <= 3650:
            raise ConfigError(f"{product.id}: erişim süresi 1–3650 gün olmalı")
        if product.kind == "subscription":
            raise ConfigError(f"{product.id}: abonelik zaten 30 günde bir yenilenir; süre sadece tek seferlik üründe kullanılır")
    if product.stock is not None and (not isinstance(product.stock, int) or isinstance(product.stock, bool)
                                      or not 1 <= product.stock <= 1_000_000):
        raise ConfigError(f"{product.id}: stok 1 ile 1.000.000 arasında olmalı")
    if product.sale_until is not None and not isinstance(product.sale_until, float):
        raise ConfigError(f"{product.id}: satış bitişi 2026-12-05 ya da 2026-12-05 18:00 biçiminde olmalı")
    if product.compare_at is not None and (not isinstance(product.compare_at, int) or isinstance(product.compare_at, bool)
                                           or not product.stars < product.compare_at <= 1_000_000):
        raise ConfigError(f"{product.id}: eski fiyat satış fiyatından büyük olmalı")
    if product.upsell is not None:
        if not PRODUCT_ID.match(product.upsell) or product.upsell == product.id:
            raise ConfigError(f"{product.id}: önerilen ürün başka bir ürünün kimliği olmalı")
    if product.upsell_percent is not None:
        if not isinstance(product.upsell_percent, int) or isinstance(product.upsell_percent, bool) \
                or not 1 <= product.upsell_percent <= 90:
            raise ConfigError(f"{product.id}: öneri indirimi 1–90 arası olmalı")
        if product.upsell is None:
            raise ConfigError(f"{product.id}: öneri indirimi için önerilen ürünü de seç")
    if product.group is not None and (not isinstance(product.group, str) or len(product.group) > 32):
        raise ConfigError(f"{product.id}: seçenek grubu en fazla 32 karakter olmalı")
    if product.option is not None:
        if not isinstance(product.option, str) or len(product.option) > 20:
            raise ConfigError(f"{product.id}: seçenek adı en fazla 20 karakter olmalı")
        if product.group is None:
            raise ConfigError(f"{product.id}: seçenek adı için grup da yaz (ör. grup 'Kurs', seçenek '30 gün')")
    if product.group is not None and product.option is None:
        raise ConfigError(f"{product.id}: grupta her ürünün bir seçenek adı olmalı (ör. '7 gün')")
    if product.ask is not None and (not isinstance(product.ask, str) or len(product.ask) > 80):
        raise ConfigError(f"{product.id}: alıcıya sorulacak soru en fazla 80 karakter olmalı")
    if product.flexible and product.kind == "subscription":
        raise ConfigError(f"{product.id}: abonelikte 'istediğin kadar öde' kullanılamaz")
    if product.includes:
        if not isinstance(product.includes, tuple) or len(product.includes) > 10 or len(set(product.includes)) != len(product.includes):
            raise ConfigError(f"{product.id}: paket en fazla 10 farklı ürün kimliği içerebilir")
        if product.id in product.includes:
            raise ConfigError(f"{product.id}: paket kendini içeremez")
        if product.kind == "subscription":
            raise ConfigError(f"{product.id}: abonelik paket olamaz")
    return product


def check_bundles(products: dict[str, Product]) -> None:
    """A bundle may only hold existing one-time products that are not bundles themselves."""
    for product in products.values():
        for child_id in product.includes:
            child = products.get(child_id)
            if child is None:
                raise ConfigError(f"{product.id}: pakette olmayan ürün var: {child_id}")
            if child.kind == "subscription" or child.includes:
                raise ConfigError(f"{product.id}: pakete abonelik ya da başka bir paket konamaz ({child_id})")



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
    check_bundles(products)
    for product in products.values():
        if product.upsell and product.upsell not in products:
            raise ConfigError(f"{product.id}: önerilen ürün bulunamadı: {product.upsell}")
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
    cart_discount: int = 0              # 1..90: the unpaid-order reminder carries a one-use code for this product
    sale_webhook: str = ""              # https URL that gets a JSON POST for every sale (Zapier, Make, Sheets…)
    review_request: bool = True         # a day after a purchase, ask the buyer for a rating
    referral_percent: int = 0           # 1..90: a buyer whose friend makes a first purchase gets a code; 0 = off

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
            cart_discount=max(0, min(90, int(os.getenv("CART_DISCOUNT", "0") or 0))),
            sale_webhook=os.getenv("SALE_WEBHOOK_URL", "").strip(),
            review_request=os.getenv("REVIEW_REQUEST", "true").strip().lower() not in ("0", "false", "no", "off"),
            referral_percent=max(0, min(90, int(os.getenv("REFERRAL_PERCENT", "0") or 0))),
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
