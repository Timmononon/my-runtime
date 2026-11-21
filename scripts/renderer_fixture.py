"""Load the desktop build inside a Playwright context used for workbench QA."""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit


async def prepare_desktop_context(context, origin: str, ticket: str, static_root: str | Path | None = None):
    default_root = Path(__file__).resolve().parents[1] / "src/aster/web/static"
    root = Path(static_root or os.environ.get("ASTER_WEB_STATIC_ROOT") or default_root).resolve(strict=True)
    index = root / "index.html"
    if not index.is_file() or index.is_symlink():
        raise FileNotFoundError(f"Missing desktop renderer index: {index}")
    files = {"/": index}
    asset_types = {".js", ".css", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".ico", ".woff", ".woff2"}
    for path in (root / "assets").rglob("*"):
        if path.suffix in asset_types and path.is_file() and path.resolve() == path:
            files["/" + path.relative_to(root).as_posix()] = path

    async def read_asset(route):
        request = route.request
        if request.method not in {"GET", "HEAD"}:
            await route.fallback()
            return
        path = files.get(unquote(urlsplit(request.url).path))
        if path is None:
            await route.fulfill(status=404)
            return
        # Windows can map .js to a machine-specific MIME via its registry.
        content_type = "text/javascript" if path.suffix == ".js" else None
        await route.fulfill(path=path, content_type=content_type, headers={"X-Content-Type-Options": "nosniff"})

    # Intercept local renderer files only. API traffic, including SSE, goes
    # directly to the real Runtime, and artifact previews use their own origin.
    await context.route(re.compile(rf"^{re.escape(origin)}/(?:assets/[^?#]*)?(?:[?#].*)?$"), read_asset)
    # Mirror Electron's main process: establish the HttpOnly cookie in this
    # context before navigating to the workbench, including after a reload.
    exchange = await context.request.post(f"{origin}/ui/auth", data={"ticket": ticket},
                                          headers={"Origin": origin, "X-Aster-UI": "1"})
    assert exchange.status == 200 and (await exchange.json())["authenticated"] is True
