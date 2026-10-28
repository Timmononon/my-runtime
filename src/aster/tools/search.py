"""Configured real search backends, with explicit credential origins and approval."""
from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import unquote, unquote_plus, urlsplit

import httpx

from aster.config import canonical_http_origin
from aster.domain import ToolResult, ToolSpec, Usage, to_dict
from aster.providers.deepseek import _cancellable
from aster.providers.profiles import PublicProviderTransport, native_deepseek_client
from aster.state.store import BudgetExceeded, StateError
from aster.tools.files import TEXT, schema
from aster.tools.http import PRIVATE_CREDENTIALS, safe_url


def validate_search_config(value):
    from aster.customization.management import validate_web_search
    return validate_web_search(value)


DEEPSEEK_SEARCH_ENDPOINT = "https://api.deepseek.com/anthropic/v1/messages"


class SearchFailure(ValueError):
    def __init__(self, message, category="protocol"):
        super().__init__(message)
        self.category = category


def safe_text(value, secret):
    if not isinstance(value, str):
        return ""
    if secret:
        for decode in (lambda text: text, unquote, unquote_plus):
            decoded = decode(value)
            if secret in decoded:
                value = decoded.replace(secret, "[redacted]")
    return re.sub(r"(?i)bearer\s+[^\s,;]+|\bsk-[A-Za-z0-9_-]{6,}", "[redacted]", value)[:4000]


def safe_result_url(value, secret):
    parsed = urlsplit(value) if isinstance(value, str) else None
    if (not parsed or parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or (secret and any(secret in item for item in (value, unquote(value), unquote_plus(value))))):
        return None
    return safe_url(value)


def native_usage(raw, identity):
    def count(value):
        return value if type(value) is int and value >= 0 else None
    raw = raw if isinstance(raw, dict) else {}
    input_tokens, output_tokens = count(raw.get("input_tokens")), count(raw.get("output_tokens"))
    hit, creation = count(raw.get("cache_read_input_tokens")), count(raw.get("cache_creation_input_tokens"))
    miss = None
    if "cache_read_input_tokens" in raw or "cache_creation_input_tokens" in raw:
        if all(value is not None for value in (input_tokens, hit, creation)):
            miss = input_tokens + creation
            input_tokens = miss + hit
        else:
            input_tokens = None
    server = raw.get("server_tool_use", {})
    requests = count(server.get("web_search_requests")) if isinstance(server, dict) else None
    return Usage(input_tokens, output_tokens, cache_hit_tokens=hit, cache_miss_tokens=miss,
                 raw={"purpose": "web_search", **identity, "native_search_requests": requests})


