"""SQLite persistence for orders, Stars payments, refunds and operator issues.

SQLite fits one process on one machine. For several workers or containers move
these tables to PostgreSQL; the queries are plain SQL.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders(
    id TEXT PRIMARY KEY,
    user INTEGER NOT NULL,
    product TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    stars INTEGER NOT NULL,
    content TEXT NOT NULL,          -- snapshot: editing products.json never changes a paid order
    created REAL NOT NULL,
    paid INTEGER NOT NULL DEFAULT 0,
    delivered INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS orders_user ON orders(user);
CREATE TABLE IF NOT EXISTS payments(
    charge TEXT PRIMARY KEY,        -- telegram_payment_charge_id, unique per payment
    order_id TEXT NOT NULL REFERENCES orders(id),
    user INTEGER NOT NULL,
    stars INTEGER NOT NULL,
    created REAL NOT NULL,
    expires REAL,                   -- subscription_expiration_date for subscriptions
    recurring INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS payments_order ON payments(order_id);
CREATE TABLE IF NOT EXISTS refunds(charge TEXT PRIMARY KEY, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS payment_issues(
    charge TEXT PRIMARY KEY, user INTEGER, stars INTEGER, payload TEXT, reason TEXT, created REAL);
CREATE TABLE IF NOT EXISTS delivery_state(
    order_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0, last_attempt REAL);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value INTEGER);
-- one row per order that grants a channel: the invite sent and when access was taken back
CREATE TABLE IF NOT EXISTS channel_access(
    order_id TEXT PRIMARY KEY, user INTEGER NOT NULL, channel INTEGER NOT NULL, invite TEXT,
    created REAL NOT NULL, removed REAL, attempts INTEGER NOT NULL DEFAULT 0);
-- channels and groups the bot was made admin of (from my_chat_member updates)
CREATE TABLE IF NOT EXISTS channels(
    id INTEGER PRIMARY KEY, title TEXT, type TEXT, status TEXT, can_invite INTEGER, can_restrict INTEGER, updated REAL);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        if str(path) != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        if "channel" not in {row[1] for row in self.db.execute("PRAGMA table_info(orders)")}:
            self.db.execute("ALTER TABLE orders ADD COLUMN channel INTEGER")  # databases from before channels
            self.db.commit()

    # orders -----------------------------------------------------------------
    def create_order(self, user: int, product) -> str:
        order_id = uuid.uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO orders(id,user,product,kind,title,stars,content,created,channel) VALUES(?,?,?,?,?,?,?,?,?)",
                (order_id, user, product.id, product.kind, product.title, product.stars, product.content, time.time(),
                 getattr(product, "channel_id", None)),
            )
        return order_id

    def order(self, order_id: str):
        return self.db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()

    def payment(self, charge: str):
        return self.db.execute("SELECT * FROM payments WHERE charge=?", (charge,)).fetchone()

    def add_payment(self, charge: str, order, expires: float | None, recurring: bool) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO payments(charge,order_id,user,stars,created,expires,recurring) VALUES(?,?,?,?,?,?,?)",
                (charge, order["id"], order["user"], order["stars"], time.time(), expires, int(recurring)),
            )
            self.db.execute("UPDATE orders SET paid=1 WHERE id=?", (order["id"],))

    def live_payments(self, order_id: str):
        return self.db.execute(
            "SELECT p.* FROM payments p WHERE p.order_id=? "
            "AND NOT EXISTS (SELECT 1 FROM refunds r WHERE r.charge=p.charge) ORDER BY p.created",
            (order_id,),
        ).fetchall()

    def has_access(self, order, now: float | None = None) -> bool:
        """A paid, unrefunded one-time order, or a subscription that has not expired."""
        now = time.time() if now is None else now
        payments = self.live_payments(order["id"])
        if not payments:
            return False
        if order["kind"] == "subscription":
            return max((p["expires"] or 0) for p in payments) > now
        return True

    def access_until(self, order) -> float | None:
        payments = self.live_payments(order["id"])
        expiries = [p["expires"] for p in payments if p["expires"]]
        return max(expiries) if expiries else None

    def paid_orders(self, user: int):
        return self.db.execute(
            "SELECT * FROM orders WHERE user=? AND paid=1 ORDER BY created DESC", (user,)
        ).fetchall()

    # refunds and issues --------------------------------------------------------
    def is_refunded(self, charge: str | None) -> bool:
        return bool(charge) and self.db.execute(
            "SELECT 1 FROM refunds WHERE charge=?", (charge,)).fetchone() is not None

    def mark_refunded(self, charge: str) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO refunds VALUES(?,?)", (charge, time.time()))

    def record_issue(self, charge, user, stars, payload, reason: str) -> None:
        """Keep a payment the operator must look at (refund or manual delivery)."""
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO payment_issues VALUES(?,?,?,?,?,?)",
                (charge or f"nocharge:{uuid.uuid4().hex}", user, stars, payload, reason, time.time()),
            )

    def open_issues(self):
        return self.db.execute(
            "SELECT i.* FROM payment_issues i "
            "WHERE NOT EXISTS (SELECT 1 FROM refunds r WHERE r.charge=i.charge) ORDER BY i.created"
        ).fetchall()

    # channel access ----------------------------------------------------------------
    def channel_access(self, order_id: str):
        return self.db.execute("SELECT * FROM channel_access WHERE order_id=?", (order_id,)).fetchone()

    def save_invite(self, order, invite: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO channel_access(order_id,user,channel,invite,created) VALUES(?,?,?,?,?) "
                "ON CONFLICT(order_id) DO UPDATE SET invite=excluded.invite, removed=NULL, attempts=0",
                (order["id"], order["user"], order["channel"], invite, time.time()))

    def active_channel_access(self):
        return self.db.execute("SELECT * FROM channel_access WHERE removed IS NULL").fetchall()

    def mark_removed(self, order_id: str) -> None:
        with self.db:
            self.db.execute("UPDATE channel_access SET removed=? WHERE order_id=?", (time.time(), order_id))

    def removal_failed(self, order_id: str) -> int:
        with self.db:
            self.db.execute("UPDATE channel_access SET attempts=attempts+1 WHERE order_id=?", (order_id,))
        return self.db.execute("SELECT attempts FROM channel_access WHERE order_id=?", (order_id,)).fetchone()[0]

    def reopen_channel(self, order_id: str) -> bool:
        """A renewal after access was taken back: send a fresh invite."""
        row = self.channel_access(order_id)
        if not row or row["removed"] is None:
            return False
        with self.db:
            self.db.execute("UPDATE orders SET delivered=0 WHERE id=?", (order_id,))
        return True

    def save_channel(self, chat: dict, status: str, can_invite: bool, can_restrict: bool) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO channels VALUES(?,?,?,?,?,?,?)",
                (chat["id"], chat.get("title") or "", chat.get("type") or "", status, int(can_invite),
                 int(can_restrict), time.time()))

    def channels(self):
        return self.db.execute("SELECT * FROM channels WHERE status='administrator' ORDER BY updated DESC").fetchall()

    # reports -------------------------------------------------------------------------
    def summary(self, since: float, now: float) -> dict:
        live = "NOT EXISTS (SELECT 1 FROM refunds r WHERE r.charge=p.charge)"
        sales = self.db.execute(f"SELECT COUNT(*), COALESCE(SUM(stars),0), COALESCE(SUM(recurring),0) "
                                f"FROM payments p WHERE {live} AND created>=? AND created<?", (since, now)).fetchone()
        buyers = self.db.execute(
            "SELECT COUNT(*) FROM (SELECT user, MIN(created) AS first FROM payments GROUP BY user) "
            "WHERE first>=? AND first<?", (since, now)).fetchone()[0]
        refunds = self.db.execute("SELECT COUNT(*) FROM refunds WHERE created>=? AND created<?",
                                  (since, now)).fetchone()[0]
        subs = self.db.execute(
            f"SELECT COUNT(DISTINCT p.order_id) FROM payments p JOIN orders o ON o.id=p.order_id "
            f"WHERE o.kind='subscription' AND {live} AND p.expires>?", (now,)).fetchone()[0]
        top = self.db.execute(
            f"SELECT o.title, COUNT(*) AS n FROM payments p JOIN orders o ON o.id=p.order_id "
            f"WHERE {live} AND p.created>=? AND p.created<? GROUP BY o.title ORDER BY n DESC LIMIT 1",
            (since, now)).fetchone()
        return {"sales": sales[0], "stars": sales[1], "renewals": sales[2], "new_buyers": buyers,
                "refunds": refunds, "active_subscriptions": subs, "top": top["title"] if top else None}

    # misc ------------------------------------------------------------------------
    def get_state(self, key: str, default: int = 0) -> int:
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_state(self, key: str, value: int) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, value))

    def close(self) -> None:
        self.db.close()
