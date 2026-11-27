"""HTML pages with cache-busted asset URLs.

Telegram's in-app browser keeps /static/app.js and style.css cached across opens, so
after an update buyers could see the new page with the old script. Every asset URL in
the HTML gets ?v=<content hash>, and the HTML itself is never cached."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from fastapi.responses import HTMLResponse

ASSET = re.compile(r'((?:src|href)="(/(?:panel/)?static/)([^"?]+))"')
WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"
PANEL_DIR = Path(__file__).resolve().parent.parent / "panel"
DIRS = {"/static/": WEBAPP_DIR, "/panel/static/": PANEL_DIR}


def _version(directory: Path, name: str) -> str:
    try:
        return hashlib.sha256((directory / name).read_bytes()).hexdigest()[:10]
    except OSError:
        return "0"


def page(path: Path) -> HTMLResponse:
    html = path.read_text(encoding="utf-8")
    html = ASSET.sub(lambda m: f'{m.group(1)}?v={_version(DIRS[m.group(2)], m.group(3))}"', html)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


def image_type(data: bytes) -> str:
    """Media type of a picture from its first bytes (Telegram photos are JPEG; documents may be PNG/WebP)."""
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:3] == b"GIF":
        return "image/gif"
    return "image/jpeg"
