"""Multi-tenant registry: other bot owners rent a ready Stars shop on this server.

Money never passes through the platform. Each tenant connects their own bot, every
invoice is created with that bot's token, so Stars land in the tenant's own bot
balance and they withdraw it on Fragment themselves. The platform earns from its
own plan (a monthly Star subscription sold by the hub bot, see hub.py).

Isolation:
  * bot tokens are encrypted at rest (Fernet, HUB_MASTER_KEY) and never returned by any API;
  * every tenant has its own SQLite file for orders and payments, so one tenant's data
    can be exported, inspected or deleted without touching another's;
  * every tenant has its own webhook path and secret.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import ConfigError, Product, Settings, _bool, validate_product

TOKEN_RE = re.compile(r"^\d{5,16}:[A-Za-z0-9_-]{30,64}$")
STATUSES = ("active", "suspended", "deleted")

SCHEMA = """
CREATE TABLE IF NOT EXISTS owners(
    id INTEGER PRIMARY KEY,           -- Telegram user id of the bot owner
    created REAL NOT NULL,
    trial_until REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tenants(
    id TEXT PRIMARY KEY,
    owner INTEGER NOT NULL REFERENCES owners(id),
    bot_id INTEGER NOT NULL UNIQUE,
    username TEXT NOT NULL UNIQUE,    -- bot username, lower case; also the shop slug
    token_enc BLOB NOT NULL,
    webhook_secret TEXT NOT NULL,
    shop_name TEXT NOT NULL,
    support_text TEXT NOT NULL DEFAULT '',
    terms_text TEXT NOT NULL DEFAULT '',
    sales_enabled INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    -- forward mode: the bot already had a backend; updates the shop does not own go there
    forward_url TEXT,
    forward_secret TEXT,
    forward_updates TEXT,             -- JSON list of the bot's previous allowed_updates
    last_sync REAL,
    last_sync_error TEXT,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tenants_owner ON tenants(owner);
CREATE TABLE IF NOT EXISTS products(
    tenant TEXT NOT NULL REFERENCES tenants(id),
    id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    stars INTEGER NOT NULL,
    content TEXT NOT NULL,
    kind TEXT NOT NULL,
    photo_url TEXT,
    category TEXT,
    badge TEXT,
    channel_id INTEGER,
    access_days INTEGER,
    position INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    updated REAL NOT NULL,
    PRIMARY KEY(tenant, id)
);
"""


@dataclass(frozen=True)
class Plan:
    name: str            # "pro" | "trial" | "free"
    until: float | None
    max_shops: int
    max_products: int
    branding: bool       # show "made with" footer in the buyer Mini App

    def public(self) -> dict:
        return {"name": self.name, "until": self.until, "max_shops": self.max_shops,
                "max_products": self.max_products, "branding": self.branding}


@dataclass(frozen=True)
class HubSettings:
    bot_token: str
    webhook_secret: str
    base_url: str
    master_key: str
    support_text: str
    terms_text: str
    name: str = "SiyahKare Stars"
    bot_username: str = ""
    pro_stars: int = 1000
    trial_days: int = 14
    billing_enabled: bool = True
    admin_ids: frozenset[int] = frozenset()
    data_dir: str = "data/hub"
    free_shops: int = 1
    free_products: int = 3
    pro_shops: int = 5
    pro_products: int = 100
    init_data_max_age: int = 86400

    @classmethod
    def from_env(cls) -> "HubSettings":
        need = ("HUB_BOT_TOKEN", "HUB_WEBHOOK_SECRET", "HUB_BASE_URL", "HUB_MASTER_KEY")
        missing = [k for k in need if not os.getenv(k)]
        if missing:
            raise ConfigError("Eksik ortam değişkeni: " + ", ".join(missing))
        env = os.getenv
        settings = cls(
            bot_token=env("HUB_BOT_TOKEN"),
            webhook_secret=env("HUB_WEBHOOK_SECRET"),
            base_url=env("HUB_BASE_URL").rstrip("/"),
            master_key=env("HUB_MASTER_KEY"),
            support_text=env("HUB_SUPPORT_TEXT", ""),
            terms_text=env("HUB_TERMS_TEXT", ""),
            name=env("HUB_NAME", "SiyahKare Stars"),
            bot_username=env("HUB_BOT_USERNAME", "").lstrip("@"),
            pro_stars=int(env("HUB_PRO_STARS", "1000")),
            trial_days=int(env("HUB_TRIAL_DAYS", "14")),
            billing_enabled=_bool(env("HUB_BILLING_ENABLED", "true")),
            admin_ids=frozenset(int(x) for x in env("HUB_ADMIN_IDS", "").replace(" ", "").split(",") if x),
            data_dir=env("HUB_DATA_DIR", "data/hub"),
            free_shops=int(env("HUB_FREE_SHOPS", "1")),
            free_products=int(env("HUB_FREE_PRODUCTS", "3")),
            pro_shops=int(env("HUB_PRO_SHOPS", "5")),
            pro_products=int(env("HUB_PRO_PRODUCTS", "100")),
        )
        settings.check()
        return settings

    def check(self) -> None:
        if len(self.webhook_secret) < 32 or not re.fullmatch(r"[A-Za-z0-9_-]+", self.webhook_secret):
            raise ConfigError("HUB_WEBHOOK_SECRET en az 32 karakter olmalı (A-Z, a-z, 0-9, _ ve -)")
        if not self.base_url.startswith("https://"):
            raise ConfigError("HUB_BASE_URL https:// ile başlamalı")
        try:
            Fernet(self.master_key.encode())
        except (ValueError, TypeError):
            raise ConfigError("HUB_MASTER_KEY geçerli bir Fernet anahtarı değil. Üret: "
                              "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"") from None
        if self.billing_enabled and (not self.support_text or not self.terms_text):
            raise ConfigError("Plan satışı açıkken HUB_SUPPORT_TEXT ve HUB_TERMS_TEXT zorunlu")
        if self.pro_stars < 1:
            raise ConfigError("HUB_PRO_STARS pozitif olmalı")

    def shop_url(self, slug: str) -> str:
        return f"{self.base_url}/s/{slug}"

    def webhook_url(self, tenant_id: str) -> str:
        return f"{self.base_url}/t/{tenant_id}/webhook"


class Registry:
    """Tenants, their settings and catalogs. Payments live in per-tenant Store files."""

    def __init__(self, hub: HubSettings, path: str | Path | None = None):
        self.hub = hub
        self.fernet = Fernet(hub.master_key.encode())
        path = str(path if path is not None else Path(hub.data_dir) / "hub.sqlite3")
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(products)")}
        for column, kind in (("category", "TEXT"), ("badge", "TEXT"), ("channel_id", "INTEGER"),
                             ("access_days", "INTEGER")):
            if column not in columns:  # databases created before these fields
                self.db.execute(f"ALTER TABLE products ADD COLUMN {column} {kind}")
        self.db.commit()

    # owners and plans ----------------------------------------------------------
    def ensure_owner(self, owner: int, now: float | None = None) -> sqlite3.Row:
        now = time.time() if now is None else now
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO owners VALUES(?,?,?)",
                            (owner, now, now + self.hub.trial_days * 86400))
        return self.db.execute("SELECT * FROM owners WHERE id=?", (owner,)).fetchone()

    def plan(self, owner: int, pro_until: float | None, now: float | None = None) -> Plan:
        """pro_until comes from the hub's own Stars subscription (billing store)."""
        now = time.time() if now is None else now
        hub = self.hub
        if pro_until and pro_until > now:
            return Plan("pro", pro_until, hub.pro_shops, hub.pro_products, False)
        row = self.db.execute("SELECT trial_until FROM owners WHERE id=?", (owner,)).fetchone()
        if row and row["trial_until"] > now:
            return Plan("trial", row["trial_until"], hub.pro_shops, hub.pro_products, False)
        return Plan("free", None, hub.free_shops, hub.free_products, True)

    # tenants -------------------------------------------------------------------
    def encrypt(self, token: str) -> bytes:
        return self.fernet.encrypt(token.encode())

    def token(self, tenant: sqlite3.Row) -> str:
        try:
            return self.fernet.decrypt(tenant["token_enc"]).decode()
        except InvalidToken:
            raise ConfigError(f"{tenant['id']}: bot token'ı çözülemedi (HUB_MASTER_KEY değişmiş olabilir)") from None

    def tenant(self, tenant_id: str):
        return self.db.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()

    def by_slug(self, slug: str):
        return self.db.execute("SELECT * FROM tenants WHERE username=? AND status!='deleted'",
                               (slug.lower(),)).fetchone()

    def by_bot(self, bot_id: int):
        return self.db.execute("SELECT * FROM tenants WHERE bot_id=?", (bot_id,)).fetchone()

    def tenants_of(self, owner: int):
        return self.db.execute("SELECT * FROM tenants WHERE owner=? AND status!='deleted' ORDER BY created",
                               (owner,)).fetchall()

    def all(self, include_deleted: bool = False):
        sql = "SELECT * FROM tenants" + ("" if include_deleted else " WHERE status!='deleted'") + " ORDER BY created"
        return self.db.execute(sql).fetchall()

    def create(self, owner: int, token: str, bot: dict) -> sqlite3.Row:
        tenant_id = secrets.token_hex(8)
        with self.db:
            self.db.execute(
                "INSERT INTO tenants(id,owner,bot_id,username,token_enc,webhook_secret,shop_name,created) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (tenant_id, owner, bot["id"], bot["username"].lower(), self.encrypt(token),
                 secrets.token_urlsafe(32), (bot.get("first_name") or bot["username"])[:64], time.time()),
            )
        return self.tenant(tenant_id)

    def reconnect(self, tenant: sqlite3.Row, token: str, bot: dict) -> sqlite3.Row:
        """Same owner, same bot, new token (e.g. after /revoke in BotFather) or a deleted shop revived."""
        with self.db:
            self.db.execute(
                "UPDATE tenants SET token_enc=?, username=?, "
                "status=CASE status WHEN 'deleted' THEN 'active' ELSE status END WHERE id=?",
                (self.encrypt(token), bot["username"].lower(), tenant["id"]),
            )
        return self.tenant(tenant["id"])

    EDITABLE = {"shop_name": 64, "support_text": 1024, "terms_text": 2048}

    def update(self, tenant_id: str, **fields) -> sqlite3.Row:
        sets, values = [], []
        for key, value in fields.items():
            if key in self.EDITABLE:
                value = (value or "").strip()
                if len(value) > self.EDITABLE[key]:
                    raise ConfigError(f"{key} en fazla {self.EDITABLE[key]} karakter olabilir")
                if key == "shop_name" and not value:
                    raise ConfigError("Mağaza adı boş olamaz")
            elif key == "sales_enabled":
                value = int(bool(value))
            elif key == "status":
                if value not in STATUSES:
                    raise ConfigError("Geçersiz durum")
            elif key == "forward_url":
                value = (value or "").strip() or None
                if value and (not value.startswith("https://") or len(value) > 512):
                    raise ConfigError("Yönlendirme adresi https:// ile başlamalı")
            elif key == "forward_secret":
                value = (value or "").strip() or None
                if value and not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value):
                    raise ConfigError("Yönlendirme gizli anahtarı yalnızca A-Z, a-z, 0-9, _ ve - içerebilir")
            elif key == "forward_updates":
                value = json.dumps(sorted(set(value))) if value else None
            elif key in ("last_sync", "last_sync_error"):
                pass
            else:
                raise ConfigError(f"Değiştirilemez alan: {key}")
            sets.append(f"{key}=?")
            values.append(value)
        if sets:
            with self.db:
                self.db.execute(f"UPDATE tenants SET {', '.join(sets)} WHERE id=?", (*values, tenant_id))
        return self.tenant(tenant_id)

    # products ------------------------------------------------------------------
    def products(self, tenant_id: str, include_archived: bool = False):
        sql = "SELECT * FROM products WHERE tenant=?" + ("" if include_archived else " AND archived=0")
        return self.db.execute(sql + " ORDER BY position, updated", (tenant_id,)).fetchall()

    def product_count(self, tenant_id: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM products WHERE tenant=? AND archived=0",
                               (tenant_id,)).fetchone()[0]

    def save_product(self, tenant_id: str, item: dict, limit: int, create: bool) -> Product:
        product = validate_product(item)
        existing = self.db.execute("SELECT archived FROM products WHERE tenant=? AND id=?",
                                   (tenant_id, product.id)).fetchone()
        if create and existing and not existing["archived"]:
            raise ConfigError(f"'{product.id}' kimlikli ürün zaten var")
        if not create and not existing:
            raise ConfigError("Ürün bulunamadı")
        if (not existing or existing["archived"]) and self.product_count(tenant_id) >= limit:
            raise ConfigError(f"Planındaki ürün sınırına ({limit}) ulaştın. Daha fazlası için Pro'ya geç.")
        position = item.get("position")
        if not isinstance(position, int) or isinstance(position, bool):
            position = self.db.execute("SELECT COALESCE(MAX(position),0)+1 FROM products WHERE tenant=?",
                                       (tenant_id,)).fetchone()[0] if not existing else None
        with self.db:
            self.db.execute(
                "INSERT INTO products(tenant,id,title,description,stars,content,kind,photo_url,category,badge,channel_id,"
                "access_days,position,archived,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,?) "
                "ON CONFLICT(tenant,id) DO UPDATE SET "
                "title=excluded.title, description=excluded.description, stars=excluded.stars, "
                "content=excluded.content, kind=excluded.kind, photo_url=excluded.photo_url, "
                "category=excluded.category, badge=excluded.badge, channel_id=excluded.channel_id, "
                "access_days=excluded.access_days, archived=0, "
                "updated=excluded.updated, position=COALESCE(?, products.position)",
                (tenant_id, product.id, product.title, product.description, product.stars, product.content,
                 product.kind, product.photo_url, product.category, product.badge, product.channel_id,
                 product.access_days, position or 0, time.time(), position),
            )
        return product

    def archive_product(self, tenant_id: str, product_id: str) -> bool:
        """Paid orders keep a snapshot of the content, so archiving never breaks a purchase."""
        with self.db:
            cur = self.db.execute("UPDATE products SET archived=1, updated=? WHERE tenant=? AND id=? AND archived=0",
                                  (time.time(), tenant_id, product_id))
        return cur.rowcount > 0

    def catalog(self, tenant_id: str, limit: int) -> dict[str, Product]:
        """The products the shop may sell under the owner's current plan (first N by position)."""
        rows = self.products(tenant_id)[:limit]
        return {r["id"]: Product(r["id"], r["title"], r["description"], r["stars"], r["content"],
                                 r["kind"], r["photo_url"], r["category"], r["badge"], r["channel_id"],
                                 r["access_days"])
                for r in rows}

    # runtime settings ------------------------------------------------------------
    def shop_settings(self, tenant: sqlite3.Row) -> Settings:
        """Settings for the shared StarsShop engine. Admin of a tenant shop is its owner."""
        sales = bool(tenant["sales_enabled"]) and tenant["status"] == "active" \
            and bool(tenant["support_text"]) and bool(tenant["terms_text"])
        return Settings(
            bot_token=self.token(tenant),
            webhook_secret=tenant["webhook_secret"],
            webapp_url=self.hub.shop_url(tenant["username"]),
            support_text=tenant["support_text"],
            terms_text=tenant["terms_text"],
            shop_name=tenant["shop_name"],
            sales_enabled=sales,
            db_path=str(self.store_path(tenant["id"])),
            admin_ids=frozenset({tenant["owner"]}),
            init_data_max_age=self.hub.init_data_max_age,
            bot_username=tenant["username"],
            admin_url=f"https://t.me/{self.hub.bot_username}?start=panel" if self.hub.bot_username else "",
            buy_url=f"https://t.me/{self.hub.bot_username}?start=lisans" if self.hub.bot_username else "",
        )

    def store_path(self, tenant_id: str) -> Path:
        return Path(self.hub.data_dir) / "tenants" / f"{tenant_id}.sqlite3"

    def close(self) -> None:
        self.db.close()


def public_tenant(tenant: sqlite3.Row, hub: HubSettings) -> dict:
    """Tenant fields an owner may see. Never the token or the webhook secret."""
    return {
        "id": tenant["id"], "username": tenant["username"], "shop_name": tenant["shop_name"],
        "support_text": tenant["support_text"], "terms_text": tenant["terms_text"],
        "sales_enabled": bool(tenant["sales_enabled"]), "status": tenant["status"],
        "mode": "forward" if tenant["forward_url"] else "standalone",
        "forward_url": tenant["forward_url"], "forward_secret_set": bool(tenant["forward_secret"]),
        "shop_url": hub.shop_url(tenant["username"]),
        "bot_link": f"https://t.me/{tenant['username']}",
        "last_sync": tenant["last_sync"], "last_sync_error": tenant["last_sync_error"],
        "created": tenant["created"],
    }
