"""FastAPI app: serves the Mini App, its API and the Telegram webhook."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import initdata
from .config import Product, Settings, load_products
from .shop import ShopError, StarsShop, process_update
from .store import Store
from .telegram import BotAPI, TelegramError

log = logging.getLogger("stars_shop")
WEBAPP_DIR = Path(__file__).resolve().parents[1] / "webapp"


class OrderRequest(BaseModel):
    product_id: str = Field(min_length=1, max_length=32)


def create_app(settings: Settings | None = None, products: dict[str, Product] | None = None,
               store: Store | None = None, api=None, background: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    products = products if products is not None else load_products(settings.products_path)
    store = store or Store(settings.db_path)
    own_api = api is None
    api = api or BotAPI(settings.bot_token)
    shop = StarsShop(api, settings, products, store)
    failures: dict[int, int] = {}

    async def redeliver_loop():
        while True:
            try:
                await shop.redeliver()
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
            return initdata.validate(init_data or "", settings.bot_token, settings.init_data_max_age)["id"]
        except initdata.InvalidInitData:
            raise HTTPException(401, "Telegram oturumu doğrulanamadı. Mağazayı bot üzerinden yeniden aç.") from None

    @app.exception_handler(ShopError)
    async def shop_error(_request: Request, exc: ShopError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/api/config")
    async def config():
        return {"shop_name": settings.shop_name, "sales_enabled": settings.sales_enabled,
                "support": settings.support_text, "terms": settings.terms_text,
                "bot_username": settings.bot_username, "contact_url": settings.contact_url}

    @app.get("/api/products")
    async def list_products():
        return shop.catalog()

    @app.post("/api/orders")
    async def create_order(body: OrderRequest, x_telegram_init_data: str | None = Header(None)):
        user = user_id(x_telegram_init_data)
        try:
            return await shop.create_invoice(user, body.product_id)
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
        return FileResponse(WEBAPP_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=WEBAPP_DIR), name="static")
    return app


def app() -> FastAPI:  # factory for `uvicorn app.main:app --factory`
    logging.basicConfig(level=logging.INFO)
    return create_app()
