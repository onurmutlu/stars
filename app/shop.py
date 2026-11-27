"""Stars shop logic shared by the Mini App API, the webhook and the polling runner.

Payment rules (https://core.telegram.org/bots/payments-stars):
  * an order is created first; its id is the invoice payload;
  * pre_checkout_query is accepted only for the same user, currency XTR, the
    exact Stars amount and an unpaid, unexpired order;
  * only message.successful_payment records a payment; the charge id is unique,
    so duplicate updates are idempotent;
  * a payment that cannot be matched is never dropped: it goes to payment_issues
    for the operator (refund or manual delivery).
"""
from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable

from .config import SUBSCRIPTION_PERIOD, Product, Settings
from .store import Store

log = logging.getLogger("stars_shop")
Api = Callable[..., Awaitable]

MAX_DELIVERY_ATTEMPTS = 5
DELIVERY_RETRY_SECONDS = 300
MAX_UPDATE_FAILURES = 3


class ShopError(Exception):
    """An error whose message is safe to show to the buyer."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class StarsShop:
    def __init__(self, api: Api, settings: Settings, products: dict[str, Product], store: Store):
        self.api, self.settings, self.products, self.store = api, settings, products, store

    # ------------------------------------------------------------------ Mini App
    def catalog(self) -> list[dict]:
        return [p.public() for p in self.products.values()] if self.settings.sales_enabled else []

    async def create_invoice(self, user: int, product_id: str) -> dict:
        if not self.settings.sales_enabled:
            raise ShopError("Satış şu an kapalı.", 403)
        product = self.products.get(product_id)
        if not product:
            raise ShopError("Ürün bulunamadı.", 404)
        if self._owns(user, product.id):
            raise ShopError("Bu ürün zaten sende. 'Satın aldıklarım' bölümünden açabilirsin.", 409)
        order_id = self.store.create_order(user, product)
        extra = {"subscription_period": SUBSCRIPTION_PERIOD} if product.kind == "subscription" else {}
        if product.photo_url:
            extra["photo_url"] = product.photo_url
        link = await self.api(
            "createInvoiceLink",
            title=product.title,
            description=product.description,
            payload=order_id,
            currency="XTR",
            prices=[{"label": product.title, "amount": product.stars}],
            **extra,
        )
        return {"order_id": order_id, "invoice_link": link}

    def order_status(self, user: int, order_id: str) -> dict:
        order = self.store.order(order_id)
        if not order or order["user"] != user:
            raise ShopError("Sipariş bulunamadı.", 404)
        return self._view(order)

    def purchases(self, user: int) -> list[dict]:
        return [self._view(o) for o in self.store.paid_orders(user)]

    def _view(self, order) -> dict:
        view = {"order_id": order["id"], "product": order["product"], "title": order["title"],
                "kind": order["kind"], "stars": order["stars"]}
        if self.store.has_access(order):
            view["status"] = "paid"
            view["content"] = order["content"]
            view["access_until"] = self.store.access_until(order)
        elif order["paid"]:
            view["status"] = "expired" if order["kind"] == "subscription" and self.store.live_payments(order["id"]) else "refunded"
        else:
            view["status"] = "pending"
        return view

    def _owns(self, user: int, product_id: str) -> bool:
        """An unrefunded one-time purchase or an active subscription for this product."""
        return any(o["product"] == product_id and self.store.has_access(o) for o in self.store.paid_orders(user))

    # ------------------------------------------------------------------ delivery
    async def deliver(self, order) -> None:
        if order["delivered"] or not self.store.has_access(order):
            return
        text = f"✅ {order['title']}\n\n{order['content']}"
        await self.api("sendMessage", chat_id=order["user"],
                       text=text if len(text) <= 4096 else order["content"], protect_content=True)
        with self.store.db:
            self.store.db.execute("UPDATE orders SET delivered=1 WHERE id=?", (order["id"],))

    async def try_deliver(self, order) -> None:
        """A buyer who blocked the bot must not stall the update queue.

        The payment is already stored and the content is visible in the Mini App,
        so a failed DM is retried later by redeliver() or by /orders."""
        try:
            await self.deliver(order)
        except Exception as exc:  # noqa: BLE001 - every failure is retried
            with self.store.db:
                self.store.db.execute(
                    "INSERT INTO delivery_state(order_id,attempts,last_attempt) VALUES(?,1,?) "
                    "ON CONFLICT(order_id) DO UPDATE SET attempts=attempts+1, last_attempt=excluded.last_attempt",
                    (order["id"], time.time()),
                )
            log.warning("Teslimat başarısız: order=%s hata=%s", order["id"], type(exc).__name__)

    async def redeliver(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        rows = self.store.db.execute(
            "SELECT o.* FROM orders o LEFT JOIN delivery_state d ON d.order_id=o.id "
            "WHERE o.paid=1 AND o.delivered=0 AND COALESCE(d.attempts,0)<? AND COALESCE(d.last_attempt,0)<=?",
            (MAX_DELIVERY_ATTEMPTS, now - DELIVERY_RETRY_SECONDS),
        ).fetchall()
        for order in rows:
            if not self.store.has_access(order):
                continue
            await self.try_deliver(order)
            state = self.store.db.execute(
                "SELECT attempts FROM delivery_state WHERE order_id=?", (order["id"],)).fetchone()
            if state and state["attempts"] >= MAX_DELIVERY_ATTEMPTS:
                payment = self.store.live_payments(order["id"])[-1]
                self.store.record_issue(payment["charge"], order["user"], order["stars"], order["id"],
                                        "delivery_failed")
                log.error("Teslimat %s denemede başarısız, operatör gerekli: order=%s",
                          MAX_DELIVERY_ATTEMPTS, order["id"])

    # ------------------------------------------------------------------ refunds
    async def refund(self, charge: str) -> bool:
        """Refund by charge id. Returns False if it was already refunded."""
        owner = self.store.payment(charge) or self.store.db.execute(
            "SELECT user FROM payment_issues WHERE charge=?", (charge,)).fetchone()
        if not owner:
            raise ShopError("Bu ödeme kimliği için kayıt yok.", 404)
        if self.store.is_refunded(charge):
            return False
        await self.api("refundStarPayment", user_id=owner["user"], telegram_payment_charge_id=charge)
        self.store.mark_refunded(charge)
        return True

    # ------------------------------------------------------------------ updates
    async def handle_update(self, update: dict) -> None:
        query = update.get("pre_checkout_query")
        if query:
            await self._pre_checkout(query)
            return
        message = update.get("message") or {}
        sender = message.get("from") or {}
        if message.get("chat", {}).get("type") != "private" or sender.get("is_bot"):
            return
        if message.get("successful_payment"):
            await self._successful_payment(sender["id"], message["successful_payment"])
        elif message.get("refunded_payment"):
            # Refunds made outside this bot (e.g. by Telegram support) revoke access too.
            self.store.mark_refunded(message["refunded_payment"]["telegram_payment_charge_id"])
        elif message.get("text"):
            await self._command(sender["id"], message["text"])

    async def _pre_checkout(self, query: dict) -> None:
        order = self.store.order(query.get("invoice_payload", ""))
        ok = bool(
            self.settings.sales_enabled
            and order
            and order["user"] == query["from"]["id"]
            and query.get("currency") == "XTR"
            and query.get("total_amount") == order["stars"]
            and not order["paid"]
            and time.time() - order["created"] <= self.settings.order_ttl
        )
        extra = {} if ok else {"error_message": "Sipariş geçersiz ya da süresi doldu. Mağazayı yeniden açıp tekrar dene."}
        await self.api("answerPreCheckoutQuery", pre_checkout_query_id=query["id"], ok=ok, **extra)

    async def _successful_payment(self, user: int, payment: dict) -> None:
        charge = payment["telegram_payment_charge_id"]
        if self.store.payment(charge):
            return  # duplicate update
        order = self.store.order(payment.get("invoice_payload", ""))
        issue = lambda reason: self.store.record_issue(  # noqa: E731
            charge, user, payment.get("total_amount"), payment.get("invoice_payload"), reason)
        if (not order or order["user"] != user or payment.get("currency") != "XTR"
                or payment.get("total_amount") != order["stars"]):
            issue("no_matching_order")
            log.error("Ödeme siparişle eşleşmiyor, operatör gerekli: charge=%s", charge)
            return
        expires = payment.get("subscription_expiration_date")
        if order["kind"] == "subscription":
            if not expires:
                issue("subscription_without_expiration")
                log.error("Abonelik ödemesinde bitiş tarihi yok: charge=%s", charge)
                return
        elif self.store.live_payments(order["id"]):
            issue("order_already_paid")  # a second charge for a one-time order: refund it
            log.error("Tek seferlik sipariş ikinci kez ödendi: order=%s charge=%s", order["id"], charge)
            return
        self.store.add_payment(charge, order, expires, bool(payment.get("is_recurring")))
        await self.try_deliver(self.store.order(order["id"]))

    async def _command(self, user: int, text: str) -> None:
        command, _, argument = text.strip().partition(" ")
        command = command.split("@")[0].lower()
        argument = argument.strip()
        if command == "/paysupport":
            reply = self.settings.support_text or "Destek bilgisi henüz tanımlanmadı."
        elif command == "/terms":
            reply = self.settings.terms_text or "Koşullar henüz tanımlanmadı."
        elif command == "/orders":
            orders = [o for o in self.store.paid_orders(user) if self.store.has_access(o)]
            for order in orders:
                with self.store.db:
                    self.store.db.execute("UPDATE orders SET delivered=0 WHERE id=?", (order["id"],))
                await self.try_deliver(self.store.order(order["id"]))
            reply = f"{len(orders)} aktif siparişin yeniden gönderildi." if orders else "Aktif siparişin yok."
        elif command == "/refund" and user in self.settings.admin_ids:
            if not argument:
                reply = "Kullanım: /refund <telegram_payment_charge_id>"
            else:
                try:
                    reply = "İade edildi." if await self.refund(argument) else "Bu ödeme zaten iade edilmiş."
                except ShopError as exc:
                    reply = str(exc)
        elif command == "/issues" and user in self.settings.admin_ids:
            rows = self.store.open_issues()
            reply = "\n".join(f"{r['reason']} · user={r['user']} · {r['stars']}⭐ · {r['charge']}" for r in rows[:30]) \
                or "Açık ödeme sorunu yok."
        elif command in ("/admin", "/yonetim") and user in self.settings.admin_ids:
            rows = []
            if self.settings.admin_url:
                rows.append([{"text": "⚙️ Yönetim paneli", "url": self.settings.admin_url}])
            if self.settings.buy_url:
                rows.append([{"text": "🔑 Lisans yönetimi", "url": self.settings.buy_url}])
            rows.append([{"text": "🛍 Mağazayı aç", "web_app": {"url": self.settings.webapp_url + "/app"}}])
            await self.api("sendMessage", chat_id=user,
                           text="Yönetim\nİade: /refund <charge_id> · Ödeme sorunları: /issues",
                           reply_markup={"inline_keyboard": rows})
            return
        elif command == "/start" and argument.startswith("p_") and argument[2:] in self.products:
            # deep link t.me/<bot>?start=p_<id>: open the Mini App on that product
            product = self.products[argument[2:]]
            await self.api(
                "sendMessage", chat_id=user,
                text=f"{product.title} · ⭐ {product.stars}\n{product.description}",
                reply_markup={"inline_keyboard": [[{"text": "🛍 Ürünü aç",
                                                    "web_app": {"url": f"{self.settings.webapp_url}/app?p={product.id}"}}]]},
            )
            return
        else:
            await self.api(
                "sendMessage", chat_id=user,
                text=f"{self.settings.shop_name}\nAlışveriş için mağazayı aç. Destek: /paysupport · Koşullar: /terms · Siparişlerim: /orders",
                reply_markup={"inline_keyboard": [[{"text": "🛍 Mağazayı aç", "web_app": {"url": self.settings.webapp_url + "/app"}}]]},
            )
            return
        await self.api("sendMessage", chat_id=user, text=reply)


async def process_update(shop: StarsShop, update: dict, failures: dict[int, int]) -> None:
    """Run one update so a single bad update can never stall the queue.

    While retries remain the exception is re-raised (the webhook answers 500 and
    Telegram redelivers; the poller sleeps and refetches). After MAX_UPDATE_FAILURES
    the update is skipped, and a payment inside it is kept for the operator."""
    update_id = update.get("update_id", -1)
    try:
        await shop.handle_update(update)
    except Exception as exc:
        failures[update_id] = failures.get(update_id, 0) + 1
        if failures[update_id] < MAX_UPDATE_FAILURES:
            raise
        failures.pop(update_id, None)
        message = update.get("message") or {}
        payment = message.get("successful_payment")
        if payment:
            shop.store.record_issue(payment.get("telegram_payment_charge_id"), message.get("from", {}).get("id"),
                                    payment.get("total_amount"), payment.get("invoice_payload"),
                                    f"update_failed:{type(exc).__name__}")
        log.error("Güncelleme %s atlandı (%s)", update_id, type(exc).__name__)
    else:
        failures.pop(update_id, None)
