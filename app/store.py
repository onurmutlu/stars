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
-- everyone who talked to the bot or opened the shop: the CRM
CREATE TABLE IF NOT EXISTS contacts(
    user INTEGER PRIMARY KEY, name TEXT, username TEXT, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
    source TEXT, note TEXT);
-- admin's copy of a customer message -> that customer, so a reply to it goes back to them
CREATE TABLE IF NOT EXISTS inbox(
    admin INTEGER NOT NULL, admin_message INTEGER NOT NULL, user INTEGER NOT NULL, created REAL NOT NULL,
    PRIMARY KEY(admin, admin_message));
CREATE TABLE IF NOT EXISTS cart_reminders(order_id TEXT PRIMARY KEY, sent REAL NOT NULL);
CREATE TABLE IF NOT EXISTS expiry_notices(order_id TEXT PRIMARY KEY, sent REAL NOT NULL);
CREATE TABLE IF NOT EXISTS expiry_warnings(order_id TEXT PRIMARY KEY, sent REAL NOT NULL);
-- announcements to people who talked to the bot; one per day at most, opt-out with /durdur
CREATE TABLE IF NOT EXISTS broadcasts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
    confirmed REAL, finished REAL, total INTEGER NOT NULL DEFAULT 0, sent INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS broadcast_queue(
    broadcast INTEGER NOT NULL, user INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
    PRIMARY KEY(broadcast, user));
-- discount codes; uses count paid orders only
CREATE TABLE IF NOT EXISTS promo_codes(
    code TEXT PRIMARY KEY, percent INTEGER NOT NULL, product TEXT, max_uses INTEGER, uses INTEGER NOT NULL DEFAULT 0,
    expires REAL, active INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL);
-- a file the bot sends after payment (Telegram file_id of THIS bot; the seller sends it with /dosya <product>)
CREATE TABLE IF NOT EXISTS product_files(
    product TEXT PRIMARY KEY, file_id TEXT NOT NULL, kind TEXT NOT NULL, name TEXT, added REAL NOT NULL);
-- one reward per referred buyer: the referrer gets a single-use code after the friend's first paid order
CREATE TABLE IF NOT EXISTS referral_rewards(
    referred INTEGER PRIMARY KEY, referrer INTEGER NOT NULL, code TEXT NOT NULL, created REAL NOT NULL);
-- a gift: the giver paid order_id; whoever opens t.me/<bot>?start=gift_<code> first gets the product
CREATE TABLE IF NOT EXISTS gifts(
    code TEXT PRIMARY KEY, order_id TEXT NOT NULL UNIQUE, giver INTEGER NOT NULL, product TEXT NOT NULL,
    created REAL NOT NULL, claimed_by INTEGER, claimed_at REAL, claim_order TEXT);
-- one review per buyer and product; the admin can hide one
CREATE TABLE IF NOT EXISTS reviews(
    product TEXT NOT NULL, user INTEGER NOT NULL, rating INTEGER NOT NULL, text TEXT, name TEXT,
    created REAL NOT NULL, hidden INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(product, user));
-- people waiting for a sold-out product
CREATE TABLE IF NOT EXISTS waitlist(
    product TEXT NOT NULL, user INTEGER NOT NULL, created REAL NOT NULL, PRIMARY KEY(product, user));
-- a product picture sent to the bot (/foto <product>); served by the shop itself
CREATE TABLE IF NOT EXISTS product_photos(
    product TEXT PRIMARY KEY, file_id TEXT NOT NULL, unique_id TEXT NOT NULL, added REAL NOT NULL);
