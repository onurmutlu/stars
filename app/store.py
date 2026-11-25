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

    # orders -----------------------------------------------------------------
    def create_order(self, user: int, product) -> str:
        order_id = uuid.uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO orders(id,user,product,kind,title,stars,content,created) VALUES(?,?,?,?,?,?,?,?)",
                (order_id, user, product.id, product.kind, product.title, product.stars, product.content, time.time()),
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

    # misc ------------------------------------------------------------------------
    def get_state(self, key: str, default: int = 0) -> int:
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_state(self, key: str, value: int) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, value))

    def close(self) -> None:
        self.db.close()
