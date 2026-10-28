"""Ephemeral loopback preview leases. No API credentials, file browsing or process execution."""
from __future__ import annotations

import asyncio
import html
import mimetypes
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

PREVIEW_EXTENSIONS = {".html", ".htm", ".css", ".js", ".mjs", ".json", ".txt", ".svg", ".png", ".jpg",
                      ".jpeg", ".gif", ".webp", ".woff", ".woff2", ".ttf"}
ACTIVE_EXTENSIONS = {".html", ".htm", ".svg"}
INTERACTIVE_CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; font-src 'self' data:; connect-src 'none'; frame-src 'none'; "
       "object-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'; sandbox allow-scripts")
STATIC_CSP = INTERACTIVE_CSP.replace("script-src 'self' 'unsafe-inline'", "script-src 'none'").replace("sandbox allow-scripts", "sandbox")
WRAPPER_CSP = ("default-src 'none'; style-src 'unsafe-inline'; frame-src 'self'; connect-src 'none'; "
               "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class PreviewManager:
    def __init__(self, *, ttl_seconds=3600):
        self.ttl_seconds, self._leases = ttl_seconds, {}
        self._lock = threading.RLock()
        self._server, self._thread, self._closed = None, None, False
        self._connections = set()
        self._handlers = set()

    def wrapped(self, raw: str, session_id: str) -> bool:
        url = urlsplit(raw)
        with self._lock:
            if not self._server or url.hostname != "127.0.0.1" or url.port != self._server.server_port:
                return False
            pieces = unquote(url.path).strip("/").split("/", 1)
            lease = self._leases.get(pieces[0])
            return bool(len(pieces) == 2 and lease and lease["session_id"] == session_id
                        and lease["expires_at"] > time.monotonic() and Path(pieces[1]).suffix.lower() in ACTIVE_EXTENSIONS)

    def register(self, path: Path, session_id: str, *, root: Path | None = None, interactive=False) -> str:
        path = Path(path).resolve(strict=True)
        root = Path(root).resolve(strict=True) if root is not None else None
        if not path.is_file() or path.suffix.lower() not in PREVIEW_EXTENSIONS:
            raise ValueError("File type is unsupported for local preview")
        if root and (not root.is_dir() or not path.is_relative_to(root)):
            raise PermissionError("Preview entry must stay inside its explicitly registered asset root")
        with self._lock:
            if self._closed:
                raise RuntimeError("Preview manager is closed")
            self._start()
            token = secrets.token_urlsafe(32)
            relative = path.relative_to(root).as_posix() if root else path.name
            self._leases[token] = {"path": path, "root": root, "entry": relative, "session_id": session_id,
                                   "expires_at": time.monotonic() + self.ttl_seconds, "interactive": bool(interactive)}
            return f"http://127.0.0.1:{self._server.server_port}/{token}/{quote(relative)}"

    def rewrite_file_url(self, raw: str, session_id: str) -> str:
        parsed = urlsplit(raw)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise PermissionError("Only a registered local file can be previewed")
        decoded = unquote(parsed.path)
        if len(decoded) > 3 and decoded[0] == "/" and decoded[2] == ":":
            decoded = decoded[1:]
        path = Path(decoded).resolve(strict=True)
        with self._lock:
            for token, lease in self._leases.items():
                if lease["session_id"] != session_id or lease["expires_at"] <= time.monotonic():
                    continue
                if path == lease["path"]:
                    relative = lease["entry"]
                elif lease["root"] and path.is_relative_to(lease["root"]) and path.is_file():
                    relative = path.relative_to(lease["root"]).as_posix()
                else:
                    continue
                return f"http://127.0.0.1:{self._server.server_port}/{token}/{quote(relative)}"
        raise PermissionError("File has no active preview registration in this session")

    def close_session(self, session_id: str):
        with self._lock:
            self._leases = {token: lease for token, lease in self._leases.items() if lease["session_id"] != session_id}

    def _start(self):
        if self._server:
            return
        manager = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                # A loopback preview GET has no streamed request body. Bound a
                # partial header read below close()'s handler join budget: Windows
                # shutdown() alone does not reliably wake the socket timeout wait.
                self.connection.settimeout(1)
                with manager._lock:
                    manager._connections.add(self.connection)
                    manager._handlers.add(threading.current_thread())
                    if manager._closed:
                        try:
                            self.connection.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass

            def handle(self):
                try:
                    super().handle()
                except OSError:
                    # A disconnected client or owned shutdown is not a server error.
                    pass

            def finish(self):
                try:
                    try:
                        super().finish()
                    except OSError:
                        pass
                finally:
                    with manager._lock:
                        manager._connections.discard(self.connection)
                        manager._handlers.discard(threading.current_thread())

            def do_GET(self):
                self._serve(False)

            def do_HEAD(self):
                self._serve(True)

            def _serve(self, head):
                expected_host = f"127.0.0.1:{self.server.server_port}"
                if self.headers.get("Host") != expected_host:
                    self.send_error(403)
                    return
                pieces = unquote(urlsplit(self.path).path).lstrip("/").split("/", 1)
                if len(pieces) != 2:
                    self.send_error(404)
                    return
                token, relative = pieces
                inner = relative.startswith(".aster-content/")
                if inner:
                    relative = relative[len(".aster-content/"):]
                parts = PurePosixPath(relative)
                if parts.is_absolute() or ".." in parts.parts or "\\" in relative or ":" in relative:
                    self.send_error(404)
                    return
                with manager._lock:
                    lease = manager._leases.get(token)
                    if not lease or lease["expires_at"] <= time.monotonic():
                        manager._leases.pop(token, None)
                        self.send_error(404)
                        return
                    try:
                        if lease["root"]:
                            root = lease["root"].resolve(strict=True)
                            path = root.joinpath(*parts.parts).resolve(strict=True)
                            if not path.is_relative_to(root) or root != lease["root"]:
                                raise PermissionError()
                        else:
                            if relative != lease["entry"]:
                                raise PermissionError()
                            path = lease["path"].resolve(strict=True)
                            if path != lease["path"]:
                                raise PermissionError()
                        if not path.is_file() or path.suffix.lower() not in PREVIEW_EXTENSIONS or path.stat().st_size > 25 * 1024 * 1024:
                            raise PermissionError()
                        raw = path.read_bytes()
                    except (OSError, ValueError, PermissionError):
                        self.send_error(404)
                        return
                mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                policy = INTERACTIVE_CSP if lease["interactive"] else STATIC_CSP
                if path.suffix.lower() in ACTIVE_EXTENSIONS:
                    if inner:
                        # Raw active documents are accepted only as the same-origin
                        # iframe navigation of our trusted wrapper, never top-level.
                        if self.headers.get("Sec-Fetch-Dest") != "iframe" or self.headers.get("Sec-Fetch-Site") != "same-origin":
                            self.send_error(403)
                            return
                    elif path.suffix.lower() == ".svg" and self.headers.get("Sec-Fetch-Dest") == "image":
                        pass  # Browser SVG image decoding does not execute scripts.
                    else:
                        source = f"/{token}/.aster-content/{quote(relative)}"
                        sandbox = "allow-scripts" if lease["interactive"] else ""
                        raw = ("<!doctype html><meta charset=utf-8><title>" + html.escape(path.name) + "</title>"
                               "<style>html,body{margin:0;width:100%;height:100%;overflow:hidden}iframe{width:100%;height:100%;border:0}</style>"
                               f'<iframe data-aster-preview sandbox="{sandbox}" referrerpolicy="no-referrer" src="'
                               + html.escape(source, quote=True) + '"></iframe>').encode()
                        mime, policy = "text/html; charset=utf-8", WRAPPER_CSP
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Content-Security-Policy", policy)
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-DNS-Prefetch-Control", "off")
                self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), usb=(), serial=(), "
                                 "hid=(), clipboard-read=(), clipboard-write=(), display-capture=(), payment=()")
                self.end_headers()
                if not head:
                    try:
                        self.wfile.write(raw)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="aster-preview")
        self._thread.start()

    async def close(self):
        with self._lock:
            self._closed, self._leases = True, {}
            server, thread = self._server, self._thread
            connections = list(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if server:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            await asyncio.to_thread(thread.join, 3)
        with self._lock:
            handlers = list(self._handlers)
        if handlers:
            await asyncio.gather(*(asyncio.to_thread(handler.join, 2) for handler in handlers))
        if self._handlers:
            raise RuntimeError("Preview request handlers did not stop within the shutdown budget")
        self._server = self._thread = None
