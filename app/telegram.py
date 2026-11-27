"""Minimal Bot API client. Never puts the token in logs or exception text."""
from __future__ import annotations

import json
import logging

import httpx

# httpx logs every request URL at INFO, and a Bot API URL contains the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class TelegramError(Exception):
    def __init__(self, method: str, code: int | None, description: str, retry_after: int = 0):
        super().__init__(f"Telegram {method} başarısız ({code}): {description}")
        self.method, self.code, self.description, self.retry_after = method, code, description, retry_after


class BotAPI:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None,
                 base_url: str = "https://api.telegram.org"):
        self._url = f"{base_url}/bot{token}/"
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(40.0))

    async def __call__(self, method: str, files: dict[str, tuple[str, bytes]] | None = None, **payload):
        """files={"document": ("satis.csv", b"...")} uploads as multipart; other fields go as form values."""
        try:
            if files:
                form = {k: v if isinstance(v, str) else json.dumps(v) for k, v in payload.items() if v is not None}
                response = await self._client.post(self._url + method, data=form,
                                                   files={k: (name, body) for k, (name, body) in files.items()})
            else:
                response = await self._client.post(self._url + method, json=payload)
        except httpx.HTTPError as exc:
            # str(exc) may contain the URL, which contains the token.
            raise TelegramError(method, None, type(exc).__name__) from None
        try:
            data = response.json()
        except ValueError:
            raise TelegramError(method, response.status_code, "geçersiz yanıt") from None
        if not data.get("ok"):
            raise TelegramError(method, data.get("error_code"), data.get("description", ""),
                                data.get("parameters", {}).get("retry_after", 0))
        return data["result"]

    async def close(self) -> None:
        await self._client.aclose()
