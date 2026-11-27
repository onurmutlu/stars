"""FastAPI app: serves the Mini App, its API and the Telegram webhook."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import initdata, assets
from .config import Product, Settings, load_products
from .shop import ShopError, StarsShop, process_update
from .store import Store
from .telegram import BotAPI, TelegramError

log = logging.getLogger("stars_shop")
WEBAPP_DIR = Path(__file__).resolve().parents[1] / "webapp"


def _mtime(path) -> float | None:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


class OrderRequest(BaseModel):
    product_id: str = Field(min_length=1, max_length=32)
    code: str | None = Field(None, max_length=24)


def create_app(settings: Settings | None = None, products: dict[str, Product] | None = None,
               store: Store | None = None, api=None, background: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    watch_products = products is None  # products from the file are reloaded when it changes
    products = products if products is not None else load_products(settings.products_path)
    store = store or Store(settings.db_path)
    own_api = api is None
    api = api or BotAPI(settings.bot_token)
    shop = StarsShop(api, settings, products, store)
    failures: dict[int, int] = {}
    seen = {"mtime": _mtime(settings.products_path) if watch_products else None}

    def refresh_products() -> None:
        """Pick up edits to products.json without a restart. A broken file keeps the old catalog."""
        if not watch_products:
            return
        mtime = _mtime(settings.products_path)
        if mtime == seen["mtime"]:
            return
        seen["mtime"] = mtime
        try:
            shop.products = load_products(settings.products_path)
            log.info("Ürünler yeniden yüklendi: %s ürün", len(shop.products))
        except Exception as exc:  # noqa: BLE001
            log.error("products.json okunamadı, eski katalog kullanılıyor: %s", exc)

    async def redeliver_loop():
        while True:
            try:
                refresh_products()
                await shop.tick()
            except Exception as exc:  # noqa: BLE001
                log.error("Yeniden teslim döngüsü hatası: %s", type(exc).__name__)
            await asyncio.sleep(60)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = asyncio.create_task(redeliver_loop()) if background else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if own_api:
                await api.close()

    app = FastAPI(title=settings.shop_name, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.shop = shop

    def user_id(init_data: str | None) -> int:
        try:
            user = initdata.validate(init_data or "", settings.bot_token, settings.init_data_max_age)
        except initdata.InvalidInitData:
            raise HTTPException(401, "Telegram oturumu doğrulanamadı. Mağazayı bot üzerinden yeniden aç.") from None
        store.touch_contact(user)  # opening the shop counts as a visit in the CRM
        return user["id"]

    @app.exception_handler(ShopError)
    async def shop_error(_request: Request, exc: ShopError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/api/config")
    async def config(x_telegram_init_data: str | None = Header(None)):
        body = {"shop_name": settings.shop_name, "sales_enabled": settings.sales_enabled,
                "support": settings.support_text, "terms": settings.terms_text,
                "bot_username": settings.bot_username, "contact_url": settings.contact_url,
                "checkout_urls": dict(settings.checkout_urls)}
        if settings.buy_url:
            body["buy"] = {"text": settings.buy_text or "Sen de Stars mağazası aç", "url": settings.buy_url}
        if settings.admin_url and x_telegram_init_data:
            try:
                viewer = initdata.validate(x_telegram_init_data, settings.bot_token, settings.init_data_max_age)["id"]
            except initdata.InvalidInitData:
                viewer = None
            if viewer in settings.admin_ids:  # only admins see the way to the admin screen
                body["admin_url"] = settings.admin_url
                if settings.buy_url:
                    body["license_url"] = settings.buy_url  # the license link shows the active license or sells one
        return body

    @app.get("/api/products")
    async def list_products():
        refresh_products()
        return shop.catalog()

    @app.get("/api/quote")
    async def quote(product_id: str, code: str | None = None):
        refresh_products()
        return shop.quote(product_id, code)

    @app.post("/api/orders")
    async def create_order(body: OrderRequest, x_telegram_init_data: str | None = Header(None)):
        user = user_id(x_telegram_init_data)
        refresh_products()
        try:
            return await shop.create_invoice(user, body.product_id, body.code)
        except TelegramError as exc:
            log.error("Fatura oluşturulamadı: %s", exc.description)
            raise HTTPException(502, "Fatura oluşturulamadı, biraz sonra tekrar dene.") from None

    @app.get("/api/orders/{order_id}")
    async def get_order(order_id: str, x_telegram_init_data: str | None = Header(None)):
        return shop.order_status(user_id(x_telegram_init_data), order_id)

    @app.get("/api/purchases")
    async def purchases(x_telegram_init_data: str | None = Header(None)):
        return shop.purchases(user_id(x_telegram_init_data))

    @app.post("/telegram/webhook")
    async def webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(None)):
        if not hmac.compare_digest(x_telegram_bot_api_secret_token or "", settings.webhook_secret):
            raise HTTPException(403)
        update = await request.json()
        refresh_products()
        try:
            await process_update(shop, update, failures)
        except Exception:
            # Non-2xx makes Telegram redeliver the same update later.
            raise HTTPException(500) from None
        return {"ok": True}

    @app.get("/")
    async def site():
        return FileResponse(WEBAPP_DIR / "site.html", headers={"Cache-Control": "no-cache"})

    @app.get("/app")
    async def index():
        return assets.page(WEBAPP_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=WEBAPP_DIR), name="static")
    return app


def app() -> FastAPI:  # factory for `uvicorn app.main:app --factory`
    logging.basicConfig(level=logging.INFO)
    return create_app()