-- the one review request per order, sent a day after the purchase
CREATE TABLE IF NOT EXISTS review_requests(order_id TEXT PRIMARY KEY, sent REAL NOT NULL);
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
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(orders)")}
        for column, kind in (("channel", "INTEGER"), ("access_days", "INTEGER"), ("promo", "TEXT"),
                             ("gift", "INTEGER NOT NULL DEFAULT 0")):
            if column not in columns:  # databases from before these fields
                self.db.execute(f"ALTER TABLE orders ADD COLUMN {column} {kind}")
        contact_columns = {row[1] for row in self.db.execute("PRAGMA table_info(contacts)")}
        for column in ("opted_out", "blocked"):
            if column not in contact_columns:
                self.db.execute(f"ALTER TABLE contacts ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
        self.db.commit()

    # orders -----------------------------------------------------------------
    def create_order(self, user: int, product, stars: int | None = None, promo: str | None = None,
                     gift: bool = False) -> str:
        order_id = uuid.uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO orders(id,user,product,kind,title,stars,content,created,channel,access_days,promo,gift) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (order_id, user, product.id, product.kind, product.title, stars if stars is not None else product.stars,
                 product.content, time.time(), getattr(product, "channel_id", None), getattr(product, "access_days", None),
                 promo, int(gift)),
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

    def payment_exists_for(self, order_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM payments WHERE order_id=?", (order_id,)).fetchone() is not None

    def live_payments(self, order_id: str):
        return self.db.execute(
            "SELECT p.* FROM payments p WHERE p.order_id=? "
            "AND NOT EXISTS (SELECT 1 FROM refunds r WHERE r.charge=p.charge) ORDER BY p.created",
            (order_id,),
        ).fetchall()

    def has_access(self, order, now: float | None = None) -> bool:
        """A paid, unrefunded order whose time (subscription period or access days) has not run out."""
        now = time.time() if now is None else now
        payments = self.live_payments(order["id"])
        if not payments:
            return False
        if order["kind"] != "subscription" and any(p["expires"] is None for p in payments):
            return True  # one-time purchase without a time limit
        return max((p["expires"] or 0) for p in payments) > now

    def access_until(self, order) -> float | None:
        payments = self.live_payments(order["id"])
        expiries = [p["expires"] for p in payments if p["expires"]]
        return max(expiries) if expiries else None

    def active_until(self, user: int, product: str, now: float | None = None,
                     exclude: str | None = None) -> float | None:
        """Latest end of a still-running timed purchase of this product (to stack a renewal on it)."""
        now = time.time() if now is None else now
        ends = [self.access_until(o) for o in self.paid_orders(user)
                if o["product"] == product and o["id"] != exclude and self.has_access(o, now)]
        ends = [e for e in ends if e]
        return max(ends) if ends else None

    def has_other_access(self, user: int, *, product: str | None = None, channel: int | None = None,
                         exclude: str, now: float | None = None) -> bool:
        """Another order still gives this user the same product or the same channel."""
        now = time.time() if now is None else now
        return any(o["id"] != exclude and ((product and o["product"] == product) or (channel and o["channel"] == channel))
                   and self.has_access(o, now) for o in self.paid_orders(user))

    def expiring_timed_orders(self, now: float, window: float):
        rows = self.db.execute(
            "SELECT o.* FROM orders o WHERE o.paid=1 AND o.kind!='subscription' AND o.access_days IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM expiry_warnings w WHERE w.order_id=o.id)").fetchall()
        return [o for o in rows if self.has_access(o, now) and (self.access_until(o) or 0) - now < window]

    def mark_expiry_warned(self, order_id: str) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO expiry_warnings VALUES(?,?)", (order_id, time.time()))

    def sold_count(self, product: str) -> int:
        """Paid, unrefunded orders of a product (bundle grants included): what stock counts."""
        return self.db.execute(
            "SELECT COUNT(DISTINCT o.id) FROM orders o JOIN payments p ON p.order_id=o.id WHERE o.product=? "
            "AND p.charge NOT LIKE '%:gift' "  # a claimed gift is the same unit the giver bought
            "AND NOT EXISTS (SELECT 1 FROM refunds r WHERE r.charge=p.charge)", (product,)).fetchone()[0]

    def paid_orders(self, user: int):
        """What the user bought for themselves (gifts they bought for others are in gift_orders)."""
        return self.db.execute(
            "SELECT * FROM orders WHERE user=? AND paid=1 AND gift=0 ORDER BY created DESC", (user,)
        ).fetchall()

    def gift_orders(self, user: int):
        return self.db.execute(
            "SELECT o.*, g.code, g.claimed_by, g.claimed_at FROM orders o JOIN gifts g ON g.order_id=o.id "
            "WHERE o.user=? ORDER BY o.created DESC", (user,)).fetchall()

    # gifts ---------------------------------------------------------------------
    def create_gift(self, code: str, order) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO gifts(code,order_id,giver,product,created) VALUES(?,?,?,?,?)",
                            (code, order["id"], order["user"], order["product"], time.time()))

    def gift(self, code: str):
        return self.db.execute("SELECT * FROM gifts WHERE code=?", (code.strip().upper(),)).fetchone()

    def gift_for_order(self, order_id: str):
        return self.db.execute("SELECT * FROM gifts WHERE order_id=?", (order_id,)).fetchone()

    def claim_gift(self, code: str, user: int, claim_order: str) -> bool:
        with self.db:
            return self.db.execute(
                "UPDATE gifts SET claimed_by=?, claimed_at=?, claim_order=? WHERE code=? AND claimed_by IS NULL",
                (user, time.time(), claim_order, code)).rowcount > 0

    # reviews -------------------------------------------------------------------
    def save_review(self, product: str, user: int, rating: int, text: str | None, name: str | None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO reviews(product,user,rating,text,name,created) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(product,user) DO UPDATE SET rating=excluded.rating, text=excluded.text, "
                "name=excluded.name, created=excluded.created",
                (product, user, rating, (text or "").strip()[:500] or None, (name or "")[:40] or None, time.time()))

    def review(self, product: str, user: int):
        return self.db.execute("SELECT * FROM reviews WHERE product=? AND user=?", (product, user)).fetchone()

    def reviews(self, product: str | None = None, limit: int = 20, include_hidden: bool = False) -> list[dict]:
        where, args = [], []
        if product:
            where.append("product=?")
            args.append(product)
        if not include_hidden:
            where.append("hidden=0")
        sql = "SELECT * FROM reviews" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created DESC LIMIT ?"
        return [dict(r) for r in self.db.execute(sql, (*args, limit))]

    def ratings(self) -> dict[str, tuple[float, int]]:
        """product -> (average rating, number of visible reviews)."""
        return {r["product"]: (round(r["avg"], 1), r["n"]) for r in self.db.execute(
            "SELECT product, AVG(rating) AS avg, COUNT(*) AS n FROM reviews WHERE hidden=0 GROUP BY product")}

    def hide_review(self, product: str, user: int, hidden: bool = True) -> bool:
        with self.db:
            return self.db.execute("UPDATE reviews SET hidden=? WHERE product=? AND user=?",
                                   (int(hidden), product, user)).rowcount > 0

    def review_candidates(self, older_than: float, newer_than: float):
        """Paid own orders (not gifts, not bundle parts) between the two moments with no request sent yet."""
        return self.db.execute(
            "SELECT o.* FROM orders o JOIN payments p ON p.order_id=o.id "
            "WHERE o.gift=0 AND p.stars>0 AND p.created<? AND p.created>? "
            "AND NOT EXISTS (SELECT 1 FROM review_requests q WHERE q.order_id=o.id) "
            "AND NOT EXISTS (SELECT 1 FROM refunds r WHERE r.charge=p.charge) "
            "GROUP BY o.id", (older_than, newer_than)).fetchall()

    def mark_review_requested(self, order_id: str) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO review_requests VALUES(?,?)", (order_id, time.time()))

    # waitlist ------------------------------------------------------------------
    def join_waitlist(self, product: str, user: int) -> bool:
        with self.db:
            return self.db.execute("INSERT OR IGNORE INTO waitlist VALUES(?,?,?)",
                                   (product, user, time.time())).rowcount > 0

    def waiting(self, product: str) -> list[int]:
        return [r[0] for r in self.db.execute("SELECT user FROM waitlist WHERE product=? ORDER BY created", (product,))]

    def waitlist_counts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.db.execute("SELECT product, COUNT(*) FROM waitlist GROUP BY product")}

    def leave_waitlist(self, product: str, users: list[int]) -> None:
        with self.db:
            self.db.executemany("DELETE FROM waitlist WHERE product=? AND user=?", [(product, u) for u in users])

    # photos --------------------------------------------------------------------
    def set_product_photo(self, product: str, file_id: str, unique_id: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO product_photos VALUES(?,?,?,?)", (product, file_id, unique_id, time.time()))

    def product_photo(self, product: str):
        return self.db.execute("SELECT * FROM product_photos WHERE product=?", (product,)).fetchone()

    def product_photos(self) -> dict[str, str]:
        return {r["product"]: r["unique_id"] for r in self.db.execute("SELECT product, unique_id FROM product_photos")}

    def remove_product_photo(self, product: str) -> bool:
        with self.db:
            return self.db.execute("DELETE FROM product_photos WHERE product=?", (product,)).rowcount > 0

    # sales over time -------------------------------------------------------------
    def daily_sales(self, days: int = 30, now: float | None = None) -> dict:
        """Stars and sales per local day for the last N days, and the best products in that window."""
        now = time.time() if now is None else now
        start_day = time.localtime(now - (days - 1) * 86400)
        start = time.mktime((start_day.tm_year, start_day.tm_mon, start_day.tm_mday, 0, 0, 0, 0, 0, -1))
        live = "NOT EXISTS (SELECT 1 FROM refunds r WHERE r.charge=p.charge)"
        rows = self.db.execute(
            f"SELECT p.created, p.stars, o.product, o.title FROM payments p JOIN orders o ON o.id=p.order_id "
            f"WHERE p.stars>0 AND p.created>=? AND {live}", (start,)).fetchall()
        labels = [time.strftime("%Y-%m-%d", time.localtime(start + i * 86400 + 3600)) for i in range(days)]
        stars, sales, top = dict.fromkeys(labels, 0), dict.fromkeys(labels, 0), {}
        for r in rows:
            day = time.strftime("%Y-%m-%d", time.localtime(r["created"]))
            if day in stars:
                stars[day] += r["stars"]
                sales[day] += 1
            entry = top.setdefault(r["product"], {"product": r["product"], "title": r["title"], "sales": 0, "stars": 0})
            entry["sales"] += 1
            entry["stars"] += r["stars"]
        return {"days": labels, "stars": [stars[d] for d in labels], "sales": [sales[d] for d in labels],
                "top": sorted(top.values(), key=lambda e: (-e["stars"], -e["sales"]))[:5],
                "total_stars": sum(stars.values()), "total_sales": sum(sales.values())}

    # sources -------------------------------------------------------------------
    def source_stats(self) -> list[dict]:
        """Per first-touch source: people, buyers, Stars (refunds excluded). '' is direct."""
        live = "NOT EXISTS (SELECT 1 FROM refunds r WHERE r.charge=p.charge)"
        rows = self.db.execute(
            "SELECT COALESCE(CASE WHEN substr(c.source, 1, 4)='ref_' THEN 'davet' "
            "WHEN substr(c.source, 1, 4)='kod_' THEN 'kampanya:' || substr(c.source, 5) "
            "WHEN substr(c.source, 1, 2)='p_' THEN 'ürün linki' "
            "WHEN substr(c.source, 1, 5)='gift_' THEN 'hediye' ELSE c.source END, '') AS src, "
            "COUNT(*) AS people, "
            f"SUM(EXISTS(SELECT 1 FROM payments p WHERE p.user=c.user AND p.stars>0 AND {live})) AS buyers, "
            f"SUM((SELECT COALESCE(SUM(p.stars),0) FROM payments p WHERE p.user=c.user AND {live})) AS stars "
            "FROM contacts c GROUP BY src ORDER BY stars DESC, people DESC").fetchall()
        return [{"source": r["src"] or "doğrudan", "people": r["people"], "buyers": r["buyers"] or 0,
                 "stars": r["stars"] or 0,
                 "conversion": round(100 * (r["buyers"] or 0) / r["people"], 1) if r["people"] else 0.0}
                for r in rows]

    # refunds and issues --------------------------------------------------------
    def is_refunded(self, charge: str | None) -> bool:
        return bool(charge) and self.db.execute(
            "SELECT 1 FROM refunds WHERE charge=?", (charge,)).fetchone() is not None

    def mark_refunded(self, charge: str) -> None:
        """Refunding a bundle also takes back what came with it (charges '<charge>:<product>')."""
        with self.db:
            now = time.time()
            self.db.execute("INSERT OR IGNORE INTO refunds VALUES(?,?)", (charge, now))
            self.db.execute("INSERT OR IGNORE INTO refunds SELECT charge, ? FROM payments WHERE charge LIKE ? ESCAPE '\\'",
                            (now, charge.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ":%"))

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

    # broadcasts ----------------------------------------------------------------------
    def audience(self, exclude: frozenset[int] = frozenset()) -> list[int]:
        rows = self.db.execute("SELECT user FROM contacts WHERE opted_out=0 AND blocked=0").fetchall()
        return [r["user"] for r in rows if r["user"] not in exclude]

    def set_opt_out(self, user: int, opted_out: bool) -> None:
        with self.db:
            self.db.execute("UPDATE contacts SET opted_out=? WHERE user=?", (int(opted_out), user))

    def mark_blocked(self, user: int) -> None:
        with self.db:
            self.db.execute("UPDATE contacts SET blocked=1 WHERE user=?", (user,))

    def last_broadcast(self):
        return self.db.execute("SELECT * FROM broadcasts WHERE confirmed IS NOT NULL ORDER BY confirmed DESC LIMIT 1").fetchone()

    def pending_broadcast(self):
        return self.db.execute("SELECT * FROM broadcasts WHERE status='pending' ORDER BY id DESC LIMIT 1").fetchone()

    def create_broadcast(self, text: str) -> int:
        with self.db:
            self.db.execute("UPDATE broadcasts SET status='cancelled' WHERE status='pending'")
            return self.db.execute("INSERT INTO broadcasts(text,status,created) VALUES(?,?,?)",
                                   (text, "pending", time.time())).lastrowid

    def cancel_broadcast(self) -> bool:
        with self.db:
            return self.db.execute("UPDATE broadcasts SET status='cancelled' WHERE status='pending'").rowcount > 0

    def confirm_broadcast(self, broadcast_id: int, audience: list[int]) -> int:
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO broadcast_queue(broadcast,user) VALUES(?,?)",
                                [(broadcast_id, u) for u in audience])
            self.db.execute("UPDATE broadcasts SET status='sending', confirmed=?, total=? WHERE id=?",
                            (time.time(), len(audience), broadcast_id))
        return len(audience)

    def sending_broadcast(self):
        return self.db.execute("SELECT * FROM broadcasts WHERE status='sending' ORDER BY id LIMIT 1").fetchone()

    def broadcast_batch(self, broadcast_id: int, limit: int) -> list[int]:
        return [r["user"] for r in self.db.execute(
            "SELECT user FROM broadcast_queue WHERE broadcast=? AND state='queued' LIMIT ?", (broadcast_id, limit))]

    def broadcast_result(self, broadcast_id: int, user: int, ok: bool) -> None:
        with self.db:
            self.db.execute("UPDATE broadcast_queue SET state=? WHERE broadcast=? AND user=?",
                            ("sent" if ok else "failed", broadcast_id, user))
            self.db.execute(f"UPDATE broadcasts SET {'sent' if ok else 'failed'}={'sent' if ok else 'failed'}+1 WHERE id=?",
                            (broadcast_id,))

    def finish_broadcast(self, broadcast_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE broadcasts SET status='done', finished=? WHERE id=?", (time.time(), broadcast_id))

    def broadcasts(self, limit: int = 10) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM broadcasts WHERE status!='cancelled' "
                                                 "ORDER BY id DESC LIMIT ?", (limit,))]

    # promo codes ---------------------------------------------------------------------
    def save_promo(self, code: str, percent: int, product: str | None = None, max_uses: int | None = None,
                   expires: float | None = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO promo_codes(code,percent,product,max_uses,expires,created) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET percent=excluded.percent, product=excluded.product, "
                "max_uses=excluded.max_uses, expires=excluded.expires, active=1",
                (code.upper(), percent, product, max_uses, expires, time.time()))

    def promo(self, code: str):
        return self.db.execute("SELECT * FROM promo_codes WHERE code=?", (code.strip().upper(),)).fetchone()

    def promos(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM promo_codes ORDER BY active DESC, created DESC")]

    def deactivate_promo(self, code: str) -> bool:
        with self.db:
            return self.db.execute("UPDATE promo_codes SET active=0 WHERE code=? AND active=1",
                                   (code.strip().upper(),)).rowcount > 0

    def count_promo_use(self, code: str) -> None:
        with self.db:
            self.db.execute("UPDATE promo_codes SET uses=uses+1 WHERE code=?", (code,))

    # product files -------------------------------------------------------------------
    def set_product_file(self, product: str, file_id: str, kind: str, name: str | None) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO product_files VALUES(?,?,?,?,?)",
                            (product, file_id, kind, (name or None) and name[:200], time.time()))

    def product_file(self, product: str):
        return self.db.execute("SELECT * FROM product_files WHERE product=?", (product,)).fetchone()

    def remove_product_file(self, product: str) -> bool:
        with self.db:
            return self.db.execute("DELETE FROM product_files WHERE product=?", (product,)).rowcount > 0

    def product_files(self) -> dict[str, dict]:
        return {r["product"]: {"kind": r["kind"], "name": r["name"]}
                for r in self.db.execute("SELECT * FROM product_files")}

    # contacts (CRM) ------------------------------------------------------------------
    def touch_contact(self, user: dict, source: str | None = None, now: float | None = None) -> None:
        """Record or refresh a person. The first source (deep link, campaign) is kept."""
        if not user or not user.get("id"):
            return
        now = time.time() if now is None else now
        name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)[:128] or None
        username = (user.get("username") or None)
        with self.db:
            self.db.execute(
                "INSERT INTO contacts(user,name,username,first_seen,last_seen,source) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(user) DO UPDATE SET last_seen=excluded.last_seen, "
                "name=COALESCE(excluded.name, contacts.name), username=COALESCE(excluded.username, contacts.username), "
                "source=COALESCE(contacts.source, excluded.source)",
                (int(user["id"]), name, username, now, now, (source or None) and source[:64]))

    def contact(self, user: int):
        return self.db.execute("SELECT * FROM contacts WHERE user=?", (user,)).fetchone()

    def paid_count(self, user: int) -> int:
        """Payments of real Stars by this user (bundle children with 0 Stars do not count)."""
        return self.db.execute("SELECT COUNT(*) FROM payments WHERE user=? AND stars>0", (user,)).fetchone()[0]

    def add_referral_reward(self, referred: int, referrer: int, code: str) -> bool:
        with self.db:
            return self.db.execute("INSERT OR IGNORE INTO referral_rewards VALUES(?,?,?,?)",
                                   (referred, referrer, code, time.time())).rowcount > 0

    def referral_stats(self, referrer: int) -> dict:
        invited = self.db.execute("SELECT COUNT(*) FROM contacts WHERE source=?", (f"ref_{referrer}",)).fetchone()[0]
        rewarded = self.db.execute("SELECT COUNT(*) FROM referral_rewards WHERE referrer=?", (referrer,)).fetchone()[0]
        return {"invited": invited, "rewarded": rewarded}

    def set_note(self, user: int, note: str) -> bool:
        with self.db:
            cur = self.db.execute("UPDATE contacts SET note=? WHERE user=?", (note[:500] or None, user))
        return cur.rowcount > 0

    def customers(self, limit: int = 200, now: float | None = None) -> list[dict]:
        """Contacts with their stage: ziyaretçi -> sepette -> müşteri (or abone / iade)."""
        now = time.time() if now is None else now
        live = "NOT EXISTS (SELECT 1 FROM refunds r WHERE r.charge=p.charge)"
        rows = self.db.execute(
            f"SELECT c.*, "
            f"(SELECT COALESCE(SUM(p.stars),0) FROM payments p WHERE p.user=c.user AND {live}) AS spent, "
            f"(SELECT COUNT(*) FROM payments p WHERE p.user=c.user AND {live}) AS payments, "
            f"(SELECT COUNT(*) FROM payments p WHERE p.user=c.user AND NOT {live}) AS refunded, "
            f"(SELECT COUNT(*) FROM payments p JOIN orders o ON o.id=p.order_id WHERE p.user=c.user AND {live} "
            f" AND o.kind='subscription' AND p.expires>?) AS active_subs, "
            f"(SELECT COUNT(*) FROM orders o WHERE o.user=c.user AND o.paid=0) AS open_orders "
            f"FROM contacts c ORDER BY c.last_seen DESC LIMIT ?", (now, limit)).fetchall()
        result = []
        for r in rows:
            if r["active_subs"]:
                stage = "abone"
            elif r["payments"]:
                stage = "müşteri"
            elif r["refunded"]:
                stage = "iade"
            elif r["open_orders"]:
                stage = "sepette"
            else:
                stage = "ziyaretçi"
            result.append({"user": r["user"], "name": r["name"], "username": r["username"], "stage": stage,
                           "spent": r["spent"], "payments": r["payments"], "source": r["source"],
                           "note": r["note"], "first_seen": r["first_seen"], "last_seen": r["last_seen"]})
        return result

    def sales_rows(self, limit: int = 20000) -> list[dict]:
        """Every payment with its order and buyer, newest first (for the CSV report)."""
        rows = self.db.execute(
            "SELECT p.created, p.charge, p.stars, p.expires, p.recurring, o.id AS order_id, o.product, o.title, "
            "o.kind, o.promo, o.user, c.name, c.username, c.source, "
            "EXISTS(SELECT 1 FROM refunds r WHERE r.charge=p.charge) AS refunded "
            "FROM payments p JOIN orders o ON o.id=p.order_id LEFT JOIN contacts c ON c.user=o.user "
            "ORDER BY p.created DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def funnel(self, now: float | None = None) -> dict[str, int]:
        counts = {"ziyaretçi": 0, "sepette": 0, "müşteri": 0, "abone": 0, "iade": 0}
        for c in self.customers(limit=100000, now=now):
            counts[c["stage"]] += 1
        return counts

    def link_inbox(self, admin: int, admin_message: int, user: int) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO inbox VALUES(?,?,?,?)", (admin, admin_message, user, time.time()))

    def inbox_user(self, admin: int, admin_message: int) -> int | None:
        row = self.db.execute("SELECT user FROM inbox WHERE admin=? AND admin_message=?",
                              (admin, admin_message)).fetchone()
        return row["user"] if row else None

    def abandoned_orders(self, now: float, min_age: float = 3600, max_age: float = 86400):
        """Unpaid orders between one hour and a day old, not reminded, and not bought since."""
        return self.db.execute(
            "SELECT o.* FROM orders o WHERE o.paid=0 AND o.created<=? AND o.created>=? "
            "AND NOT EXISTS (SELECT 1 FROM cart_reminders c WHERE c.order_id=o.id) "
            "AND NOT EXISTS (SELECT 1 FROM orders x WHERE x.user=o.user AND x.product=o.product AND x.paid=1) "
            "AND NOT EXISTS (SELECT 1 FROM cart_reminders c JOIN orders y ON y.id=c.order_id "
            "                WHERE y.user=o.user AND y.product=o.product)",
            (now - min_age, now - max_age)).fetchall()

    def expired_timed_orders(self, now: float):
        """One-time orders with access days whose access ended and the buyer was not told yet."""
        rows = self.db.execute(
            "SELECT o.* FROM orders o WHERE o.paid=1 AND o.kind!='subscription' AND o.access_days IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM expiry_notices e WHERE e.order_id=o.id)").fetchall()
        return [o for o in rows if self.live_payments(o["id"]) and not self.has_access(o, now)]

    def mark_expiry_noticed(self, order_id: str) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO expiry_notices VALUES(?,?)", (order_id, time.time()))

    def mark_reminded(self, order_id: str) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO cart_reminders VALUES(?,?)", (order_id, time.time()))

    # reports -------------------------------------------------------------------------
    def summary(self, since: float, now: float) -> dict:
        live = "NOT EXISTS (SELECT 1 FROM refunds r WHERE r.charge=p.charge)"
        sales = self.db.execute(f"SELECT COUNT(*), COALESCE(SUM(stars),0), COALESCE(SUM(recurring),0) "
                                f"FROM payments p WHERE {live} AND p.stars>0 AND created>=? AND created<?",
                                (since, now)).fetchone()
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
            f"WHERE {live} AND p.stars>0 AND p.created>=? AND p.created<? GROUP BY o.title ORDER BY n DESC LIMIT 1",
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
