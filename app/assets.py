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
