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

import asyncio
import contextlib
import csv
import io
import logging
import re
import secrets
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Awaitable, Callable

from .config import SUBSCRIPTION_PERIOD, Product, Settings
from .store import Store
from .telegram import TelegramError

log = logging.getLogger("stars_shop")
Api = Callable[..., Awaitable]

MAX_DELIVERY_ATTEMPTS = 5
DELIVERY_RETRY_SECONDS = 300
MAX_UPDATE_FAILURES = 3
RENEW_WINDOW = 3 * 86400       # timed access can be extended (and is reminded) in its last three days
BROADCAST_GAP = 86400          # one announcement a day at most: keeps the bot out of spam reports
BROADCAST_BATCH = 300          # messages per background tick (a tick is ~1 minute)
BROADCAST_FOOTER = "\n\n— Duyuru almak istemiyorsan /durdur yaz."


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


def _html(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _quote(text: str) -> str:
    from urllib.parse import quote
    return quote(text, safe="")


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
    def availability(self, product, now: float | None = None) -> tuple[str | None, int | None]:
        """(why it cannot be bought now, or None; how many are left when stock is limited)."""
        now = time.time() if now is None else now
        left = max(0, product.stock - self.store.sold_count(product.id)) if product.stock else None
        if product.sale_until and now > product.sale_until:
            return "Bu ürünün satışı sona erdi.", left
        if left == 0:
            return "Bu ürün tükendi.", 0
        return None, left

    def catalog(self) -> list[dict]:
        if not self.settings.sales_enabled:
            return []
        files = self.store.product_files()
        photos = self.store.product_photos()
        ratings = self.store.ratings()
        now = time.time()
        items = []
        for p in self.products.values():
            if p.sale_until and now > p.sale_until:
                continue  # closed sales disappear from the shop; buyers keep their purchases
            reason, left = self.availability(p, now)
            rating, reviews = ratings.get(p.id, (None, 0))
            item = p.public() | {"file": files.get(p.id, {}).get("kind"), "left": left, "sold_out": left == 0,
                                 "rating": rating, "reviews": reviews}
            if p.id in photos:  # a photo sent to the bot wins over a photo URL
                item["photo_url"] = f"api/photo/{p.id}?v={photos[p.id][:12]}"
            items.append(item)
        return items

    def quote(self, product_id: str, code: str | None) -> dict:
        """Price of a product with an optional discount code; raises ShopError for a code that does not apply."""
        product = self.products.get(product_id)
        if not product:
            raise ShopError("Ürün bulunamadı.", 404)
        if not code or not code.strip():
            return {"product": product.id, "stars": product.stars, "original": product.stars, "percent": 0, "code": None}
        promo = self.store.promo(code)
        now = time.time()
        if not promo or not promo["active"] or (promo["expires"] and promo["expires"] < now):
            raise ShopError("Bu kod geçerli değil ya da süresi dolmuş.")
        if promo["max_uses"] is not None and promo["uses"] >= promo["max_uses"]:
            raise ShopError("Bu kodun kullanım hakkı bitti.")
        if promo["product"] and promo["product"] != product.id:
            raise ShopError("Bu kod bu ürün için geçerli değil.")
        if product.kind == "subscription":
            raise ShopError("İndirim kodu aboneliklerde kullanılamıyor.")
        if product.flexible:
            raise ShopError("Bu üründe tutarı sen seçiyorsun; indirim kodu geçmez.")
        stars = max(1, -(-product.stars * (100 - promo["percent"]) // 100))  # round up, never below 1 Star
        return {"product": product.id, "stars": stars, "original": product.stars, "percent": promo["percent"],
                "code": promo["code"]}

    async def create_invoice(self, user: int, product_id: str, code: str | None = None, gift: bool = False,
                             answer: str | None = None, amount: int | None = None) -> dict:
        if not self.settings.sales_enabled:
            raise ShopError("Satış şu an kapalı.", 403)
        product = self.products.get(product_id)
        if not product:
            raise ShopError("Ürün bulunamadı.", 404)
        if gift and product.kind == "subscription":
            raise ShopError("Abonelik hediye edilemez; tek seferlik ürünleri hediye edebilirsin.")
        if not gift and self._owns(user, product.id):
            raise ShopError("Bu ürün zaten sende. 'Satın aldıklarım' bölümünden açabilirsin.", 409)
        reason, _ = self.availability(product)
        if reason:
            raise ShopError(reason, 409)
        answer = (answer or "").strip()[:300] or None
        if product.ask and not answer:
            raise ShopError(f"Önce şu soruyu cevapla: {product.ask}")
        price = self.quote(product.id, code)
        if product.flexible and amount is not None:
            if not product.stars <= amount <= 100000:
                raise ShopError(f"Tutar en az ⭐{product.stars} olmalı.")
            price = {**price, "stars": int(amount)}
        order_id = self.store.create_order(user, product, stars=price["stars"], promo=price["code"], gift=gift,
                                           answer=answer if product.ask else None)
        extra = {"subscription_period": SUBSCRIPTION_PERIOD} if product.kind == "subscription" else {}
        if self.store.product_photo(product.id):
            extra["photo_url"] = f"{self.settings.webapp_url}/api/photo/{product.id}"
        elif product.photo_url:
            extra["photo_url"] = product.photo_url
        link = await self.api(
            "createInvoiceLink",
            title=("🎁 " + product.title)[:32] if gift else product.title,
            description=("Hediye: ödemeden sonra arkadaşına göndereceğin bir link alırsın. " + product.description)[:255]
            if gift else product.description,
            payload=order_id,
            currency="XTR",
            prices=[{"label": product.title + (f" ({price['code']} -%{price['percent']})" if price["code"] else ""),
                     "amount": price["stars"]}],
            **extra,
        )
        return {"order_id": order_id, "invoice_link": link}

    def order_status(self, user: int, order_id: str) -> dict:
        order = self.store.order(order_id)
        if not order or order["user"] != user:
            raise ShopError("Sipariş bulunamadı.", 404)
        return self._view(order)

    def purchases(self, user: int) -> list[dict]:
        own = [self._view(o) for o in self.store.paid_orders(user)]
        gifts = []
        for g in self.store.gift_orders(user):
            if not g["paid"]:
                continue
            refunded = not self.store.live_payments(g["id"])
            gifts.append({"order_id": g["id"], "product": g["product"], "title": g["title"], "kind": g["kind"],
                          "stars": g["stars"], "status": "gift",
                          "gift": {"link": None if refunded else self.gift_link(g["code"]),
                                   "claimed": g["claimed_by"] is not None, "refunded": refunded}})
        return own + gifts

    def gift_link(self, code: str) -> str | None:
        return f"https://t.me/{self.settings.bot_username}?start=gift_{code}" if self.settings.bot_username else None

    def _view(self, order) -> dict:
        view = {"order_id": order["id"], "product": order["product"], "title": order["title"],
                "kind": order["kind"], "stars": order["stars"]}
        if order["gift"]:
            gift = self.store.gift_for_order(order["id"])
            view["status"] = "gift" if gift and self.store.live_payments(order["id"]) else \
                ("refunded" if order["paid"] else "pending")
            if gift:
                view["gift"] = {"link": self.gift_link(gift["code"]), "claimed": gift["claimed_by"] is not None}
            return view
        if self.store.has_access(order):
            view["status"] = "paid"
            view["content"] = order["content"]
            if self.store.product_file(order["product"]):
                view["content"] += "\n\n📎 Dosyan bot sohbetine gönderildi. Tekrar almak için botta /orders yaz."
            access = self.store.channel_access(order["id"]) if order["channel"] else None
            if access and access["invite"] and access["removed"] is None:
                view["content"] += f"\n\n👉 Katılma linkin (tek kullanımlık): {access['invite']}"
            view["access_until"] = self.store.access_until(order)
            lessons = self.lesson_view(order)
            if lessons is not None:
                view["lessons"] = lessons
        elif order["paid"]:
            view["status"] = "expired" if self.store.live_payments(order["id"]) else "refunded"
        else:
            view["status"] = "pending"
        return view

    # ------------------------------------------------------------------ course mode
    def unlocked_lessons(self, order, now: float | None = None) -> int:
        """How many lessons of this order are open now (all of them without drip)."""
        product = self.products.get(order["product"])
        if not product or not product.lessons:
            return 0
        if not product.drip_days:
            return len(product.lessons)
        start = self.store.first_payment_time(order["id"]) or time.time()
        now = time.time() if now is None else now
        return min(len(product.lessons), 1 + int((now - start) // (product.drip_days * 86400)))

    def lesson_view(self, order) -> list[dict] | None:
        product = self.products.get(order["product"])
        if not product or not product.lessons:
            return None
        open_count = self.unlocked_lessons(order)
        start = self.store.first_payment_time(order["id"]) or time.time()
        step = (product.drip_days or 0) * 86400
        return [{"title": title, "content": content if i < open_count else None,
                 "unlocks_at": None if i < open_count else int(start + i * step)}
                for i, (title, content) in enumerate(product.lessons)]

    async def drip_lessons(self, now: float | None = None) -> int:
        """Send each newly opened lesson to its buyer (one per order per run, so a long pause does not flood)."""
        courses = [p.id for p in self.products.values() if p.lessons and p.drip_days]
        sent = 0
        for order in self.store.course_orders(courses):
            if not self.store.has_access(order, now):
                continue
            product = self.products[order["product"]]
            next_index = self.store.lessons_sent(order["id"]) + 1
            if next_index >= self.unlocked_lessons(order, now):
                continue
            title, content = product.lessons[next_index]
            text = f"📚 {product.title} · Ders {next_index + 1}/{len(product.lessons)}: {title}\n\n{content}"
            try:
                await self.api("sendMessage", chat_id=order["user"], text=text[:4096], protect_content=True)
            except TelegramError as exc:
                if exc.code == 403:
                    self.store.set_lessons_sent(order["id"], len(product.lessons))  # blocked: stop trying
                continue
            self.store.set_lessons_sent(order["id"], next_index)
            sent += 1
        return sent

    def _owns(self, user: int, product_id: str) -> bool:
        """An unrefunded purchase or active subscription for this product. Timed access in its last
        days does not count, so the buyer can extend it (the new days are added to the remaining ones)."""
        now = time.time()
        for o in self.store.paid_orders(user):
            if o["product"] != product_id or not self.store.has_access(o, now):
                continue
            until = self.store.access_until(o) if o["access_days"] else None
            if until is None or until - now > RENEW_WINDOW:
                return True
        return False

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
        product = self.products.get(order["product"])
        with self.store.db:
            self.store.db.execute("UPDATE orders SET delivered=1 WHERE id=?", (order["id"],))
        product = self.products.get(order["product"])
        if product and product.lessons:
            # lessons go after the delivery is recorded: a failure here must not resend the content
            first = 1 if product.drip_days else min(len(product.lessons), 10)
            try:
                for index in range(first):
                    title, content = product.lessons[index]
                    await self.api("sendMessage", chat_id=order["user"], protect_content=True,
                                   text=f"📚 Ders {index + 1}/{len(product.lessons)}: {title}\n\n{content}"[:4096])
                    self.store.set_lessons_sent(order["id"], index)
                note = (f"Yeni ders her {product.drip_days} günde bir bu sohbete gelir; hepsi Mini App'te "
                        "'Satın aldıklarım'da." if product.drip_days else
                        "Tüm dersler Mini App'te 'Satın aldıklarım'da da duruyor.")
                await self.api("sendMessage", chat_id=order["user"], text=note)
            except TelegramError as exc:
                log.info("Ders mesajı gönderilemedi: %s", exc.description)

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
    async def _grant_bundle(self, user: int, order, charge: str) -> None:
        """A bundle gives each included product as its own free order, tied to the bundle's charge
        ('<charge>:<product>'), so a refund of the bundle takes them back too."""
        bundle = self.products.get(order["product"])
        if not bundle or not bundle.includes:
            return
        for child_id in bundle.includes:
            child = self.products.get(child_id)
            if not child or self.store.payment(f"{charge}:{child_id}"):
                continue
            child_order = self.store.create_order(user, child, stars=0)
            expires = None
            if child.access_days:
                start = max(time.time(), self.store.active_until(user, child.id, exclude=child_order) or 0)
                expires = int(start) + child.access_days * 86400
            self.store.add_payment(f"{charge}:{child_id}", self.store.order(child_order), expires, False)
            await self.try_deliver(self.store.order(child_order))

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
            if self.store.has_other_access(row["user"], channel=row["channel"], exclude=row["order_id"], now=now):
                self.store.mark_removed(row["order_id"])  # renewed: this invite is done, the member stays
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
        for job in (self.redeliver, self.sweep_channels, self.daily_summary, self.remind_carts, self.warn_expiring,
                    self.notify_expired, self.send_broadcasts, self.ask_reviews, self.notify_waitlist, self.drip_lessons):
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
        elif sender["id"] in self.settings.admin_ids and text.split("@")[0].split(" ")[0].lower() == "/foto":
            await self._photo(sender["id"], text, message)
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

    def _promo_command(self, argument: str) -> str:
        """/kod · /kod YAZ20 20 [7g] [50x] [ürün] · /kod YAZ20 sil"""
        parts = argument.split()
        if not parts:
            rows = self.store.promos()
            if not rows:
                return ("Kod yok. Oluşturmak için: /kod YAZ20 20  (yüzde 20)\n"
                        "İsteğe bağlı: 7g (7 gün geçerli), 50x (50 kullanım), ürün kimliği.")
            lines = ["🎁 İndirim kodları"]
            for r in rows[:20]:
                state = "aktif" if r["active"] and not (r["expires"] and r["expires"] < time.time()) else "kapalı"
                limit = f"{r['uses']}/{r['max_uses']}" if r["max_uses"] is not None else f"{r['uses']} kullanım"
                lines.append(f"• {r['code']} %{r['percent']} · {limit} · {state}" + (f" · {r['product']}" if r["product"] else ""))
            return "\n".join(lines)
        code = parts[0].upper()
        if not re.fullmatch(r"[A-Z0-9_-]{3,24}", code):
            return "Kod 3–24 karakter olmalı: harf, rakam, - ve _."
        if len(parts) > 1 and parts[1].lower() in ("sil", "kapat", "iptal"):
            return f"{code} kapatıldı." if self.store.deactivate_promo(code) else f"{code} bulunamadı ya da zaten kapalı."
        if len(parts) < 2 or not parts[1].rstrip("%").isdigit() or not 1 <= int(parts[1].rstrip("%")) <= 90:
            return "Kullanım: /kod YAZ20 20 [7g] [50x] [ürün] — indirim %1–90."
        percent = int(parts[1].rstrip("%"))
        days = uses = product = None
        for token in parts[2:]:
            low = token.lower()
            if low[:-1].isdigit() and low.endswith("g"):
                days = int(low[:-1])
            elif low[:-1].isdigit() and low.endswith("x"):
                uses = int(low[:-1])
            elif low in self.products:
                product = low
            else:
                return f"'{token}' anlaşılmadı. Gün için 7g, kullanım için 50x, ürün için kimliğini yaz."
        self.store.save_promo(code, percent, product, uses, time.time() + days * 86400 if days else None)
        details = [f"%{percent} indirim"]
        if product:
            details.append(f"sadece {self.products[product].title}")
        if days:
            details.append(f"{days} gün geçerli")
        if uses:
            details.append(f"{uses} kullanım")
        link = f"\nKampanya linki: https://t.me/{self.settings.bot_username}?start=kod_{code}" if self.settings.bot_username else ""
        return f"✅ {code}: " + ", ".join(details) + "." + link + "\nAlıcı mağazada kodu yazar ya da linke tıklar."

    async def _photo(self, admin: int, text: str, message: dict) -> None:
        """/foto <product> as the caption of a picture makes it the product image; /foto <product> sil removes it."""
        parts = text.split()
        product = parts[1].lower() if len(parts) > 1 else ""
        picture = (message.get("photo") or [None])[-1]
        document = message.get("document") or {}
        if not picture and (document.get("mime_type") or "").startswith("image/"):
            picture = document
        if product not in self.products:
            reply = ("Kullanım: ürün görselini bota fotoğraf olarak gönder, açıklamasına /foto <ürün-kimliği> yaz.\n"
                     f"Ürünler: {', '.join(self.products) or 'henüz ürün yok'}")
        elif len(parts) > 2 and parts[2].lower() in ("sil", "kaldir", "kaldır"):
            reply = "Görsel kaldırıldı." if self.store.remove_product_photo(product) else "Bu üründe yüklenmiş görsel yoktu."
        elif not picture:
            reply = f"Fotoğrafı gönderirken açıklamasına /foto {product} yaz."
        elif (picture.get("file_size") or 0) > 5_000_000:
            reply = "Görsel 5 MB'tan küçük olmalı."
        else:
            self.store.set_product_photo(product, picture["file_id"], picture.get("file_unique_id") or picture["file_id"])
            reply = f"🖼 Görsel '{self.products[product].title}' ürününe eklendi; mağazada hemen görünür."
        await self.api("sendMessage", chat_id=admin, text=reply)

    async def photo_bytes(self, product_id: str) -> bytes | None:
        """The uploaded product picture, cached on disk next to the database."""
        photo = self.store.product_photo(product_id)
        if not photo:
            return None
        cache = None
        if self.settings.db_path and self.settings.db_path != ":memory:":
            folder = Path(self.settings.db_path).parent / "photos"
            cache = folder / (re.sub(r"[^A-Za-z0-9_-]", "", photo["unique_id"])[:64] + ".img")
            if cache.exists():
                return cache.read_bytes()
        data = await self.api.download(photo["file_id"])
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(data)
        return data

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
            if order["channel"] or self.store.has_other_access(order["user"], product=order["product"],
                                                               exclude=order["id"], now=now):
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

    # ------------------------------------------------------------------ announcements
    SEGMENTS = {"hepsi": "Herkes", "alicilar": "Satın alanlar", "ziyaretciler": "Henüz almayanlar",
                "sepet": "Siparişi yarım kalanlar"}

    def segment_label(self, segment: str | None) -> str:
        if segment and segment.startswith("urun:"):
            product = self.products.get(segment[5:])
            return f"'{product.title if product else segment[5:]}' alanlar"
        return self.SEGMENTS.get(segment or "hepsi", segment or "Herkes")

    def _segment(self, segment: str | None) -> str | None:
        segment = (segment or "hepsi").strip().lower()
        if segment in self.SEGMENTS or (segment.startswith("urun:") and segment[5:] in self.products):
            return None if segment == "hepsi" else segment
        raise ShopError("Kitle: hepsi, alicilar, ziyaretciler, sepet ya da urun:<ürün-kimliği> olmalı.")

    def audience(self, segment: str | None = None) -> list[int]:
        return self.store.audience(self.settings.admin_ids, self._segment(segment))

    def broadcast_state(self, now: float | None = None, segment: str | None = None) -> dict:
        now = time.time() if now is None else now
        last = self.store.last_broadcast()
        next_allowed = (last["confirmed"] + BROADCAST_GAP) if last else None
        return {"audience": len(self.audience(segment)), "segment": self._segment(segment) or "hepsi",
                "segments": [{"id": k, "label": v} for k, v in self.SEGMENTS.items()]
                + [{"id": f"urun:{p}", "label": self.segment_label(f"urun:{p}")} for p in self.products],
                "next_allowed": next_allowed if next_allowed and next_allowed > now else None,
                "history": self.store.broadcasts()}

    def _check_broadcast(self, text: str, now: float, segment: str | None = None) -> None:
        if not text or len(text) > 3500:
            raise ShopError("Duyuru metni 1–3500 karakter olmalı.")
        if self.store.sending_broadcast():
            raise ShopError("Önceki duyuru hâlâ gönderiliyor.")
        state = self.broadcast_state(now, segment)
        if state["next_allowed"]:
            hours = max(1, round((state["next_allowed"] - now) / 3600))
            raise ShopError(f"Günde en fazla bir duyuru gönderilebilir. Yaklaşık {hours} saat sonra tekrar dene.")
        if not state["audience"]:
            raise ShopError("Duyuru gidecek kimse yok: bu kitlede kimse yok (ya da hepsi /durdur dedi).")

    def start_broadcast(self, text: str, now: float | None = None, segment: str | None = None) -> int:
        """Queue an announcement right away (the panel asks for confirmation itself)."""
        now = time.time() if now is None else now
        text = (text or "").strip()
        segment = self._segment(segment)
        self._check_broadcast(text, now, segment)
        broadcast_id = self.store.create_broadcast(text, segment)
        return self.store.confirm_broadcast(broadcast_id, self.audience(segment))

    async def send_broadcasts(self, now: float | None = None) -> int:
        current = self.store.sending_broadcast()
        if not current:
            return 0
        sent = 0
        for user in self.store.broadcast_batch(current["id"], BROADCAST_BATCH):
            try:
                await self.api("sendMessage", chat_id=user, text=current["text"] + BROADCAST_FOOTER)
            except TelegramError as exc:
                if exc.retry_after:
                    return sent  # Telegram asked us to slow down: continue next tick
                if exc.code == 403:
                    self.store.mark_blocked(user)
                self.store.broadcast_result(current["id"], user, False)
                continue
            except Exception:  # noqa: BLE001
                self.store.broadcast_result(current["id"], user, False)
                continue
            self.store.broadcast_result(current["id"], user, True)
            sent += 1
            await asyncio.sleep(0.04)  # ~25 messages a second, under Telegram's limit
        if not self.store.broadcast_batch(current["id"], 1):
            self.store.finish_broadcast(current["id"])
            done = self.store.db.execute("SELECT * FROM broadcasts WHERE id=?", (current["id"],)).fetchone()
            await self.notify_admins(f"📣 Duyuru tamamlandı: {done['sent']} kişiye ulaştı"
                                     + (f", {done['failed']} kişiye ulaşmadı (botu engellemiş)." if done["failed"] else "."))
        return sent

    async def warn_expiring(self, now: float | None = None) -> int:
        """Three days before timed access ends: say when, and offer to extend."""
        now = time.time() if now is None else now
        sent = 0
        for order in self.store.expiring_timed_orders(now, RENEW_WINDOW):
            self.store.mark_expiry_warned(order["id"])
            if self.store.has_other_access(order["user"], product=order["product"], exclude=order["id"], now=now) \
                    and (self.store.active_until(order["user"], order["product"], now, exclude=order["id"]) or 0) \
                    > (self.store.access_until(order) or 0):
                continue  # already extended
            product = self.products.get(order["product"])
            day = time.strftime("%d.%m.%Y", time.localtime(self.store.access_until(order)))
            extra = {}
            if product and self.settings.sales_enabled:
                extra = {"reply_markup": {"inline_keyboard": [[{
                    "text": f"⏳ {product.access_days or order['access_days']} gün uzat · ⭐{product.stars}",
                    "web_app": {"url": f"{self.settings.webapp_url}/app?p={product.id}"}}]]}}
            try:
                await self.api("sendMessage", chat_id=order["user"],
                               text=f"'{order['title']}' erişimin {day} tarihinde bitiyor. Şimdi uzatırsan "
                                    "yeni süre kalan günlerinin üstüne eklenir.", **extra)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                log.info("Süre uyarısı gönderilemedi: %s", type(exc).__name__)
        return sent

    async def remind_carts(self, now: float | None = None) -> int:
        """One gentle reminder for an order left unpaid for an hour (never twice for the same product)."""
        if not self.settings.cart_reminder or not self.settings.sales_enabled:
            return 0
        now = time.time() if now is None else now
        sent = 0
        for order in self.store.abandoned_orders(now):
            self.store.mark_reminded(order["id"])
            product = self.products.get(order["product"])
            if not product:
                continue
            text = f"'{order['title']}' siparişin yarım kaldı. Hâlâ istiyorsan tek dokunuşla tamamlayabilirsin."
            url = f"{self.settings.webapp_url}/app?p={order['product']}"
            price = order["stars"]
            percent = self.settings.cart_discount
            if percent and product.kind != "subscription" and not product.flexible and not order["promo"]:
                code = "SEPET" + secrets.token_hex(3).upper()
                self.store.save_promo(code, percent, product=product.id, max_uses=1, expires=now + 24 * 3600)
                price = max(1, -(-product.stars * (100 - percent) // 100))
                text = (f"'{order['title']}' siparişin yarım kaldı. Sana özel %{percent} indirim: ⭐{product.stars} yerine "
                        f"⭐{price}. Kod: {code} (24 saat, tek kullanım).")
                url += f"&code={code}"
            try:
                await self.api(
                    "sendMessage", chat_id=order["user"], text=text,
                    reply_markup={"inline_keyboard": [[{"text": f"⭐ {price} ile tamamla", "web_app": {"url": url}}]]})
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
        message = "Sipariş geçersiz ya da süresi doldu. Mağazayı yeniden açıp tekrar dene."
        product = self.products.get(order["product"]) if order else None
        if ok and product:
            reason, _ = self.availability(product)
            if reason:
                ok, message = False, reason
        extra = {} if ok else {"error_message": message}
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
        elif order["gift"]:
            expires = None  # the clock starts when the friend opens the gift
        elif order["access_days"]:
            # an extension bought in the last days continues from the current end, not from today
            start = max(time.time(), self.store.active_until(user, order["product"], exclude=order["id"]) or 0)
            expires = int(start) + int(order["access_days"]) * 86400
        renewal = bool(self.store.live_payments(order["id"]))
        self.store.add_payment(charge, order, expires, bool(payment.get("is_recurring")))
        if order["promo"] and not renewal:
            self.store.count_promo_use(order["promo"])
        if order["stars"] > 0 and self.settings.sale_webhook:
            await self._sale_webhook(order, charge, sender, renewal=bool(self.store.live_payments(order["id"])[1:]))
        if order["stars"] > 0 and self.store.paid_count(user) == 1:
            await self._reward_referrer(user)
        if order["gift"]:
            await self._gift_paid(order, sender)
            return
        await self._grant_bundle(user, order, charge)
        if order["channel"]:
            self.store.reopen_channel(order["id"])
        await self.try_deliver(self.store.order(order["id"]))
        if self.settings.notify_sales:
            kind = "🔁 Abonelik yenilendi" if renewal or payment.get("is_recurring") and not payment.get("is_first_recurring") \
                else "💰 Yeni satış"
            until = f"\nBitiş: {time.strftime('%d.%m.%Y', time.localtime(expires))}" if expires else ""
            asked = f"\nCevap: {order['answer']}" if order["answer"] else ""
            await self.notify_admins(f"{kind}: {order['title']} · ⭐{order['stars']}\nAlıcı: {_who(sender or {'id': user})}{until}{asked}")
        if not renewal:
            await self._offer_upsell(user, order)
        product = self.products.get(order["product"])
        if product and product.stock:
            left = self.availability(product)[1]
            if left is not None and left in (0, 1, 3, 5, 10) and left < product.stock:
                await self.notify_admins(f"📦 Stok uyarısı: '{product.title}' için {left} adet kaldı."
                                         if left else f"📦 '{product.title}' tükendi. Stoğu artırırsan bekleme listesine haber gider.")

    def reports(self) -> dict[str, bytes]:
        """satislar.csv and musteriler.csv, UTF-8 with BOM and ';' so Excel opens Turkish text correctly."""
        day = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else ""  # noqa: E731

        def table(header: list[str], rows) -> bytes:
            out = io.StringIO()
            writer = csv.writer(out, delimiter=";")
            writer.writerow(header)
            writer.writerows(rows)
            return ("\ufeff" + out.getvalue()).encode("utf-8")

        sales = table(
            ["tarih", "ürün", "başlık", "tür", "yıldız", "kod", "durum", "bitiş", "kullanıcı", "ad", "kullanıcı_adı",
             "kaynak", "cevap", "sipariş", "ödeme"],
            ([day(r["created"]), r["product"], r["title"], r["kind"], r["stars"], r["promo"] or "",
              "iade" if r["refunded"] else ("paket" if r["stars"] == 0 else "ödendi"), day(r["expires"]),
              r["user"], r["name"] or "", r["username"] or "", r["source"] or "", r["answer"] or "",
              r["order_id"], r["charge"]]
             for r in self.store.sales_rows()))
        customers = table(
            ["kullanıcı", "ad", "kullanıcı_adı", "aşama", "harcanan_yıldız", "ödeme", "kaynak", "not", "ilk", "son"],
            ([c["user"], c["name"] or "", c["username"] or "", c["stage"], c["spent"], c["payments"],
              c["source"] or "", c["note"] or "", day(c["first_seen"]), day(c["last_seen"])]
             for c in self.store.customers(limit=100000)))
        return {"satislar.csv": sales, "musteriler.csv": customers}

    async def send_reports(self, chat_id: int) -> None:
        stamp = time.strftime("%Y-%m-%d")
        for name, body in self.reports().items():
            await self.api("sendDocument", chat_id=chat_id, files={"document": (f"{stamp}-{name}", body)},
                           caption=f"{self.settings.shop_name} · {name.removesuffix('.csv')}")

    # ------------------------------------------------------------------ share card
    async def share_card(self, product_id: str, chat_id) -> None:
        """A product card with a buy button, for a channel/group (the bot must be able to post there) or for the
        admin to forward. Channels do not allow Mini App buttons, so the button opens the bot with p_<id>."""
        product = self.products.get(product_id)
        if not product:
            raise ShopError("Ürün bulunamadı.", 404)
        if not self.settings.bot_username:
            raise ShopError("Botun kullanıcı adı bilinmiyor.")
        rating, reviews = self.store.ratings().get(product.id, (None, 0))
        lines = [f"<b>{_html(product.title)}</b>", _html(product.description)]
        price = f"⭐ {product.stars}" + (f"  <s>⭐ {product.compare_at}</s>" if product.compare_at else "")
        if product.flexible:
            price = f"⭐ {product.stars}'den başlayan, istediğin kadar"
        lines.append(price)
        if reviews:
            lines.append(f"★ {rating} · {reviews} değerlendirme")
        if product.lessons:
            lines.append(f"📚 {len(product.lessons)} ders")
        left = self.availability(product)[1]
        if left is not None and left <= 20:
            lines.append(f"Son {left} adet")
        button = {"inline_keyboard": [[{"text": f"⭐ {product.stars} ile al" if not product.flexible else "⭐ Satın al",
                                        "url": f"https://t.me/{self.settings.bot_username}?start=p_{product.id}"}]]}
        caption = "\n\n".join(lines[:2]) + "\n\n" + "\n".join(lines[2:])
        photo = self.store.product_photo(product.id)
        picture = photo["file_id"] if photo else product.photo_url
        if picture and len(caption) <= 1024:
            await self.api("sendPhoto", chat_id=chat_id, photo=picture, caption=caption, parse_mode="HTML",
                           reply_markup=button)
        else:
            await self.api("sendMessage", chat_id=chat_id, text=caption[:4096], parse_mode="HTML",
                           reply_markup=button, disable_web_page_preview=True)

    # ------------------------------------------------------------------ sale webhook
    async def _sale_webhook(self, order, charge: str, sender: dict | None, renewal: bool) -> None:
        """POST the sale as JSON to the seller's URL (Zapier, Make, Google Apps Script…). Never blocks a payment."""
        sender = sender or {}
        payload = {
            "event": "renewal" if renewal else "sale", "shop": self.settings.shop_name,
            "order_id": order["id"], "charge_id": charge, "product": order["product"], "title": order["title"],
            "kind": order["kind"], "stars": order["stars"], "promo": order["promo"], "gift": bool(order["gift"]),
            "answer": order["answer"], "user_id": order["user"], "username": sender.get("username"),
            "name": " ".join(x for x in (sender.get("first_name"), sender.get("last_name")) if x) or None,
            "time": int(time.time()),
        }
        try:
            await self.post_json(self.settings.sale_webhook, payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("Satış webhook'u gönderilemedi: %s", type(exc).__name__)

    async def post_json(self, url: str, payload: dict) -> None:
        if not url.startswith("https://"):
            raise ValueError("https gerekli")
        import httpx
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
            response = await client.post(url, json=payload, headers={"User-Agent": "StarsShop/1"})
            response.raise_for_status()

    # ------------------------------------------------------------------ gifts
    async def _gift_paid(self, order, sender: dict | None) -> None:
        code = secrets.token_urlsafe(9).replace("-", "").replace("_", "").upper()[:10]
        self.store.create_gift(code, order)
        code = self.store.gift_for_order(order["id"])["code"]
        link = self.gift_link(code)
        text = (f"🎁 Hediyen hazır: {order['title']}\nBu linki hediye etmek istediğin kişiye gönder. Linki ilk açan "
                f"kişi ürünü alır, tek kullanımlık:\n{link}") if link else \
            f"🎁 Hediyen hazır: {order['title']}. Hediye kodu: {code} (botta /start gift_{code})"
        extra = {}
        if link:
            share = f"https://t.me/share/url?url={_quote(link)}&text={_quote('Sana bir hediyem var 🎁 ' + order['title'])}"
            extra = {"reply_markup": {"inline_keyboard": [[{"text": "📤 Hediyeyi gönder", "url": share}]]}}
        try:
            await self.api("sendMessage", chat_id=order["user"], text=text, disable_web_page_preview=True, **extra)
        except TelegramError as exc:
            log.warning("Hediye linki gönderilemedi: %s", exc.description)
        if self.settings.notify_sales:
            await self.notify_admins(f"🎁 Hediye satışı: {order['title']} · ⭐{order['stars']}\n"
                                     f"Alan: {_who(sender or {'id': order['user']})}")

    async def claim_gift(self, user: int, code: str, sender: dict | None = None) -> str:
        """Give the gifted product to the first person who opens the link. Returns the reply text."""
        gift = self.store.gift(code)
        if not gift:
            return "Bu hediye linki geçersiz."
        order = self.store.order(gift["order_id"])
        payments = self.store.live_payments(order["id"])
        if not payments:
            return "Bu hediye iptal edilmiş."
        if gift["claimed_by"] == user:
            return "Bu hediye zaten sende. İçeriği tekrar almak için /orders yaz."
        if gift["claimed_by"] is not None:
            return "Bu hediye başka biri tarafından açılmış."
        if gift["giver"] == user:
            return "Bu senin aldığın hediye 🙂 Linki hediye etmek istediğin kişiye gönder."
        if self._owns(user, order["product"]):
            return "Bu ürün zaten sende. Hediyeyi açmadım; linki başka birine verebilirsin."
        item = SimpleNamespace(id=order["product"], kind=order["kind"], title=order["title"], content=order["content"],
                               channel_id=order["channel"], access_days=order["access_days"])
        claim_order = self.store.create_order(user, item, stars=0)
        if not self.store.claim_gift(gift["code"], user, claim_order):
            return "Bu hediye başka biri tarafından açılmış."
        expires = None
        if order["access_days"]:
            start = max(time.time(), self.store.active_until(user, order["product"], exclude=claim_order) or 0)
            expires = int(start) + int(order["access_days"]) * 86400
        charge = f"{payments[0]['charge']}:gift"
        self.store.add_payment(charge, self.store.order(claim_order), expires, False)
        await self._grant_bundle(user, self.store.order(claim_order), charge)
        await self.try_deliver(self.store.order(claim_order))
        with contextlib.suppress(Exception):
            await self.api("sendMessage", chat_id=gift["giver"],
                           text=f"🎁 Hediyen açıldı: {order['title']} → {_who(sender or {'id': user})}")
        return f"🎁 Sana bir hediye var: {order['title']}! İçeriğin bu sohbete gönderildi."

    # ------------------------------------------------------------------ upsell
    async def _offer_upsell(self, user: int, order) -> None:
        product = self.products.get(order["product"])
        target = self.products.get(product.upsell) if product and product.upsell else None
        if not target or self._owns(user, target.id) or self.availability(target)[0]:
            return
        price, extra = target.stars, ""
        target_url = f"{self.settings.webapp_url}/app?p={target.id}"
        if product.upsell_percent and target.kind != "subscription":
            code = "UP" + secrets.token_hex(3).upper()
            self.store.save_promo(code, product.upsell_percent, product=target.id, max_uses=1,
                                  expires=time.time() + 48 * 3600)
            price = max(1, -(-target.stars * (100 - product.upsell_percent) // 100))
            extra = f" yerine ⭐{price} (%{product.upsell_percent} indirim, 48 saat geçerli, kod: {code})"
            target_url += f"&code={code}"
        try:
            await self.api("sendMessage", chat_id=user,
                           text=f"✨ Bunu da seveceğini düşündük: {target.title}\n{target.description}\n"
                                f"⭐{target.stars}{extra}",
                           reply_markup={"inline_keyboard": [[{"text": f"🛍 İncele · ⭐{price}",
                                                               "web_app": {"url": target_url}}]]})
        except TelegramError as exc:
            log.info("Öneri gönderilemedi: %s", exc.description)

    # ------------------------------------------------------------------ reviews
    def can_review(self, user: int, product_id: str) -> bool:
        return any(o["product"] == product_id and self.store.live_payments(o["id"])
                   for o in self.store.paid_orders(user))

    def reviews(self, product_id: str, viewer: int | None = None) -> dict:
        items = self.store.reviews(product_id, limit=20)
        mine = self.store.review(product_id, viewer) if viewer else None
        average, count = self.store.ratings().get(product_id, (None, 0))
        return {"average": average, "count": count,
                "items": [{"rating": r["rating"], "text": r["text"], "name": r["name"] or "Alıcı",
                           "created": r["created"]} for r in items],
                "can_review": bool(viewer) and self.can_review(viewer, product_id),
                "mine": {"rating": mine["rating"], "text": mine["text"]} if mine else None}

    async def add_review(self, user: int, product_id: str, rating: int, text: str | None) -> dict:
        if product_id not in self.products and not self.store.paid_orders(user):
            raise ShopError("Ürün bulunamadı.", 404)
        if not self.can_review(user, product_id):
            raise ShopError("Yalnızca satın aldığın ürünlere puan verebilirsin.", 403)
        if not 1 <= rating <= 5:
            raise ShopError("Puan 1 ile 5 arasında olmalı.")
        contact = self.store.contact(user)
        name = None
        if contact and contact["name"]:
            parts = contact["name"].split()
            name = parts[0] + (f" {parts[1][0]}." if len(parts) > 1 else "")
        first = self.store.review(product_id, user) is None
        self.store.save_review(product_id, user, rating, text, name)
        if first:
            title = self.products[product_id].title if product_id in self.products else product_id
            body = f": {text.strip()[:200]}" if text and text.strip() else ""
            await self.notify_admins(f"{'⭐' * rating} Yeni yorum · {title} · {name or user}{body}\n"
                                     f"Gizlemek için: /yorumlar gizle {product_id} {user}")
        return self.reviews(product_id, user)

    async def ask_reviews(self, now: float | None = None) -> int:
        """A day after a purchase, one message asking for a rating (orders older than a week are skipped)."""
        if not self.settings.review_request:
            return 0
        now = time.time() if now is None else now
        sent = 0
        for order in self.store.review_candidates(now - 86400, now - 7 * 86400)[:50]:
            self.store.mark_review_requested(order["id"])
            if order["product"] not in self.products or self.store.review(order["product"], order["user"]):
                continue
            try:
                await self.api("sendMessage", chat_id=order["user"],
                               text=f"'{order['title']}' nasıldı? Bir puan bırakırsan diğer alıcılara çok yardımcı olur ⭐",
                               reply_markup={"inline_keyboard": [[{"text": "⭐ Puan ver",
                                                                   "web_app": {"url": f"{self.settings.webapp_url}/app?p={order['product']}&review=1"}}]]})
                sent += 1
            except Exception as exc:  # noqa: BLE001
                log.info("Yorum isteği gönderilemedi: %s", type(exc).__name__)
        return sent

    # ------------------------------------------------------------------ waitlist
    def join_waitlist(self, user: int, product_id: str) -> dict:
        product = self.products.get(product_id)
        if not product:
            raise ShopError("Ürün bulunamadı.", 404)
        if not self.availability(product)[0] == "Bu ürün tükendi.":
            raise ShopError("Bu ürün şu an satın alınabilir.", 409)
        self.store.join_waitlist(product_id, user)
        return {"waiting": True}

    async def notify_waitlist(self, now: float | None = None) -> int:
        """When a sold-out product can be bought again (stock raised, refund), tell everyone who waited."""
        if not self.settings.sales_enabled:
            return 0
        sent = 0
        for product_id, _count in self.store.waitlist_counts().items():
            product = self.products.get(product_id)
            if not product or self.availability(product, now)[0]:
                continue
            users = self.store.waiting(product_id)[:200]
            for user in users:
                if self._owns(user, product_id):
                    continue
                try:
                    await self.api("sendMessage", chat_id=user,
                                   text=f"🔔 '{product.title}' yeniden satışta! Stok sınırlı, kaçırma.",
                                   reply_markup={"inline_keyboard": [[{"text": f"🛍 Al · ⭐{product.stars}",
                                                                       "web_app": {"url": f"{self.settings.webapp_url}/app?p={product.id}"}}]]})
                    sent += 1
                except Exception as exc:  # noqa: BLE001
                    log.info("Bekleme listesi bildirimi gönderilemedi: %s", type(exc).__name__)
            self.store.leave_waitlist(product_id, users)
        return sent

    def referral_link(self, user: int) -> str | None:
        if not self.settings.referral_percent or not self.settings.bot_username:
            return None
        return f"https://t.me/{self.settings.bot_username}?start=ref_{user}"

    async def _reward_referrer(self, buyer: int) -> None:
        """A friend who came through ref_<id> made a first paid purchase: the referrer gets a one-use code."""
        percent = self.settings.referral_percent
        contact = self.store.contact(buyer)
        source = (contact["source"] if contact else None) or ""
        if not percent or not source.startswith("ref_") or not source[4:].isdigit():
            return
        referrer = int(source[4:])
        if referrer == buyer or not self.store.contact(referrer):
            return  # only people who already know the bot
        code = "REF" + secrets.token_hex(3).upper()
        if not self.store.add_referral_reward(buyer, referrer, code):
            return
        self.store.save_promo(code, percent, max_uses=1, expires=time.time() + 30 * 86400)
        try:
            await self.api("sendMessage", chat_id=referrer, text=(
                f"🎁 Davet ettiğin bir arkadaşın ilk alışverişini yaptı, teşekkürler!\n"
                f"Sana özel %{percent} indirim kodu: {code}\n30 gün geçerli, bir kez kullanılır. "
                f"Mağazada ürün sayfasında 'İndirim kodu' alanına yaz."))
        except TelegramError as exc:
            log.warning("Davet ödülü gönderilemedi: user=%s %s", referrer, exc)

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
        elif command == "/refund" and user in (self.settings.refund_ids if self.settings.refund_ids is not None
                                                 else self.settings.admin_ids):
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
        elif command in ("/rapor", "/report") and user in self.settings.admin_ids:
            try:
                await self.send_reports(user)
                return
            except TelegramError as exc:
                reply = f"Rapor gönderilemedi: {exc.description}"
        elif command in ("/davet", "/invite"):
            link = self.referral_link(user)
            if not link:
                reply = "Davet programı bu mağazada kapalı."
            else:
                stats = self.store.referral_stats(user)
                reply = (f"Arkadaşlarını davet et: biri ilk alışverişini yaptığında sana %{self.settings.referral_percent} "
                         f"indirim kodu gelir.\nDavet linkin: {link}\n"
                         f"Gelen: {stats['invited']} · Ödül: {stats['rewarded']}")
        elif command in ("/durdur", "/stop"):
            self.store.set_opt_out(user, True)
            reply = "Tamam, artık duyuru almayacaksın. Siparişlerin ve destek mesajların etkilenmez. Tekrar açmak için /baslat."
        elif command in ("/baslat", "/resume"):
            self.store.set_opt_out(user, False)
            reply = "Duyurular tekrar açık."
        elif command == "/duyuru" and user in self.settings.admin_ids:
            segment, body = None, argument
            if argument.startswith("@"):
                segment, _, body = argument[1:].partition(" ")
                body = body.strip()
            try:
                segment = self._segment(segment)
                self._check_broadcast(body, time.time(), segment)
            except ShopError as exc:
                reply = str(exc) if body else (
                    "Kullanım: /duyuru <metin> — botunla konuşmuş herkese.\n"
                    "Kitle seçmek için: /duyuru @alicilar <metin> · @ziyaretciler · @sepet · @urun:<ürün-kimliği>\n"
                    "Günde en fazla bir duyuru; önce önizleme gösterilir.")
            else:
                self.store.create_broadcast(body, segment)
                count = len(self.audience(segment))
                await self.api("sendMessage", chat_id=user, text=body + BROADCAST_FOOTER)
                reply = (f"↑ Önizleme. Kitle: {self.segment_label(segment)} · {count} kişi.\n"
                         "Göndermek için /duyuru_onay, vazgeçmek için /duyuru_iptal.")
        elif command == "/duyuru_onay" and user in self.settings.admin_ids:
            pending = self.store.pending_broadcast()
            if not pending:
                reply = "Onay bekleyen duyuru yok. Önce /duyuru <metin> yaz."
            else:
                segment = pending["segment"]
                try:
                    self._check_broadcast(pending["text"], time.time(), segment)
                except ShopError as exc:
                    reply = str(exc)
                else:
                    count = self.store.confirm_broadcast(pending["id"], self.store.audience(self.settings.admin_ids, segment))
                    reply = f"📣 {count} kişiye gönderiliyor. Bitince haber vereceğim."
        elif command == "/duyuru_iptal" and user in self.settings.admin_ids:
            reply = "Duyuru iptal edildi." if self.store.cancel_broadcast() else "İptal edilecek duyuru yok."
        elif command in ("/kod", "/code") and user in self.settings.admin_ids:
            reply = self._promo_command(argument)
        elif command == "/start" and argument.lower().startswith("gift_"):
            contact = self.store.contact(user)
            who = {"id": user, "first_name": contact["name"] if contact else None,
                   "username": contact["username"] if contact else None}
            reply = await self.claim_gift(user, argument[5:], who)
        elif command in ("/yorumlar", "/reviews") and user in self.settings.admin_ids:
            parts = argument.split()
            if len(parts) == 3 and parts[0] in ("gizle", "goster", "göster") and parts[2].isdigit():
                ok = self.store.hide_review(parts[1], int(parts[2]), parts[0] == "gizle")
                reply = "Tamam." if ok else "Bu yorum bulunamadı."
            else:
                rows = self.store.reviews(limit=15, include_hidden=True)
                lines = [f"{'🙈 ' if r['hidden'] else ''}{'⭐' * r['rating']} {r['product']} · {r['name'] or r['user']}"
                         + (f": {r['text'][:80]}" if r["text"] else "") + f"\n   /yorumlar {'goster' if r['hidden'] else 'gizle'} "
                         f"{r['product']} {r['user']}" for r in rows]
                reply = "Son yorumlar\n" + "\n".join(lines) if rows else "Henüz yorum yok."
        elif command in ("/paylas", "/share") and user in self.settings.admin_ids:
            parts = argument.split()
            if not parts or parts[0].lower() not in self.products:
                reply = ("Kullanım: /paylas <ürün> — kartı sana gönderir, kanalına iletebilirsin.\n"
                         "/paylas <ürün> @kanal ya da -100… — doğrudan kanala/gruba atar (bot orada yönetici olmalı).\n"
                         f"Ürünler: {', '.join(self.products) or 'henüz ürün yok'}")
            else:
                target = parts[1] if len(parts) > 1 else user
                if isinstance(target, str) and target.lstrip("-").isdigit():
                    target = int(target)
                try:
                    await self.share_card(parts[0].lower(), target)
                    reply = "📣 Kart kanala gönderildi." if len(parts) > 1 else "↑ Bu kartı kanalına ya da grubuna ilet."
                except TelegramError as exc:
                    reply = f"Gönderilemedi: bot o kanalda yönetici ve mesaj gönderebilir olmalı. ({exc.description})"
        elif command in ("/kaynaklar", "/sources") and user in self.settings.admin_ids:
            rows = self.store.source_stats()
            reply = "📈 Kaynaklar (kişi · alıcı · ⭐ · dönüşüm)\n" + "\n".join(
                f"• {r['source']}: {r['people']} · {r['buyers']} · ⭐{r['stars']} · %{r['conversion']}" for r in rows[:20]) \
                if rows else "Henüz veri yok. Linklerine ?start=instagram gibi bir kaynak ekle."
        elif command == "/start" and argument.lower().startswith("kod_"):
            code = argument[4:]
            try:
                any_product = next(iter(self.products))
                promo = self.store.promo(code)
                if not promo or not promo["active"]:
                    raise ShopError("kod yok")
                self.quote(promo["product"] or any_product, code)
            except (ShopError, StopIteration):
                reply = "Bu kampanya kodu artık geçerli değil. Mağazadaki güncel ürünlere göz atabilirsin."
            else:
                target = f"/app?code={promo['code']}" + (f"&p={promo['product']}" if promo["product"] else "")
                await self.api("sendMessage", chat_id=user,
                               text=f"🎁 {promo['code']} koduyla %{promo['percent']} indirim! Ürünü seçince kod otomatik uygulanır.",
                               reply_markup={"inline_keyboard": [[{"text": "🛍 İndirimle al",
                                                                   "web_app": {"url": self.settings.webapp_url + target}}]]})
                return
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
                           text="Yönetim\nMüşteriler: /musteriler · İade: /refund <charge_id> · Ödeme sorunları: /issues · CSV rapor: /rapor\nKaynaklar: /kaynaklar · Yorumlar: /yorumlar · Ürün görseli: fotoğraf + /foto <ürün> · Kanala kart: /paylas <ürün>\n"
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
