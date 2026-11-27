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

import contextlib
import logging
import time
from typing import Awaitable, Callable

from .config import SUBSCRIPTION_PERIOD, Product, Settings
from .store import Store
from .telegram import TelegramError

log = logging.getLogger("stars_shop")
Api = Callable[..., Awaitable]

MAX_DELIVERY_ATTEMPTS = 5
DELIVERY_RETRY_SECONDS = 300
MAX_UPDATE_FAILURES = 3


# message field -> (send method, its file parameter)
FILE_METHODS = {"document": ("sendDocument", "document"), "video": ("sendVideo", "video"),
                "audio": ("sendAudio", "audio"), "photo": ("sendPhoto", "photo"),
                "animation": ("sendAnimation", "animation"), "voice": ("sendVoice", "voice")}


def _attached_file(message: dict) -> tuple[str, str, str | None] | None:
    for kind in ("document", "video", "audio", "animation", "voice"):
        item = message.get(kind)
        if item:
            return kind, item["file_id"], item.get("file_name")
    if message.get("photo"):
        return "photo", message["photo"][-1]["file_id"], None
    return None


def _who(user: dict) -> str:
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
    handle = f"@{user['username']}" if user.get("username") else ""
    return " ".join(x for x in (name, handle, f"(id {user.get('id')})") if x)


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
        if not self.settings.sales_enabled:
            return []
        files = self.store.product_files()
        return [p.public() | {"file": files.get(p.id, {}).get("kind")} for p in self.products.values()]

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
            if self.store.product_file(order["product"]):
                view["content"] += "\n\n📎 Dosyan bot sohbetine gönderildi. Tekrar almak için botta /orders yaz."
            access = self.store.channel_access(order["id"]) if order["channel"] else None
            if access and access["invite"] and access["removed"] is None:
                view["content"] += f"\n\n👉 Katılma linkin (tek kullanımlık): {access['invite']}"
            view["access_until"] = self.store.access_until(order)
        elif order["paid"]:
            view["status"] = "expired" if self.store.live_payments(order["id"]) else "refunded"
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
        attached = self.store.product_file(order["product"])
        if attached:
            # the file first: if it fails, nothing was sent and the whole delivery is retried
            method, field = FILE_METHODS[attached["kind"]]
            await self.api(method, chat_id=order["user"], protect_content=True, caption=order["title"][:1024],
                           **{field: attached["file_id"]})
        text = f"✅ {order['title']}\n\n{order['content']}"
        if attached:
            text += "\n\n📎 Dosyan yukarıda."
        if order["channel"]:
            invite = await self._invite(order)
            text += f"\n\n👉 Katılma linkin (sadece senin için, tek kullanımlık):\n{invite}"
        await self.api("sendMessage", chat_id=order["user"],
                       text=text if len(text) <= 4096 else order["content"], protect_content=True)
        with self.store.db:
            self.store.db.execute("UPDATE orders SET delivered=1 WHERE id=?", (order["id"],))

    async def _invite(self, order) -> str:
        """A one-person invite to the product's channel. The bot must be an admin there."""
        link = await self.api("createChatInviteLink", chat_id=order["channel"], member_limit=1,
                              expire_date=int(time.time()) + 7 * 86400, name=f"Sipariş {order['id'][:8]}")
        invite = link["invite_link"] if isinstance(link, dict) else str(link)
        self.store.save_invite(order, invite)
        return invite

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
            first = self.store.db.execute("SELECT attempts FROM delivery_state WHERE order_id=?",
                                          (order["id"],)).fetchone()["attempts"] == 1
            if order["channel"] and first and not self.store.channel_access(order["id"]):
                await self.notify_admins(
                    f"⚠️ '{order['title']}' için kanal davet linki oluşturulamadı (kanal {order['channel']}).\n"
                    "Botun kanalda yönetici olduğundan ve 'Kullanıcı davet et' + 'Üyeleri yasakla' yetkilerinin "
                    "açık olduğundan emin ol. Teslimat her 5 dakikada yeniden denenecek.")

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

    # ------------------------------------------------------------------ admins
    async def notify_admins(self, text: str) -> None:
        for admin in self.settings.admin_ids:
            try:
                await self.api("sendMessage", chat_id=admin, text=text, disable_web_page_preview=True)
            except Exception as exc:  # noqa: BLE001 - a notice must never break a payment
                log.warning("Yönetici bildirimi gönderilemedi: %s", type(exc).__name__)

    async def _membership(self, event: dict) -> None:
        """The bot was added to (or removed from) a channel or group: tell the admins its ID."""
        chat = event.get("chat") or {}
        if chat.get("type") not in ("channel", "supergroup", "group"):
            return
        member = event.get("new_chat_member") or {}
        status = member.get("status", "")
        can_invite = bool(member.get("can_invite_users"))
        can_restrict = bool(member.get("can_restrict_members")) or chat.get("type") == "channel"
        self.store.save_channel(chat, status, can_invite, can_restrict)
        title = chat.get("title") or chat["id"]
        if status == "administrator":
            missing = [name for ok, name in ((can_invite, "Kullanıcı davet et"),
                                             (bool(member.get("can_restrict_members")), "Üyeleri yasakla")) if not ok]
            note = ("\n⚠️ Eksik yetki: " + ", ".join(missing) + ". Bunlar olmadan link verilemez / süresi biten çıkarılamaz.") \
                if missing else "\nYetkiler tamam."
            await self.notify_admins(f"✅ Bot '{title}' için yönetici oldu.\nKanal ID: {chat['id']}\n"
                                     f"Bu kanala erişim satmak için ürünün 'Kanal ID' alanına bu sayıyı yaz.{note}")
        elif status in ("left", "kicked", "member", "restricted"):
            await self.notify_admins(f"⚠️ Bot '{title}' içinde artık yönetici değil ({chat['id']}). "
                                     "Bu kanala bağlı ürünlerde davet ve çıkarma çalışmaz.")

    async def sweep_channels(self, now: float | None = None) -> None:
        """Take channel access back from expired subscriptions and refunded orders."""
        now = time.time() if now is None else now
        for row in self.store.active_channel_access():
            order = self.store.order(row["order_id"])
            if order and self.store.has_access(order, now):
                continue
            try:
                with contextlib.suppress(TelegramError):  # an unused invite is simply revoked
                    if row["invite"]:
                        await self.api("revokeChatInviteLink", chat_id=row["channel"], invite_link=row["invite"])
                await self.api("banChatMember", chat_id=row["channel"], user_id=row["user"], until_date=int(now) + 60)
                await self.api("unbanChatMember", chat_id=row["channel"], user_id=row["user"], only_if_banned=True)
            except Exception as exc:  # noqa: BLE001
                attempts = self.store.removal_failed(row["order_id"])
                log.warning("Kanaldan çıkarma başarısız: order=%s hata=%s", row["order_id"], type(exc).__name__)
                if attempts >= 5:
                    self.store.mark_removed(row["order_id"])
                    await self.notify_admins(f"⚠️ {row['user']} kullanıcısı {row['channel']} kanalından çıkarılamadı "
                                             f"(erişimi bitti). Elle çıkar ya da botun yetkilerini kontrol et.")
                continue
            self.store.mark_removed(row["order_id"])
            with contextlib.suppress(Exception):
                title = order["title"] if order else "Üyelik"
                await self.api("sendMessage", chat_id=row["user"],
                               text=f"'{title}' erişimin sona erdi. Yeniden katılmak için mağazadan tekrar satın alabilirsin.",
                               reply_markup={"inline_keyboard": [[{"text": "🛍 Mağazayı aç",
                                                                   "web_app": {"url": self.settings.webapp_url + "/app"}}]]})

    async def daily_summary(self, now: float | None = None) -> bool:
        """Once a day after SUMMARY hour (server local time): yesterday's sales to the admins."""
        now = time.time() if now is None else now
        if self.settings.summary_hour < 0 or not self.settings.admin_ids:
            return False
        local = time.localtime(now)
        today = int(time.strftime("%Y%m%d", local))
        if local.tm_hour < self.settings.summary_hour or self.store.get_state("summary_day") >= today:
            return False
        self.store.set_state("summary_day", today)
        report = self.store.summary(now - 86400, now)
        if not (report["sales"] or report["refunds"] or report["active_subscriptions"]):
            return False  # nothing to say: no message
        lines = [f"📊 {self.settings.shop_name} · son 24 saat",
                 f"Satış: {report['sales']} · ⭐{report['stars']}",
                 f"Yeni müşteri: {report['new_buyers']} · Yenileme: {report['renewals']} · İade: {report['refunds']}",
                 f"Aktif abonelik: {report['active_subscriptions']}"]
        if report["top"]:
            lines.append(f"En çok satan: {report['top']}")
        await self.notify_admins("\n".join(lines))
        return True

    async def tick(self, now: float | None = None) -> None:
        """Background work, once a minute: deliveries, channel removals, daily summary."""
        for job in (self.redeliver, self.sweep_channels, self.daily_summary, self.remind_carts, self.notify_expired):
            try:
                await job(now)
            except Exception as exc:  # noqa: BLE001 - one job must not stop the others
                log.error("Arka plan işi %s hatası: %s", job.__name__, type(exc).__name__)

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
        if update.get("my_chat_member"):
            await self._membership(update["my_chat_member"])
            return
        message = update.get("message") or {}
        sender = message.get("from") or {}
        if message.get("chat", {}).get("type") != "private" or sender.get("is_bot"):
            return
        text = message.get("text") or message.get("caption") or ""
        start_source = text.partition(" ")[2].strip() if text.split("@")[0].startswith("/start") else None
        self.store.touch_contact(sender, start_source)
        if message.get("successful_payment"):
            await self._successful_payment(sender["id"], message["successful_payment"], sender)
        elif message.get("refunded_payment"):
            # Refunds made outside this bot (e.g. by Telegram support) revoke access too.
            refunded = message["refunded_payment"]
            first = not self.store.is_refunded(refunded["telegram_payment_charge_id"])
            self.store.mark_refunded(refunded["telegram_payment_charge_id"])
            if first and self.settings.notify_sales:
                await self.notify_admins(f"↩️ İade: ⭐{refunded.get('total_amount', '?')} · {_who(sender)}")
        elif sender["id"] in self.settings.admin_ids and text.split("@")[0].split(" ")[0].lower() == "/dosya":
            await self._attach(sender["id"], text, message)
        elif sender["id"] in self.settings.admin_ids and message.get("reply_to_message"):
            await self._admin_reply(sender["id"], message)
        elif text.startswith("/"):
            await self._command(sender["id"], text)
        elif sender["id"] not in self.settings.admin_ids and (text or message.get("message_id")):
            await self._customer_message(sender, message)
        elif text:
            await self._command(sender["id"], text)

    async def _attach(self, admin: int, text: str, message: dict) -> None:
        """/dosya <ürün> as the caption of a file attaches it; /dosya <ürün> sil removes it."""
        parts = text.split()
        product = parts[1].lower() if len(parts) > 1 else ""
        if product not in self.products:
            ids = ", ".join(self.products) or "henüz ürün yok"
            reply = ("Kullanım: dosyayı (PDF, video, zip…) bota gönderirken açıklamaya /dosya <ürün-kimliği> yaz.\n"
                     f"Ürünler: {ids}")
        elif len(parts) > 2 and parts[2].lower() in ("sil", "kaldir", "kaldır"):
            reply = "Dosya kaldırıldı." if self.store.remove_product_file(product) else "Bu üründe dosya yoktu."
        else:
            found = _attached_file(message)
            if not found:
                current = self.store.product_file(product)
                reply = (f"'{product}' ürününde dosya var: {current['name'] or current['kind']}. Değiştirmek için yeni dosyayı "
                         f"açıklamasına /dosya {product} yazarak gönder; kaldırmak için /dosya {product} sil."
                         if current else f"Dosyayı gönderirken açıklamasına /dosya {product} yaz.")
            else:
                kind, file_id, name = found
                self.store.set_product_file(product, file_id, kind, name)
                reply = (f"📎 {name or kind} '{self.products[product].title}' ürününe eklendi. "
                         "Satın alan herkese ödemeden hemen sonra gönderilecek (iletilemez/kaydedilemez olarak).")
        await self.api("sendMessage", chat_id=admin, text=reply)

    # ------------------------------------------------------------------ customer inbox
    async def _customer_message(self, sender: dict, message: dict) -> None:
        """A customer wrote to the shop bot: hand it to the admins; their reply goes back."""
        if not self.settings.admin_ids:
            await self._command(sender["id"], "/start")
            return
        header = f"💬 {_who(sender)}"
        for admin in self.settings.admin_ids:
            try:
                if message.get("text"):
                    sent = await self.api("sendMessage", chat_id=admin,
                                          text=f"{header}\n\n{message['text']}\n\n↩️ Cevaplamak için bu mesajı yanıtla.")
                else:
                    await self.api("sendMessage", chat_id=admin, text=f"{header} bir dosya gönderdi ↓ (yanıtla)")
                    sent = await self.api("forwardMessage", chat_id=admin, from_chat_id=sender["id"],
                                          message_id=message["message_id"])
                if isinstance(sent, dict) and sent.get("message_id"):
                    self.store.link_inbox(admin, sent["message_id"], sender["id"])
            except Exception as exc:  # noqa: BLE001
                log.warning("Müşteri mesajı yöneticiye iletilemedi: %s", type(exc).__name__)
        await self.api("sendMessage", chat_id=sender["id"],
                       text="Mesajın iletildi, en kısa sürede buradan cevap vereceğiz. 🙏",
                       reply_markup={"inline_keyboard": [[{"text": "🛍 Mağazayı aç",
                                                           "web_app": {"url": self.settings.webapp_url + "/app"}}]]})

    async def _admin_reply(self, admin: int, message: dict) -> None:
        user = self.store.inbox_user(admin, message["reply_to_message"].get("message_id", 0))
        if user is None:
            if message.get("text", "").startswith("/"):
                await self._command(admin, message["text"])
            else:
                await self.api("sendMessage", chat_id=admin,
                               text="Bu mesaj bir müşteri mesajına ait değil. Müşteriye cevap için onun mesajını yanıtla.")
            return
        try:
            await self.send_to_customer(user, message.get("text"), from_chat=admin, message_id=message["message_id"])
        except Exception as exc:  # noqa: BLE001
            await self.api("sendMessage", chat_id=admin, text=f"Gönderilemedi ({type(exc).__name__}). Müşteri botu engellemiş olabilir.")
            return
        await self.api("sendMessage", chat_id=admin, text="✅ Müşteriye iletildi.")

    async def send_to_customer(self, user: int, text: str | None, from_chat: int | None = None,
                               message_id: int | None = None) -> None:
        """Only people who wrote to the bot or opened the shop are reachable (Telegram's own rule too)."""
        if text:
            await self.api("sendMessage", chat_id=user, text=text[:4096])
        elif from_chat and message_id:
            await self.api("copyMessage", chat_id=user, from_chat_id=from_chat, message_id=message_id)
        else:
            raise ShopError("Boş mesaj gönderilemez.")

    async def notify_expired(self, now: float | None = None) -> int:
        """Tell buyers of a timed one-time product that their access ended and how to renew.
        (Channel products also get removed by sweep_channels, which sends its own message.)"""
        now = time.time() if now is None else now
        sent = 0
        for order in self.store.expired_timed_orders(now):
            self.store.mark_expiry_noticed(order["id"])
            if order["channel"]:
                continue
            buttons = []
            if order["product"] in self.products:
                buttons = [[{"text": "🔁 Yeniden al", "web_app": {"url": f"{self.settings.webapp_url}/app?p={order['product']}"}}]]
            try:
                extra = {"reply_markup": {"inline_keyboard": buttons}} if buttons else {}
                await self.api("sendMessage", chat_id=order["user"],
                               text=f"'{order['title']}' erişim süren ({order['access_days']} gün) doldu. "
                                    "Devam etmek istersen yeniden alabilirsin.", **extra)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                log.info("Süre bitti bildirimi gönderilemedi: %s", type(exc).__name__)
        return sent

    async def remind_carts(self, now: float | None = None) -> int:
        """One gentle reminder for an order left unpaid for an hour (never twice for the same product)."""
        if not self.settings.cart_reminder or not self.settings.sales_enabled:
            return 0
        now = time.time() if now is None else now
        sent = 0
        for order in self.store.abandoned_orders(now):
            self.store.mark_reminded(order["id"])
            if order["product"] not in self.products:
                continue
            try:
                await self.api(
                    "sendMessage", chat_id=order["user"],
                    text=f"'{order['title']}' siparişin yarım kaldı. Hâlâ istiyorsan tek dokunuşla tamamlayabilirsin.",
                    reply_markup={"inline_keyboard": [[{"text": f"⭐ {order['stars']} ile tamamla",
                                                        "web_app": {"url": f"{self.settings.webapp_url}/app?p={order['product']}"}}]]})
                sent += 1
            except Exception as exc:  # noqa: BLE001
                log.info("Sepet hatırlatması gönderilemedi: %s", type(exc).__name__)
        return sent

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

    async def _successful_payment(self, user: int, payment: dict, sender: dict | None = None) -> None:
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
        elif order["access_days"]:
            expires = int(time.time()) + int(order["access_days"]) * 86400
        renewal = bool(self.store.live_payments(order["id"]))
        self.store.add_payment(charge, order, expires, bool(payment.get("is_recurring")))
        if order["channel"]:
            self.store.reopen_channel(order["id"])
        await self.try_deliver(self.store.order(order["id"]))
        if self.settings.notify_sales:
            kind = "🔁 Abonelik yenilendi" if renewal or payment.get("is_recurring") and not payment.get("is_first_recurring") \
                else "💰 Yeni satış"
            until = f"\nBitiş: {time.strftime('%d.%m.%Y', time.localtime(expires))}" if expires else ""
            await self.notify_admins(f"{kind}: {order['title']} · ⭐{order['stars']}\nAlıcı: {_who(sender or {'id': user})}{until}")

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
        elif command in ("/musteriler", "/customers") and user in self.settings.admin_ids:
            funnel = self.store.funnel()
            recent = self.store.customers(limit=10)
            lines = ["👥 Müşteriler",
                     " · ".join(f"{k}: {v}" for k, v in funnel.items()), ""]
            for c in recent:
                who = c["name"] or "?"
                if c["username"]:
                    who += f" @{c['username']}"
                lines.append(f"• {who} — {c['stage']}" + (f" · ⭐{c['spent']}" if c["spent"] else "")
                             + (f" · kaynak: {c['source']}" if c["source"] else ""))
            reply = "\n".join(lines) if recent else "Henüz kimse botla konuşmadı."
        elif command in ("/admin", "/yonetim") and user in self.settings.admin_ids:
            rows = []
            if self.settings.admin_url:
                rows.append([{"text": "⚙️ Yönetim paneli", "url": self.settings.admin_url}])
            if self.settings.buy_url:
                rows.append([{"text": "🔑 Lisans yönetimi", "url": self.settings.buy_url}])
            rows.append([{"text": "🛍 Mağazayı aç", "web_app": {"url": self.settings.webapp_url + "/app"}}])
            await self.api("sendMessage", chat_id=user,
                           text="Yönetim\nMüşteriler: /musteriler · İade: /refund <charge_id> · Ödeme sorunları: /issues\n"
                                "Müşteri mesajları buraya gelir; cevaplamak için yanıtla.",
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
