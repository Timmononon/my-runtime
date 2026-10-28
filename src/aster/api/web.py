"""Loopback workbench assets and short-lived, in-memory browser authentication."""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from aster.config import load_config

COOKIE_NAME = "aster_ui"
UI_HEADER = "x-aster-ui"
STATIC_ROOT = Path(__file__).resolve().parent.parent / "web" / "static"
STATIC_SUFFIXES = {".js", ".css", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".ico", ".woff", ".woff2"}
UI_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
          "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; "
          "object-src 'none'; frame-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


@dataclass(frozen=True)
class BrowserGrant:
    origin: str
    expires_at: float
    cwd: str | None


class UIAuth:
    """Only hashes are retained; restart/logout/expiry revoke browser grants.

    Caps bound memory for repeated local launcher invocations; a full live set
    returns 429 instead of evicting another active UI. Tests may set smaller
    limits; the constructor is the explicit override point for host integrators.
    """

    def __init__(self, *, clock=time.monotonic, ticket_ttl=60, session_ttl=8 * 3600,
                 max_tickets=128, max_sessions=64):
        if not 0 < ticket_ttl <= 60 or not 0 < session_ttl <= 8 * 3600 or min(max_tickets, max_sessions) < 1:
            raise ValueError("UI authentication limits must be positive and bounded")
        self.clock, self.ticket_ttl, self.session_ttl = clock, ticket_ttl, session_ttl
        self.max_tickets, self.max_sessions = max_tickets, max_sessions
        self._tickets: dict[str, BrowserGrant] = {}
        self._sessions: dict[str, BrowserGrant] = {}

    @staticmethod
    def _key(value):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _prune(self):
        now = self.clock()
        for entries in (self._tickets, self._sessions):
            for key, grant in list(entries.items()):
                if grant.expires_at <= now:
                    del entries[key]

    def mint(self, origin, cwd=None):
        self._prune()
        if len(self._tickets) >= self.max_tickets:
            raise HTTPException(429, "Too many pending browser logins; wait for tickets to expire")
        ticket = secrets.token_urlsafe(32)
        self._tickets[self._key(ticket)] = BrowserGrant(origin, self.clock() + self.ticket_ttl, cwd)
        return ticket

    def redeem(self, ticket, origin):
        self._prune()
        key = self._key(ticket)
        grant = self._tickets.get(key)
        if grant is None or grant.origin != origin:
            raise HTTPException(401, "Login ticket is invalid or expired; run aster web again")
        if len(self._sessions) >= self.max_sessions:
            raise HTTPException(429, "Too many active browser logins; sign out or wait for expiry")
        # No await separates lookup/consumption: concurrent redeems have one winner.
        del self._tickets[key]
        credential = secrets.token_urlsafe(32)
        self._sessions[self._key(credential)] = BrowserGrant(origin, self.clock() + self.session_ttl, grant.cwd)
        return credential

    def authenticate(self, credential, origin):
        self._prune()
        if not credential or len(credential) > 256:
            return None
        grant = self._sessions.get(self._key(credential))
        return grant if grant and grant.origin == origin else None

    def revoke(self, credential):
        if credential:
            self._sessions.pop(self._key(credential), None)

    def clear(self):
        self._tickets.clear()
        self._sessions.clear()


def request_origin(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}"


def public_web_request(request: Request) -> bool:
    path = request.url.path
    return ((request.method in {"GET", "HEAD"} and (path == "/" or path.startswith("/assets/")))
            or request.method == "POST" and path == "/ui/auth")


class TicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cwd: str | None = Field(default=None, max_length=4096)


class TicketExchange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket: str = Field(min_length=32, max_length=256)


PERMANENT_DELETE_CAPABILITIES = ("session_permanent_delete_v1", "project_permanent_delete_v1")


def create_web_router(coordinator, authentication: UIAuth, static_root: Path | None = None):
    router = APIRouter(tags=["workbench"])
    root = Path(static_root or STATIC_ROOT).resolve()

    @router.api_route("/", methods=["GET", "HEAD"])
    async def index():
        target = root / "index.html"
        if not target.is_file() or target.is_symlink() or target.resolve().parent != root:
            raise HTTPException(503, "Workbench assets are missing; install a built Aster distribution")
        return FileResponse(target, media_type="text/html", headers={"Cache-Control": "no-store"})

    @router.api_route("/assets/{asset_path:path}", methods=["GET", "HEAD"])
    async def asset(asset_path: str):
        # Compiled assets only; no SPA fallback, source maps, dotfiles or arbitrary
        # package files. Resolve both root and target to reject symlink escapes.
        parts = PurePosixPath(asset_path).parts
        if (not parts or "\\" in asset_path or ":" in asset_path
                or any(part in {".", ".."} or part.startswith(".") for part in parts)
                or Path(asset_path).suffix.lower() not in STATIC_SUFFIXES):
            raise HTTPException(404, "Asset not found")
        base = (root / "assets").resolve()
        target = (base / asset_path).resolve()
        if not base.is_relative_to(root) or not target.is_relative_to(base) or not target.is_file():
            raise HTTPException(404, "Asset not found")
        return FileResponse(target, headers={"Cache-Control": "public, max-age=3600"})

    @router.post("/ui/tickets", status_code=201)
    async def ticket(body: TicketRequest, request: Request):
        if request.state.auth_method != "bearer":
            raise HTTPException(403, "Only the local authenticated launcher can create login tickets")
        cwd = None
        if body.cwd is not None:
            path = Path(body.cwd).expanduser().resolve(strict=True)
            if not path.is_dir():
                raise ValueError("Workbench cwd must be an existing directory")
            cwd = str(path)
        value = authentication.mint(request_origin(request), cwd)
        return {"ticket": value, "expires_in": authentication.ticket_ttl}

    @router.post("/ui/auth")
    async def exchange(body: TicketExchange, request: Request):
        credential = authentication.redeem(body.ticket, request_origin(request))
        authentication.revoke(request.cookies.get(COOKIE_NAME))
        response = JSONResponse({"authenticated": True, "expires_in": authentication.session_ttl})
        response.set_cookie(COOKIE_NAME, credential, max_age=int(authentication.session_ttl),
                            httponly=True, samesite="strict", path="/", secure=request.url.scheme == "https")
        return response

    @router.get("/ui/bootstrap")
    async def bootstrap(request: Request):
        grant = getattr(request.state, "ui_grant", None)
        settings = load_config(coordinator.data_dir, cwd=Path(grant.cwd) if grant and grant.cwd else None)
        from aster.providers.profiles import PROTOCOLS
        return {"cwd": str(settings.cwd), "model": settings.get("model"),
                "provider_configured": coordinator.configured(), "instance_id": coordinator.instance_id,
                "schema_version": 1, "ui_version": 1, "providers": coordinator.providers.list(),
                "protocols": PROTOCOLS, "selection": coordinator.default_selection(settings.data),
                "capabilities": list(PERMANENT_DELETE_CAPABILITIES)}

    @router.post("/ui/logout")
    async def logout(request: Request):
        authentication.revoke(request.cookies.get(COOKIE_NAME))
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict",
                               secure=request.url.scheme == "https")
        return response

    return router
