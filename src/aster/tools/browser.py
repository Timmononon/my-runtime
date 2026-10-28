from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from jsonschema import validate

from aster.domain import ToolContext, ToolResult, ToolSpec
from aster.platform.filesystem import FileLock, scoped_path
from aster.providers.images import prepare_model_image
from aster.tools.files import TEXT, artifact, bounded_result, failure, schema


@dataclass
class PageState:
    page: object
    context_id: str
    scope: str
    version: int = 0
    refs: dict = field(default_factory=dict)
    bridge: bool = False
    authorized_url: str | None = None
    lease_id: str | None = None


class BrowserManager:
    """One owner task serializes browser commands; API handles never cross threads."""

    def __init__(self, profile_root: Path, *, headless=True):
        self.profile_root, self.headless = Path(profile_root), headless
        self._queue = asyncio.Queue()
        self._owner = None
        self._loop = None
        self._playwright = None
        self._contexts = {}
        self._pages: dict[str, PageState] = {}
        self._active_response = None
        self._closed = False
        self._bridges = {}
        self._leases = {}
        self._paused = set()
        self._headed = set()
        self._protected_ui = set()

    def protect_ui_origins(self, origins):
        for origin in origins:
            parsed = urlsplit(origin)
            host = "loopback" if parsed.hostname in {"localhost", "127.0.0.1", "::1"} else parsed.hostname
            self._protected_ui.add((parsed.scheme, host, parsed.port or (443 if parsed.scheme == "https" else 80)))

    def _protected_url(self, value):
        parsed = urlsplit(value)
        host = "loopback" if parsed.hostname in {"localhost", "127.0.0.1", "::1"} else parsed.hostname
        return parsed.path in {"", "/", "/index.html"} and (parsed.scheme, host, parsed.port or (443 if parsed.scheme == "https" else 80)) in self._protected_ui

    def _guard_page(self, page):
        if self._protected_url(page.url) or any(self._protected_url(frame.url) for frame in page.frames):
            raise PermissionError("Runtime workbench approval and browser control surfaces cannot be operated or observed by browser tools")

    def _config(self, context):
        from aster.customization.management import validate_browser
        loader = context.services.get("browser_config_loader")
        return validate_browser(loader() if loader else context.services.get("browser_config", {}))

    def status(self, session_id, config):
        rows = []
        for page_id, state in self._pages.items():
            if state.scope != session_id or state.page.is_closed():
                continue
            allowed = not state.bridge or (config.get("mode") == "bridge" and state.page.url in config.get("allowed_pages", []))
            rows.append({"page_id": page_id, "context_id": state.context_id,
                         "url": state.page.url if allowed else state.authorized_url, "available": allowed,
                         "ownership": "user" if state.bridge else "runtime"})
        return {"mode": config.get("mode", "isolated"), "paused": session_id in self._paused,
                "pages": rows, "attached": bool(rows), "bridge_configured": bool(config.get("cdp_endpoint")),
                "headed": bool(any(state.context_id in self._headed for state in self._pages.values() if state.scope == session_id))}

    async def call(self, action: str, arguments: dict, context: ToolContext) -> ToolResult:
        if self._closed:
            return ToolResult("Browser manager is closed", True, "browser")
        loop = asyncio.get_running_loop()
        if self._loop and self._loop is not loop:
            raise RuntimeError("BrowserManager must be used from its owning event loop")
        if self._owner is None:
            self._loop = loop
            self._owner = asyncio.create_task(self._serve(), name="aster-browser-owner")
        response = loop.create_future()
        await self._queue.put((action, arguments, context, response))
        cancelled = asyncio.create_task(context.cancel.wait())
        try:
            done, _ = await asyncio.wait({response, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done and not response.done() and self._active_response is not response:
                response.set_result(ToolResult("Cancelled while queued; no browser action dispatched", True, "cancelled"))
            return await asyncio.shield(response)
        except asyncio.CancelledError:
            context.cancel.set()
            if not response.done() and self._active_response is not response:
                response.set_result(ToolResult("Cancelled while queued; no browser action dispatched", True, "cancelled"))
            return await asyncio.shield(response)
        finally:
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)

    async def _serve(self):
        while True:
            command = await self._queue.get()
            if command is None:
                break
            action, arguments, context, response = command
            if response.done():
                continue
            if context.cancel.is_set():
                response.set_result(ToolResult("Cancelled before browser action", True, "cancelled"))
                continue
            self._active_response = response
            operation = asyncio.create_task(self._perform(action, arguments, context))
            cancelled = asyncio.create_task(context.cancel.wait())
            try:
                done, _ = await asyncio.wait({operation, cancelled}, timeout=arguments.get("timeout", 30) + 5,
                                            return_when=asyncio.FIRST_COMPLETED)
                if operation in done:
                    result = operation.result()
                else:
                    # Never cancel a live Playwright call. Closing the owning
                    # context makes the protocol action settle with an error.
                    await self._close_scope(self._scope(context))
                    finished, _ = await asyncio.wait({operation}, timeout=10)
                    if operation not in finished:
                        self._closed = True
                        operation.add_done_callback(lambda item: item.exception() if not item.cancelled() else None)
                    else:
                        operation.exception()  # retrieve the expected closed-context error
                        await self._close_scope(self._scope(context))
                    result = ToolResult("Browser action cancelled or timed out; inspect effects before retrying",
                                        True, "cancelled" if context.cancel.is_set() else "timeout",
                                        "unknown" if action in MUTATING else "known")
                if not response.done():
                    response.set_result(result)
            except Exception as exc:  # noqa: BLE001 - isolate one browser command from subsequent commands
                if not response.done():
                    unknown_navigation = action == "navigate" and self._config(context)["mode"] == "bridge"
                    response.set_result(ToolResult(f"Browser action failed: {type(exc).__name__}: {exc}", True,
                                                  "browser", "unknown" if (action in MUTATING or unknown_navigation) and
                                                  not isinstance(exc, (ValueError, PermissionError)) else "known"))
            finally:
                self._active_response = None
                cancelled.cancel()
                await asyncio.gather(cancelled, return_exceptions=True)

    def _scope(self, context):
        # Explicit configured identities can share a profile; model-supplied
        # profile names cannot cross session identity boundaries by themselves.
        return str(context.services.get("browser_identity", context.session_id))

    def _state(self, page_id, context):
        state = self._pages.get(page_id)
        if state is None or state.scope != self._scope(context):
            raise PermissionError("Unknown page or page belongs to another browser identity")
        if state.bridge:
            config = self._config(context)
            if config["mode"] != "bridge" or state.page.url not in config["allowed_pages"]:
                raise PermissionError("User browser page is outside the current exact URL allowlist")
        self._guard_page(state.page)
        return state

    def _new_page(self, page, context_id, scope, *, bridge=False, authorized_url=None, lease_id=None):
        identity = uuid.uuid4().hex
        state = PageState(page, context_id, scope, bridge=bridge, authorized_url=authorized_url, lease_id=lease_id)
        self._pages[identity] = state

        def navigated(frame):
            if frame == page.main_frame:
                state.version += 1

        page.on("framenavigated", navigated)
        return identity

    async def _perform(self, action, args, ctx):
        scope = self._scope(ctx)
        if action == "close":
            await self._close_scope(scope)
            return ToolResult("Runtime-owned contexts closed and user browser connections detached for this identity")
        config = self._config(ctx)
        if scope in self._paused and (action in AUTOMATION_MUTATING and not (action == "tabs" and args.get("operation", "list") == "list")):
            raise PermissionError("Browser automation is paused for user control; only an explicit operator resume can continue")
        pause_loader = ctx.services.get("browser_pause_loader")
        if (pause_loader() if pause_loader else ctx.services.get("browser_paused")):
            self._paused.add(scope)
            if action in AUTOMATION_MUTATING and not (action == "tabs" and args.get("operation", "list") == "list"):
                raise PermissionError("Browser automation remains paused after recovery; explicit operator resume is required")
        if action == "handoff":
            if not ctx.services.get("browser_operator"):
                raise PermissionError("Pause/resume requires an explicitly approved operator browser control")
            if args["operation"] == "pause":
                self._paused.add(scope)
            elif args["operation"] == "resume":
                self._paused.discard(scope)
            else:
                raise ValueError("Handoff operation must be pause or resume")
            result = self.status(scope, config)
            return ToolResult("Browser automation paused for user control" if scope in self._paused else "Browser automation explicitly resumed", data=result)
        if action == "attach":
            return await self._attach(args, ctx, config)
        if config["mode"] == "bridge" and action == "open":
            raise PermissionError("Bridge mode requires explicit browser_attach of an authorized existing page")
        if action == "open":
            profile = args.get("profile_id", "default")
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", profile):
                raise ValueError("profile_id must use 1–64 letters, digits, underscores or hyphens")
            url = args.get("url")
            if url is not None and self._protected_url(url):
                raise PermissionError("Browser tools cannot navigate to the Runtime workbench approval/control surface")
            if url is not None and urlsplit(url).scheme not in {"https", "http"}:
                raise ValueError("Only HTTP(S) navigation is allowed")
            scope = self._scope(ctx)
            key = hashlib.sha256(f"{scope}\0{profile}".encode()).hexdigest()[:24]
            if key not in self._contexts:
                if self._playwright is None:
                    from playwright.async_api import async_playwright
                    self._playwright = await async_playwright().start()
                lock = FileLock(self.profile_root / f"{key}.lock", timeout=0.1)
                await asyncio.to_thread(lock.acquire)
                try:
                    browser_context = await self._playwright.chromium.launch_persistent_context(
                        str(self.profile_root / key), headless=config.get("headless", self.headless), accept_downloads=True,
                        timeout=30_000)
                except BaseException:
                    lock.release()
                    raise
                self._contexts[key] = (browser_context, lock, scope, profile)
                if not config.get("headless", self.headless):
                    self._headed.add(key)
                browser_context.set_default_timeout(15_000)
                for page in browser_context.pages:
                    self._new_page(page, key, scope)
                if not browser_context.pages:
                    self._new_page(await browser_context.new_page(), key, scope)
            response = None
            if url is not None:
                target = next(state.page for state in self._pages.values()
                              if state.context_id == key and not state.page.is_closed())
                response = await target.goto(url, wait_until="domcontentloaded",
                                             timeout=args.get("timeout", 30) * 1000)
            pages = [{"page_id": identity, "url": state.page.url} for identity, state in self._pages.items()
                     if state.context_id == key and not state.page.is_closed()]
            data = {"context_id": key, "profile_id": profile, "pages": pages}
            if url is not None:
                data.update(url=target.url, status=response.status if response else None)
            return ToolResult(json.dumps(data), data=data)
        if action == "tabs":
            if args.get("operation", "list") == "new":
                record = self._contexts.get(args.get("context_id"))
                if not record or record[2] != self._scope(ctx):
                    raise PermissionError("Unknown browser context")
                identity = self._new_page(await record[0].new_page(), args["context_id"], record[2])
                return ToolResult(f"Opened page {identity}", data={"page_id": identity})
            if args.get("operation") == "close":
                state = self._state(args.get("page_id"), ctx)
                if state.bridge:
                    raise PermissionError("User-owned browser pages cannot be closed by Aster; detach the lease instead")
                await state.page.close()
                self._pages.pop(args["page_id"])
                return ToolResult("Page closed")
            pages = [{"page_id": identity, "context_id": state.context_id, "url": state.page.url}
                     for identity, state in self._pages.items()
                     if state.scope == self._scope(ctx) and not state.page.is_closed() and
                     (not state.bridge or config["mode"] == "bridge" and state.page.url in config["allowed_pages"])]
            return ToolResult(json.dumps(pages), data={"pages": pages})
        if action == "close":
            await self._close_scope(self._scope(ctx))
            return ToolResult("Runtime-owned contexts closed and user browser connections detached for this identity")
        state = self._state(args["page_id"], ctx)
        page = state.page
        timeout = args.get("timeout", 30) * 1000
        if action == "navigate":
            if self._protected_url(args["url"]):
                raise PermissionError("Browser tools cannot navigate to the Runtime workbench approval/control surface")
            if state.bridge and args["url"] not in config["allowed_pages"]:
                raise PermissionError("Navigation is outside the exact user-browser URL allowlist")
            if urlsplit(args["url"]).scheme not in {"https", "http"}:
                raise ValueError("Only HTTP(S) navigation is allowed")
            response = await page.goto(args["url"], wait_until="domcontentloaded", timeout=timeout)
            if state.bridge and page.url not in self._config(ctx)["allowed_pages"]:
                raise RuntimeError("User-browser navigation left the authorized page; inspect effects before continuing")
            return ToolResult(f"URL: {page.url}\nTitle: {await page.title()}\nHTTP: {response.status if response else None}",
                              data={"url": page.url, "status": response.status if response else None,
                                    "snapshot_version": state.version})
        surface = page
        preview = ctx.services.get("preview_manager")
        if preview and preview.wrapped(page.url, ctx.session_id):
            handle = await page.locator("iframe[data-aster-preview]").element_handle(timeout=timeout)
            surface = await handle.content_frame()
            if surface is None:
                raise RuntimeError("Preview content frame is unavailable")
            await surface.wait_for_load_state("domcontentloaded", timeout=timeout)
        if action in {"snapshot", "locate"}:
            for handle in state.refs.values():
                await handle.dispose()
            state.refs.clear()
            state.version += 1
            locator = surface.locator(args.get("selector", "a,button,input,textarea,select,[role=button]"))
            count = await locator.count()
            items = []
            for number in range(min(count, args.get("limit", 60))):
                handle = await locator.nth(number).element_handle(timeout=timeout)
                if not handle:
                    continue
                ref = f"e{number}"
                state.refs[ref] = handle
                details = await handle.evaluate("e => ({tag:e.tagName.toLowerCase(), text:(e.innerText||e.getAttribute('aria-label')||'').slice(0,500), type:e.type||null})")
                items.append({"ref": ref, **details})
            body = (await surface.locator("body").inner_text(timeout=timeout))[:24_000] if action == "snapshot" else ""
            self._state(args["page_id"], ctx)
            content = json.dumps({"url": page.url, "title": await page.title(), "snapshot_version": state.version,
                                  "elements": items, "body": body}, ensure_ascii=False, indent=2)
            return bounded_result(content, ctx, data={"page_id": args["page_id"], "snapshot_version": state.version,
                                                      "total_matches": count})
        if action == "screenshot":
            ctx.artifact_dir.mkdir(parents=True, exist_ok=True)
            path = ctx.artifact_dir / f"{uuid.uuid4().hex}-page.png"
            await page.screenshot(path=str(path), full_page=args.get("full_page", False), timeout=timeout)
            try:
                self._state(args["page_id"], ctx)
            except PermissionError:
                path.unlink(missing_ok=True)
                raise
            raw = await asyncio.to_thread(path.read_bytes)
            header = raw[:24]
            width, height = int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")
            item = artifact(path, ctx, "image/png", width=width, height=height)
            item["sha256"] = hashlib.sha256(raw).hexdigest()
            text = f"Web page screenshot: {path} ({width}×{height})"
            try:
                prepared = await asyncio.to_thread(prepare_model_image, raw, "image/png")
            except ValueError as error:
                return ToolResult(text + f"\n[Model image unavailable: {error}; original screenshot preserved.]",
                                  is_error=True, error_category="image", artifacts=[item])
            model_item, model_path = item, path
            items = [item]
            if prepared.changed:
                model_path = path.with_name(path.stem + "-model.jpg")
                await asyncio.to_thread(model_path.write_bytes, prepared.data)
                model_item = artifact(model_path, ctx, prepared.mime_type, width=prepared.width, height=prepared.height,
                                      source_artifact_id=item["artifact_id"], sha256=hashlib.sha256(prepared.data).hexdigest())
                items.append(model_item)
                text += f"\n[Model preview resized to {prepared.width}×{prepared.height}; original screenshot is preserved.]"
            return ToolResult(text, artifacts=items, data={"content_parts": [
                {"type": "input_text", "text": text},
                {"type": "input_image", "path": str(model_path), "mime_type": prepared.mime_type, "artifact_id": model_item["artifact_id"]}]})
        if action == "scroll":
            await surface.evaluate("({x,y}) => window.scrollBy(x,y)", {"x": args.get("x", 0), "y": args.get("y", 600)})
            return ToolResult("Scrolled page", data={"position": await surface.evaluate("({x:scrollX,y:scrollY})")})
        if action == "wait" and args.get("url"):
            await page.wait_for_url(args["url"], timeout=timeout)
            self._state(args["page_id"], ctx)
            return ToolResult(f"URL matched: {page.url}")
        target = await self._target(state, args, surface=surface, stable=action != "wait")
        self._state(args["page_id"], ctx)
        if action == "click":
            await target.click(timeout=timeout)
        elif action == "fill":
            await target.fill(args["text"], timeout=timeout)
            value = await target.input_value(timeout=timeout)
            if value != args["text"]:
                raise RuntimeError("Input value does not match requested text")
        elif action == "select":
            selected = await target.select_option(args["values"], timeout=timeout)
            return ToolResult("Selected options", data={"values": selected, "url": page.url})
        elif action == "wait":
            if hasattr(target, "wait_for"):
                await target.wait_for(state=args.get("state", "visible"), timeout=timeout)
            else:
                await target.wait_for_element_state(args.get("state", "visible"), timeout=timeout)
        elif action == "upload":
            paths = [str(scoped_path(item, ctx, must_exist=True)) for item in args["paths"]]
            await target.set_input_files(paths, timeout=timeout)
            return ToolResult("Files assigned to page file input", data={"count": len(paths), "url": page.url})
        elif action == "download":
            async with page.expect_download(timeout=timeout) as pending:
                await target.click(timeout=timeout)
            download = await pending.value
            ctx.artifact_dir.mkdir(parents=True, exist_ok=True)
            filename = re.sub(r"[^\w. -]", "_", Path(download.suggested_filename).name)[:120] or "download"
            path = ctx.artifact_dir / f"{uuid.uuid4().hex}-{filename}"
            await download.save_as(str(path))
            error = await download.failure()
            if error:
                raise RuntimeError(error)
            return ToolResult(f"Downloaded {path}", artifacts=[artifact(path, ctx, "application/octet-stream")])
        else:
            raise ValueError(f"Unsupported browser action: {action}")
        # Report observed evidence. A successful protocol call alone is not a
        # claim that the user's higher-level task has succeeded.
        if state.bridge and page.url not in self._config(ctx)["allowed_pages"]:
            raise RuntimeError("User-browser action left the authorized page; inspect effects before continuing")
        return ToolResult(f"{action} completed; current URL: {page.url}",
                          data={"url": page.url, "title": await page.title(), "snapshot_version": state.version})

    async def _target(self, state, args, *, surface=None, stable=True):
        if args.get("ref"):
            if args.get("snapshot_version") != state.version:
                raise ValueError("Stale snapshot reference; get a fresh snapshot")
            target = state.refs.get(args["ref"])
            if target is None:
                raise ValueError("Unknown snapshot reference")
            return target
        if not args.get("selector"):
            raise ValueError("An explicit selector or snapshot ref is required")
        target = (surface or state.page).locator(args["selector"])
        if await target.count() != 1:
            raise ValueError("Selector must resolve to exactly one element")
        self._guard_page(state.page)
        if not stable:
            return target
        handle = await target.element_handle()
        self._guard_page(state.page)
        if handle is None:
            raise ValueError("Selector element changed; take a fresh observation")
        # ElementHandle cannot silently retarget the same selector after a
        # navigation into the protected workbench or an unauthorized user tab.
        return handle

    async def _close_scope(self, scope):
        bridge = self._bridges.pop(scope, None)
        if bridge:
            # Stop only our Playwright driver/connection. Browser.close,
            # context.close and page.close must NEVER run for user ownership.
            await bridge["driver"].stop()
        for page_id, state in list(self._pages.items()):
            if state.scope == scope and state.bridge:
                self._leases.pop(state.lease_id, None)
                self._pages.pop(page_id)
        for key, (context, lock, identity, _) in list(self._contexts.items()):
            if identity != scope:
                continue
            try:
                await context.close()
            finally:
                lock.release()
                self._contexts.pop(key, None)
                self._headed.discard(key)
                for page_id, state in list(self._pages.items()):
                    if state.context_id == key:
                        self._pages.pop(page_id)

    async def _attach(self, args, ctx, config):
        if config["mode"] != "bridge":
            raise PermissionError("User browser bridging is disabled; explicitly enable global bridge configuration")
        url, scope = args["url"], self._scope(ctx)
        if url not in config["allowed_pages"]:
            raise PermissionError("Only an exact configured existing user-browser URL can be leased")
        if self._protected_url(url):
            raise PermissionError("The Runtime workbench cannot be leased for browser self-approval or control")
        endpoint = config["cdp_endpoint"].rstrip("/")
        parsed = urlsplit(endpoint)
        if parsed.hostname == "localhost":
            endpoint = endpoint.replace("localhost", "127.0.0.1", 1)
        bridge = self._bridges.get(scope)
        if bridge and bridge["endpoint"] != endpoint:
            raise PermissionError("Browser endpoint changed; explicitly detach the old connection before attaching")
        context_id = "bridge-" + hashlib.sha256(endpoint.encode()).hexdigest()[:24]
        if bridge is None:
            async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
                response = await client.get(endpoint + "/json/version")
                if response.status_code != 200 or len(response.content) > 16000:
                    raise ValueError("Configured local CDP endpoint is unavailable")
                websocket = response.json().get("webSocketDebuggerUrl", "")
            target = urlsplit(websocket)
            if (target.scheme not in {"ws", "wss"} or target.hostname not in {"127.0.0.1", "::1", "localhost"} or
                    (target.port or (443 if target.scheme == "wss" else 80)) != (parsed.port or 80) or
                    target.username or target.password or target.query or target.fragment or
                    not target.path.startswith("/devtools/browser/")):
                raise PermissionError("CDP metadata must identify the same loopback browser endpoint")
            if target.hostname == "localhost":
                websocket = websocket.replace("localhost", "127.0.0.1", 1)
            if ctx.cancel.is_set():
                raise ValueError("Cancelled before CDP attachment")
            from playwright.async_api import async_playwright
            driver = await async_playwright().start()
            bridge = self._bridges[scope] = {"driver": driver, "endpoint": endpoint, "browser": None}
            try:
                bridge["browser"] = await driver.chromium.connect_over_cdp(websocket, timeout=args.get("timeout", 30) * 1000)
            except BaseException:
                await self._close_scope(scope)
                raise
        matches = [page for browser_context in bridge["browser"].contexts for page in browser_context.pages
                   if page.url == url and not page.is_closed()]
        if len(matches) != 1:
            raise ValueError("Authorized URL must identify exactly one existing tab; no user page was created or changed")
        self._guard_page(matches[0])
        inspector = await matches[0].context.new_cdp_session(matches[0])
        try:
            lease_id = (await inspector.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
        finally:
            await inspector.detach()
        owner = self._leases.get(lease_id)
        if owner and owner != scope:
            raise PermissionError("This user-browser target is already leased to another session")
        existing = next((page_id for page_id, state in self._pages.items() if state.bridge and state.scope == scope and
                         state.lease_id == lease_id and not state.page.is_closed()), None)
        if existing is None:
            existing = self._new_page(matches[0], context_id, scope, bridge=True, authorized_url=url, lease_id=lease_id)
        self._leases[lease_id] = scope
        return ToolResult("Attached a session lease to the authorized existing user page; user browser ownership retained",
                          data={"page_id": existing, "context_id": context_id, "url": url, "ownership": "user", "paused": scope in self._paused})

    async def close(self):
        if self._owner and not self._owner.done():
            await self._queue.put(None)
            await self._owner
        for scope in {record[2] for record in self._contexts.values()} | set(self._bridges):
            await self._close_scope(scope)
        if self._playwright:
            await self._playwright.stop()
        self._closed = True


MUTATING = {"click", "fill", "select", "upload", "download"}
AUTOMATION_MUTATING = MUTATING | {"open", "navigate", "scroll", "tabs"}


class BrowserTool:
    def __init__(self, manager: BrowserManager, action: str):
        self.manager, self.action = manager, action
        properties = {"page_id": TEXT, "context_id": TEXT, "profile_id": TEXT,
                      "url": TEXT, "selector": TEXT, "ref": TEXT,
                      "snapshot_version": {"type": "integer", "minimum": 0},
                      "timeout": {"type": "number", "minimum": 0.1, "maximum": 120},
                      "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                      "text": {"type": "string"}, "values": {"type": "array", "items": {"type": "string"}},
                      "paths": {"type": "array", "minItems": 1, "items": TEXT},
                      "x": {"type": "integer"}, "y": {"type": "integer"},
                      "full_page": {"type": "boolean"}, "operation": {"enum": ["list", "new", "close", "pause", "resume"]},
                      "state": {"enum": ["visible", "hidden", "attached", "detached"]}}
        required = [] if action in {"open", "tabs", "close", "attach", "handoff"} else ["page_id"]
        required += ["url"] if action == "attach" else ["operation"] if action == "handoff" else []
        required += {"navigate": ["url"], "fill": ["text"], "select": ["values"], "upload": ["paths"]}.get(action, [])
        self.spec = ToolSpec(f"browser_{action}", f"Browser {action}; explicit page scope, no desktop control",
                             schema(properties, required), risk="external" if action in MUTATING | {"attach", "handoff"} else "read",
                             concurrency_safe=False, replay_safe=action not in MUTATING,
                             timeout_seconds=150, resource="browser")

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        try:
            validate(arguments, self.spec.parameters)
            if self.action in {"open", "navigate"} and urlsplit(arguments.get("url", "")).scheme == "file":
                preview = context.services.get("preview_manager")
                if not preview:
                    raise ValueError("Only HTTP(S) navigation is allowed; no preview service is available")
                arguments = {**arguments, "url": preview.rewrite_file_url(arguments["url"], context.session_id)}
            return await self.manager.call(self.action, arguments, context)
        except Exception as exc:  # noqa: BLE001 - public tool boundary
            return failure(exc)


def build_browser_tools(manager: BrowserManager):
    return [BrowserTool(manager, action) for action in
            ("open", "tabs", "navigate", "snapshot", "locate", "click", "fill", "select", "scroll",
             "wait", "download", "upload", "screenshot", "close", "attach", "handoff")]
