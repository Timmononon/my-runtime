"""Explicit CLI diagnostics with an opt-in calendar read; no model run."""
from __future__ import annotations

import asyncio
import re
import sys
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from aster.domain import ToolContext, ToolResult


class CheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verify_business: bool = Field(default=False, strict=True)


def _windows_registry_metadata():
    result = {"supported": sys.platform == "win32", "key_exists": None, "value_count": None}
    if not result["supported"]:
        return result
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\LarkCli\keychain\lark-cli", 0,
                            winreg.KEY_QUERY_VALUE) as key:
            # Only key metadata. Never enumerate value names or read/decrypt a
            # credential, even when checking a different daemon user context.
            count = winreg.QueryInfoKey(key)[1]
            result.update(key_exists=True, value_count=count)
    except FileNotFoundError:
        result.update(key_exists=False, value_count=0)
    except OSError:
        pass
    return result


def _status_payload(result):
    if not isinstance(result, ToolResult) or not isinstance(result.data, dict):
        raise TypeError("Invalid local diagnostic response")
    if result.outcome not in {"known", "not_executed", "unknown"}:
        raise ValueError("Invalid local diagnostic outcome")
    source = result.data
    safe = {key: source[key] for key in ("cli_installed", "app_configured")
            if type(source.get(key)) is bool}
    for identity in ("user", "bot"):
        record = source.get(identity)
        if not isinstance(record, dict):
            continue
        fields = ("available", "token_status", "scope_count", "scopes") if identity == "user" else (
            "available", "token_status")
        safe[identity] = {key: record[key] for key in fields if key in record}
        if type(safe[identity].get("available")) is not bool:
            raise ValueError("Invalid local diagnostic identity")
        status = safe[identity].get("token_status")
        if status is not None and (not isinstance(status, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", status)):
            raise ValueError("Invalid local diagnostic token status")
        if identity == "user":
            count, scopes = record.get("scope_count"), record.get("scopes")
            if ((count is not None and (type(count) is not int or count < 0)) or not isinstance(scopes, list)
                    or any(not isinstance(item, str) or not re.fullmatch(r"[a-zA-Z0-9_.:/-]{1,200}", item)
                           for item in scopes)):
                raise ValueError("Invalid local diagnostic scopes")
    for key in ("account_fingerprint", "user_binding_fingerprint"):
        value = source.get(key)
        if isinstance(value, str) and re.fullmatch(r"[a-fA-F0-9]{16}", value):
            safe[key] = value.lower()
    for key in ("error_type", "error_subtype"):
        value = source.get(key)
        if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", value):
            safe[key] = value
    if not result.is_error and not {"cli_installed", "app_configured", "user", "bot"}.issubset(safe):
        raise ValueError("Incomplete local diagnostic response")
    safe.update(is_error=bool(result.is_error), outcome=result.outcome,
                business_access_verified=False, diagnostic_only=True)
    return safe


def _business_probe(result):
    if not isinstance(result, ToolResult):
        return {"attempted": True, "success": False, "error_type": "protocol"}
    source = result.data if isinstance(result.data, dict) else {}
    if not result.is_error and result.outcome == "known" and source.get("ok") is True:
        return {"attempted": True, "success": True}
    error = source.get("error") if isinstance(source.get("error"), dict) else {}
    safe = {"attempted": True, "success": False}
    for target, value in (("error_type", result.error_category or error.get("type") or "protocol"),
                          ("error_subtype", error.get("subtype"))):
        if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", value):
            safe[target] = value
    if type(error.get("code")) is int:
        safe["error_code"] = error["code"]
    return safe


def create_feishu_router(coordinator):
    router = APIRouter()

    @router.post("/feishu/check")
    async def check(request: Request, body: CheckRequest | None = None):
        if request.query_params:
            return JSONResponse({"error": "invalid_request"}, status_code=422)
        if coordinator._closed:
            return JSONResponse({"is_error": True, "error_type": "runtime_closed", "outcome": "not_executed",
                                 "diagnostic_only": True, "business_access_verified": False}, status_code=503)
        tools = getattr(coordinator.factory, "tools", ())
        candidates = [tool for tool in tools if tool.spec.name == "feishu_cli"]
        if len(candidates) != 1 or not callable(getattr(candidates[0], "connection_status", None)):
            return JSONResponse({"is_error": True, "error_type": "configuration", "outcome": "not_executed",
                                 "diagnostic_only": True, "business_access_verified": False}, status_code=503)
        cancel = asyncio.Event()
        context = ToolContext("feishu-diagnostic", "diagnostic-" + uuid.uuid4().hex, coordinator.data_dir,
                              coordinator.data_dir / "tmp" / "feishu-check", cancel)

        async def watch_lifecycle():
            while not cancel.is_set():
                if coordinator._closed or await request.is_disconnected():
                    cancel.set()
                    return
                await asyncio.sleep(0.05)

        watcher = asyncio.create_task(watch_lifecycle())
        try:
            result = await candidates[0].connection_status(context)
            payload = _status_payload(result)
            if body is not None and body.verify_business:
                if cancel.is_set() or coordinator._closed:
                    payload["business_probe"] = {"attempted": False, "success": False, "error_type": "cancelled"}
                    payload["is_error"] = True
                else:
                    business = await candidates[0].execute({"argv": ["calendar", "+agenda"], "identity": "user"}, context)
                    probe = _business_probe(business)
                    payload["business_probe"] = probe
                    payload["business_access_verified"] = probe["success"]
                    if not probe["success"]:
                        payload["is_error"] = True
                        payload["error_type"] = probe.get("error_type", "protocol")
        except asyncio.CancelledError:
            cancel.set()
            raise
        except Exception:  # noqa: BLE001 - never expose native output or credential exception details.
            payload = {"is_error": True, "error_type": "protocol", "outcome": "known",
                       "diagnostic_only": True, "business_access_verified": False}
        finally:
            cancel.set()
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        payload["windows_registry"] = _windows_registry_metadata()
        return JSONResponse(payload, status_code=503 if payload["is_error"] else 200)

    return router
