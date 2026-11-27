"""Platform operator commands for the hub.

    python -m app.hubcli keys                 # generate HUB_MASTER_KEY and HUB_WEBHOOK_SECRET
    python -m app.hubcli check                # validate hub settings
    python -m app.hubcli setup                # hub bot webhook + panel button + commands, then re-sync every shop
    python -m app.hubcli tenants              # every shop: owner, bot, plan, sales, products, Stars
    python -m app.hubcli suspend TENANT_ID    # stop sales (content or policy problem); payments are still recorded
    python -m app.hubcli unsuspend TENANT_ID
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
from datetime import datetime

from cryptography.fernet import Fernet

from .hub import HUB_COMMANDS, create_hub_app
from .tenants import HubSettings


async def setup(settings: HubSettings) -> None:
    app = create_hub_app(settings, background=False)
    hub = app.state.hub
    api = hub.hub_api
    me = await api("getMe")
    await api("setWebhook", url=f"{settings.base_url}/hub/webhook", secret_token=settings.webhook_secret,
              allowed_updates=["message", "pre_checkout_query"], drop_pending_updates=False)
    await api("setChatMenuButton", menu_button={
        "type": "web_app", "text": "Panel", "web_app": {"url": f"{settings.base_url}/panel"}})
    await api("setMyCommands", commands=HUB_COMMANDS)
    print(f"@{me['username']} hazır: webhook, panel butonu ve komutlar ayarlandı.")
    if settings.bot_username and settings.bot_username.lower() != me["username"].lower():
        print(f"Uyarı: HUB_BOT_USERNAME={settings.bot_username} ama bot @{me['username']}.")
    for tenant in hub.registry.all():
        ok = await hub.sync(tenant)
        print(f"  @{tenant['username']}: {'senkron' if ok else 'HATA: ' + (hub.registry.tenant(tenant['id'])['last_sync_error'] or '')}")
    hub.close()


def tenants(settings: HubSettings) -> None:
    app = create_hub_app(settings, background=False)
    hub = app.state.hub
    rows = hub.registry.all(include_deleted=True)
    if not rows:
        print("Henüz mağaza yok.")
    for t in rows:
        stats = hub.stats(t)
        created = datetime.fromtimestamp(t["created"]).strftime("%Y-%m-%d")
        print(f"{t['id']}  @{t['username']:<24} owner={t['owner']:<12} plan={hub.plan(t['owner']).name:<5} "
              f"{t['status']:<9} satış={'açık' if t['sales_enabled'] else 'kapalı':<6} "
              f"ürün={hub.registry.product_count(t['id']):<3} ödeme={stats['payments']:<5} ⭐={stats['stars']:<7} "
              f"{'yönlendirme' if t['forward_url'] else 'bağımsız'}  {created}"
              + (f"  sorun={len(stats['issues'])}" if stats["issues"] else ""))
    hub.close()


def set_status(settings: HubSettings, tenant_id: str, status: str) -> None:
    app = create_hub_app(settings, background=False)
    registry = app.state.hub.registry
    if not registry.tenant(tenant_id):
        raise SystemExit("Böyle bir mağaza yok.")
    fields = {"status": status}
    if status == "suspended":
        fields["sales_enabled"] = False
    registry.update(tenant_id, **fields)
    print(f"{tenant_id}: {status}")
    app.state.hub.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("keys", "check", "setup", "tenants"):
        sub.add_parser(name)
    for name in ("suspend", "unsuspend"):
        sub.add_parser(name).add_argument("tenant_id")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    if args.cmd == "keys":
        print(f"HUB_MASTER_KEY={Fernet.generate_key().decode()}")
        print(f"HUB_WEBHOOK_SECRET={secrets.token_urlsafe(32)}")
        print("HUB_MASTER_KEY'i kaybetme: bağlı botların token'ları onunla şifreli.")
        return
    settings = HubSettings.from_env()
    if args.cmd == "check":
        print(f"Ayarlar geçerli: {settings.base_url} · Pro {settings.pro_stars}⭐/ay · deneme {settings.trial_days} gün")
    elif args.cmd == "setup":
        asyncio.run(setup(settings))
    elif args.cmd == "tenants":
        tenants(settings)
    elif args.cmd == "suspend":
        set_status(settings, args.tenant_id, "suspended")
    elif args.cmd == "unsuspend":
        set_status(settings, args.tenant_id, "active")


if __name__ == "__main__":
    main()
