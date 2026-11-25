"""Operator commands.

    python -m app.cli setup            # webhook + menu button + commands
    python -m app.cli poll             # local development without a public webhook
    python -m app.cli issues           # payments that need a refund or manual delivery
    python -m app.cli refund CHARGE_ID [--yes]
    python -m app.cli check            # validate settings and products.json
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime

from .config import Settings, load_products
from .shop import StarsShop, process_update
from .store import Store
from .telegram import BotAPI

COMMANDS = [
    {"command": "start", "description": "Mağazayı aç"},
    {"command": "orders", "description": "Satın aldıklarımı yeniden gönder"},
    {"command": "paysupport", "description": "Ödeme desteği"},
    {"command": "terms", "description": "Satış koşulları"},
]


async def setup(settings: Settings) -> None:
    api = BotAPI(settings.bot_token)
    try:
        me = await api("getMe")
        await api("setWebhook", url=f"{settings.webapp_url}/telegram/webhook",
                  secret_token=settings.webhook_secret,
                  allowed_updates=["message", "pre_checkout_query"], drop_pending_updates=False)
        await api("setChatMenuButton", menu_button={
            "type": "web_app", "text": "Mağaza", "web_app": {"url": settings.webapp_url + "/app"}})
        await api("setMyCommands", commands=COMMANDS)
        print(f"@{me['username']} hazır: webhook, menü butonu ve komutlar ayarlandı.")
    finally:
        await api.close()


async def poll(settings: Settings) -> None:
    """getUpdates loop for development. Refuses to run while a webhook is set."""
    api = BotAPI(settings.bot_token)
    store = Store(settings.db_path)
    shop = StarsShop(api, settings, load_products(settings.products_path), store)
    failures: dict[int, int] = {}
    try:
        if (await api("getWebhookInfo")).get("url"):
            raise SystemExit("Botta webhook ayarlı. Polling için önce deleteWebhook çağır.")
        offset = store.get_state("offset")
        logging.info("Polling başladı (satış=%s)", settings.sales_enabled)
        while True:
            try:
                updates = await api("getUpdates", offset=offset, timeout=25,
                                    allowed_updates=["message", "pre_checkout_query"])
                for update in updates:
                    await process_update(shop, update, failures)
                    offset = update["update_id"] + 1
                    store.set_state("offset", offset)
                await shop.redeliver()
            except Exception as exc:  # noqa: BLE001
                logging.error("Güncelleme işlenemedi: %s; yeniden denenecek", type(exc).__name__)
                await asyncio.sleep(5)
    finally:
        store.close()
        await api.close()


def issues(settings: Settings) -> None:
    rows = Store(settings.db_path).open_issues()
    if not rows:
        print("Açık ödeme sorunu yok.")
    for row in rows:
        when = datetime.fromtimestamp(row["created"]).strftime("%Y-%m-%d %H:%M")
        print(f"{when}  {row['reason']:<32} user={row['user']}  stars={row['stars']}  charge={row['charge']}")


async def refund(settings: Settings, charge: str, yes: bool) -> None:
    if not yes and input(f"{charge} iade edilsin mi? [e/H] ").strip().lower() not in ("e", "evet", "y", "yes"):
        print("İptal edildi.")
        return
    api = BotAPI(settings.bot_token)
    store = Store(settings.db_path)
    try:
        shop = StarsShop(api, settings, load_products(settings.products_path), store)
        print("İade edildi." if await shop.refund(charge) else "Bu ödeme zaten iade edilmiş.")
    finally:
        store.close()
        await api.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    sub.add_parser("poll")
    sub.add_parser("issues")
    sub.add_parser("check")
    r = sub.add_parser("refund")
    r.add_argument("charge")
    r.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    if args.cmd == "setup":
        asyncio.run(setup(settings))
    elif args.cmd == "poll":
        asyncio.run(poll(settings))
    elif args.cmd == "issues":
        issues(settings)
    elif args.cmd == "refund":
        asyncio.run(refund(settings, args.charge, args.yes))
    elif args.cmd == "check":
        products = load_products(settings.products_path)
        print(f"Ayarlar geçerli. {len(products)} ürün: " + ", ".join(products))


if __name__ == "__main__":
    main()
