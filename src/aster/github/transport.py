"""Fixed-origin bounded GitHub HTTP without redirects or implicit retries."""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import httpx

from aster.tools.http import pin_url

ORIGIN = "https://api.github.com"
API_VERSION = "2026-03-10"


class GithubHTTPError(RuntimeError):
    def __init__(self, category, status=None, headers=None):
        super().__init__("GitHub request failed (" + category + ")")
        self.category, self.status, self.headers = category, status, headers or {}


class GithubTransport(httpx.AsyncBaseTransport):
    def __init__(self, *, dns_fallback="google"):
        self.inner = httpx.AsyncHTTPTransport(retries=0)
        self.dns_fallback = dns_fallback

    async def handle_async_request(self, request):
        if request.url.scheme != "https" or request.url.host != "api.github.com" or request.url.port not in {None, 443}:
            raise GithubHTTPError("origin")
        pinned_url, host, sni = await pin_url(str(request.url), set(), dns_fallback=self.dns_fallback)
        # DNS verification yields, so revalidate source authority before destination dispatch.
        # A source revoked during resolution must not send its captured request.
        guard = request.extensions.get("aster.github.guard")
        if guard:
            guard()
        headers = request.headers.copy()
        headers["Host"] = host
        pinned = httpx.Request(request.method, pinned_url, headers=headers, stream=request.stream,
                               extensions={**request.extensions, "sni_hostname": sni})
        return await self.inner.handle_async_request(pinned)

    async def aclose(self):
        await self.inner.aclose()


async def bounded_request(client, method, path, *, headers, params=None, payload=None, guard=None, max_bytes=2_000_000):
    if not path.startswith("/") or path.startswith("//") or "\\" in path or "?" in path or "#" in path:
        raise GithubHTTPError("path")
    if guard:
        guard()
    request = client.build_request(method, ORIGIN + path, headers=headers, params=params, json=payload,
                                   extensions={"aster.github.guard": guard} if guard else {})
    response = await client.send(request, stream=True, follow_redirects=False)
    try:
        data = bytearray()
        async for chunk in response.aiter_bytes():
            if len(data) + len(chunk) > max_bytes:
                raise GithubHTTPError("response_too_large", response.status_code, dict(response.headers))
            data.extend(chunk)
        if response.status_code == 304:
            return 304, None, dict(response.headers)
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError, RecursionError):
            raise GithubHTTPError("invalid_json", response.status_code, dict(response.headers)) from None
        if response.status_code not in {200, 201, 202}:
            raise GithubHTTPError("rate_limited" if response.status_code == 429 or response.headers.get("x-ratelimit-remaining") == "0"
                                  else "rejected", response.status_code, dict(response.headers))
        return response.status_code, value, dict(response.headers)
    finally:
        await response.aclose()


def next_page(headers, path):
    for link in headers.get("link", "").split(","):
        if 'rel="next"' not in link:
            continue
        raw = link.strip().split(">", 1)[0].removeprefix("<")
        url = urlsplit(raw)
        # A pagination link may advance the page but cannot change the endpoint authority.
        # Require the same API path before accepting any continuation page number.
        if url.scheme != "https" or url.netloc != "api.github.com" or url.path != path or url.fragment:
            raise GithubHTTPError("pagination_origin")
        values = parse_qs(url.query).get("page", [])
        if len(values) != 1 or not values[0].isdigit() or not 1 <= int(values[0]) <= 1_000_000:
            raise GithubHTTPError("pagination_page")
        return int(values[0])
    return None