class SearchTool:
    spec = ToolSpec("web_search", "Search the explicitly configured service and return native observed source links",
                    schema({"query": {**TEXT, "maxLength": 4000},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, ["query"]),
                    risk="external", replay_safe=False, concurrency_safe=True, timeout_seconds=310)

    def __init__(self, *, client_factory=None):
        self.client_factory = client_factory  # Explicit offline transport injection.

    async def execute(self, arguments, context):
        state = {"sent": False, "received": False, "usage": None, "cost": None}
        try:
            config = validate_search_config(context.services.get("web_search_config", {}))
            if not config:
                return ToolResult("Web search is disabled; configure a search backend in settings", True, "unavailable", "not_executed")
            async with asyncio.timeout(config["timeout_seconds"]):
                if config["backend"] == "deepseek":
                    return await _cancellable(self._native_request(arguments, context, config, state), context.cancel)
                return await _cancellable(self._request(arguments, context, config, state), context.cancel)
        except asyncio.CancelledError:
            message, category = "Web search cancelled; no automatic retry", "cancelled"
        except TimeoutError:
            message, category = "Web search exceeded its configured time budget; no automatic retry", "timeout"
        except Exception as error:  # noqa: BLE001 - network exceptions can embed headers and source query.
            category = (error.category if isinstance(error, SearchFailure) else "budget" if isinstance(error, BudgetExceeded)
                        else "configuration" if isinstance(error, (ValueError, KeyError, PermissionError, StateError)) else "search")
            message = (str(error) if isinstance(error, SearchFailure) else
                       "Web search could not complete; check configuration and backend availability")
        outcome = "unknown" if state["sent"] and not state["received"] else "known" if state["sent"] else "not_executed"
        data = {"usage": to_dict(state["usage"]), "cost_estimate": state["cost"]} if state["usage"] else {}
        return ToolResult(message, True, category, outcome, data=data)

    def _current_native(self, context, config, profiles, pin):
        profiles.validate_pin(pin)
        loader = context.services.get("search_config_loader")
        if loader and validate_search_config(loader()) != config:
            raise SearchFailure("Search settings changed during this operation; start a new explicitly approved search", "configuration")

    async def _native_request(self, arguments, context, config, state):
        profiles = context.services.get("search_profiles")
        if profiles is None:
            raise SearchFailure("Configured DeepSeek provider is unavailable", "configuration")
        profile = profiles.get(config["provider_id"])
        if profile["protocol"] != "deepseek_responses" or profile["base_url"] != "https://api.deepseek.com":
            raise SearchFailure("Native search requires the fixed official DeepSeek provider", "configuration")
        if not profile["configured"]:
            raise SearchFailure("Native DeepSeek search requires the existing provider key to be configured", "authentication")
        pin = profiles.pin(config["provider_id"], config["model"], None)
        self._current_native(context, config, profiles, pin)
        secret = profiles.resolve_pin(pin)
        identity = {"provider_id": pin["id"], "model": pin["model"], "protocol": "anthropic_messages"}
        begin = context.services.get("search_usage_begin")
        receipt = await begin(context, identity) if begin else None
        self._current_native(context, config, profiles, pin)
        if context.cancel.is_set():
            raise asyncio.CancelledError
        factory = self.client_factory or context.services.get("search_client_factory") or profiles.client_factory
        client = factory() if factory else native_deepseek_client(config["timeout_seconds"])
        body = {"model": config["model"], "max_tokens": config["max_tokens"],
                "messages": [{"role": "user", "content": [{"type": "text", "text": "Perform a web search for the query: " + arguments["query"]}]}],
                "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": config["max_uses"]}]}
        headers = {"x-api-key": secret, "Authorization": "Bearer " + secret, "anthropic-version": "2023-06-01",
                   "accept": "application/json", "content-type": "application/json"}
        try:
            state["sent"] = True
            async with client.stream("POST", DEEPSEEK_SEARCH_ENDPOINT, json=body, headers=headers,
                                     follow_redirects=False) as response:
                content = bytearray()
                if response.status_code == 200:
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 4 * 1024 * 1024:
                            raise SearchFailure("Native search exceeded its response byte budget", "budget")
                state["received"] = True
            payload = None
            if response.status_code == 200:
                try:
                    payload = json.loads(content)
                except (ValueError, TypeError):
                    pass
            state["usage"] = native_usage(payload.get("usage") if isinstance(payload, dict) else None, identity)
            finish = context.services.get("search_usage_finish")
            if finish:
                state["cost"] = await finish(context, receipt, state["usage"])
            self._current_native(context, config, profiles, pin)
            if context.cancel.is_set():
                raise asyncio.CancelledError
            if response.status_code != 200:
                raise SearchFailure(f"DeepSeek search returned HTTP {response.status_code}; no automatic retry", "search")
            results = self._native_results(payload, secret)
            limit = min(arguments.get("limit", config["result_limit"]), config["result_limit"])
            data = {"results": results[:limit], "backend": "deepseek", "truncated": len(results) > limit,
                    "no_data": not results, "retrieval": "native_web_search", "result_limit": limit,
                    "usage": to_dict(state["usage"]), "cost_estimate": state["cost"]}
            return ToolResult(json.dumps(data, ensure_ascii=False), data=data)
        finally:
            await client.aclose()

    @staticmethod
    def _native_results(payload, secret):
        blocks = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(blocks, list) or payload.get("error"):
            raise SearchFailure("DeepSeek returned an invalid native search response")
        native = [block for block in blocks if isinstance(block, dict) and block.get("type") == "web_search_tool_result"]
        if not native:
            raise SearchFailure("DeepSeek returned no native web search result blocks")
        snippets = {}
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            for citation in block.get("citations", []) if isinstance(block.get("citations", []), list) else []:
                if isinstance(citation, dict) and (url := safe_result_url(citation.get("url"), secret)):
                    snippets.setdefault(url, safe_text(citation.get("cited_text"), secret))
        results, seen = [], set()
        for block in native:
            rows = block.get("content")
            if not isinstance(rows, list):
                raise SearchFailure("DeepSeek native search reported a tool error or invalid result block", "search")
            for row in rows:
                if not isinstance(row, dict) or row.get("type") != "web_search_result":
                    raise SearchFailure("DeepSeek native search reported an invalid result item")
                url = safe_result_url(row.get("url"), secret)
                if not url or url in seen:
                    continue
                seen.add(url)
                results.append({"url": url, "title": safe_text(row.get("title"), secret), "snippet": snippets.get(url, ""),
                                "source": "deepseek", "published_at": safe_text(row.get("page_age"), secret) or None})
        return results

    async def _request(self, args, ctx, config, state):
        headers, secret = {}, None
        reference = config.get("credential_ref")
        if reference:
            forbidden = PRIVATE_CREDENTIALS | {value.casefold() for value in ctx.services.get("protected_credential_refs", [])}
            if reference.casefold() in forbidden:
                raise PermissionError("Model credentials cannot be used for search")
            origins = ctx.services.get("http_credential_origins", {}).get(reference, [])
            if canonical_http_origin(config["base_url"]) not in {canonical_http_origin(value) for value in origins}:
                raise PermissionError("Search credential is not bound to this origin")
            secret = ctx.services["secret_resolver"](reference)
            if not secret or any(value in secret for value in ("\r", "\n")):
                raise ValueError("Search credential is unavailable")
            headers["X-Subscription-Token" if config["backend"] == "brave" else "Authorization"] = secret if config["backend"] == "brave" else "Bearer " + secret
        elif config["backend"] == "brave":
            raise ValueError("Brave search requires its own configured credential reference")
        client_factory = self.client_factory or ctx.services.get("search_client_factory")
        client = client_factory() if client_factory else httpx.AsyncClient(
            timeout=config["timeout_seconds"], follow_redirects=False, trust_env=False,
            transport=PublicProviderTransport(config["base_url"]))
        limit = min(args.get("limit", config["result_limit"]), config["result_limit"])
        params = {"q": args["query"], "count": limit} if config["backend"] == "brave" else {"q": args["query"], "format": "json"}
        path = "/res/v1/web/search" if config["backend"] == "brave" else "/search"
        try:
            state["sent"] = True
            async with client.stream("GET", config["base_url"] + path, params=params, headers=headers, follow_redirects=False) as response:
                if response.status_code != 200:
                    state["received"] = True
                    return ToolResult(f"Search backend returned HTTP {response.status_code}; no automatic retry", True, "search", "known")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 4 * 1024 * 1024:
                        raise ValueError("Search response exceeds receive budget")
            state["received"] = True
            raw = json.loads(body)
            if not isinstance(raw, dict) or raw.get("error"):
                raise SearchFailure("Search backend returned an invalid response")
            rows = (raw.get("web") or {}).get("results", []) if config["backend"] == "brave" else raw.get("results")
            if not isinstance(rows, list):
                raise SearchFailure("Search backend returned an invalid results schema")
            results = []

            for row in rows:
                url = row.get("url") if isinstance(row, dict) else None
                url = safe_result_url(url, secret)
                if not url:
                    continue
                results.append({"title": safe_text(row.get("title"), secret), "url": url,
                                "snippet": safe_text(row.get("description" if config["backend"] == "brave" else "content"), secret),
                                "source": config["backend"]})
                if len(results) == limit:
                    break
            data = {"results": results, "backend": config["backend"], "truncated": len(rows) > limit,
                    "no_data": not results, "retrieval": "configured_search_backend", "result_limit": limit}
            return ToolResult(json.dumps(data, ensure_ascii=False), data=data)
        finally:
            await client.aclose()
