"""Send plain text to the locally configured Feishu group bot."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path

import httpx

from aster.customization.credentials import (
    FEISHU_WEBHOOK_CREDENTIAL_SERVICE,
    CredentialError,
    CredentialStore,
)
from aster.domain import ToolContext, ToolResult, ToolSpec
from aster.state import StateError
from aster.tools.files import schema
from aster.tools.http import pin_url

_ORIGIN = "https://open.feishu.cn"
_PATH = "/open-apis/bot/v2/hook/"
_UUID = r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}"
_WEBHOOK_URL = re.compile(re.escape(_ORIGIN + _PATH) + _UUID)
_LABEL_SECRET = re.compile(_UUID + r"|[0-9a-fA-F]{32}|://|" + re.escape(_PATH), re.IGNORECASE)
_ACCOUNT = "destination"
_GROUP_LIMIT = 80
_REQUEST_LIMIT = 20 * 1024  # Feishu checks the entire UTF-8 JSON body.
_RESPONSE_LIMIT = 16 * 1024
_TIMEOUT_SECONDS = 20

_REJECTIONS = {
    9499: ("validation", "飞书拒绝了消息，请检查纯文本格式和请求大小。"),
    11232: ("api", "飞书机器人触发了频率限制，消息未发送。"),
    19021: ("configuration", "飞书的签名校验拒绝了请求，请检查自定义机器人的安全设置。"),
    19022: ("configuration", "飞书的 IP 白名单拒绝了请求，请检查自定义机器人的安全设置。"),
    19024: ("validation", "飞书未找到所需的安全关键词，请核对自定义机器人的关键词设置。"),
}
_STALE_CONFIGURATION = "飞书群机器人配置已更改或不可用，请刷新工具后重新确认目标群。"


def _validate_destination(group: str, url: str) -> str:
    if (not isinstance(group, str) or not 1 <= len(group) <= _GROUP_LIMIT
            or not group.isprintable() or not group.strip() or _LABEL_SECRET.search(group)):
        raise ValueError("飞书群名应为 1–80 个可打印字符，且不能包含链接或机器人凭据。")
    if not isinstance(url, str) or _WEBHOOK_URL.fullmatch(url) is None:
        raise ValueError("请输入飞书群自定义机器人的有效 HTTPS Webhook 地址。")
    return group.strip()


def configure_feishu_webhook(data_dir: str | Path, group: str, url: str) -> None:
    """Save the operator-selected destination as one atomic credential entry."""
    group = _validate_destination(group, url)
    value = json.dumps({"group": group, "url": url}, ensure_ascii=False, separators=(",", ":"))
    CredentialStore(FEISHU_WEBHOOK_CREDENTIAL_SERVICE, data_dir=data_dir).set(_ACCOUNT, value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("Duplicate JSON keys")
    return value


def _reject_json_constant(_value: str) -> None:
    raise ValueError("Invalid JSON constant")


def _destination(store: CredentialStore) -> tuple[str, str, bytes] | None:
    try:
        raw = store.get(_ACCOUNT, use_environment=False)
        if raw is None or len(raw) > 1024:
            return None
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or value.keys() != {"group", "url"}:
            return None
        group = _validate_destination(value["group"], value["url"])
        return group, value["url"][len(_ORIGIN):], hashlib.sha256(raw.encode("utf-8")).digest()
    except (CredentialError, ValueError, RecursionError):
        return None


def _uncertain(category: str = "protocol") -> ToolResult:
    return ToolResult("飞书消息的发送结果未知，请先在目标群内核实。", True, category, "unknown")


async def _discard_trace_payload(_event: str, info: dict) -> None:
    # httpcore logs these fields at DEBUG after calling the trace extension.
    # A response header or transport exception can echo the secret path.
    info.pop("return_value", None)
    info.pop("exception", None)


async def _delivery_result(response: httpx.Response) -> ToolResult:
    status = response.status_code // 100
    if status not in {2, 4} or response.headers.get("content-encoding", "identity").lower() != "identity":
        return _uncertain()
    declared = response.headers.get("content-length")
    if declared is not None and (not declared.isascii() or not declared.isdecimal()
                                 or len(declared) > 5 or int(declared) > _RESPONSE_LIMIT):
        return _uncertain()
    if response.is_stream_consumed:
        raw = response.content
    else:
        raw = bytearray()
        async for chunk in response.aiter_raw():
            if len(raw) + len(chunk) > _RESPONSE_LIMIT:
                return _uncertain()
            raw.extend(chunk)
    if len(raw) > _RESPONSE_LIMIT or (declared is not None and int(declared) != len(raw)):
        return _uncertain()
    try:
        receipt = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_json_constant)
    except (ValueError, RecursionError):
        return _uncertain()
    if not isinstance(receipt, dict) or type(code := receipt.get("code")) is not int:
        return _uncertain()
    legacy_code = receipt.get("StatusCode", code)
    if type(legacy_code) is not int or legacy_code != code:
        return _uncertain()
    if status == 2 and code == 0:
        return ToolResult("飞书已确认接收群消息。", data={"code": code})
    if code in _REJECTIONS:
        category, message = _REJECTIONS[code]
        return ToolResult(message, True, category, "not_executed", data={"code": code})
    return _uncertain()


class FeishuWebhookTool:
    def __init__(self, store: CredentialStore, group: str, binding: bytes):
        self._store = store
        self._binding = binding
        self.spec = ToolSpec(
            "feishu_webhook_send",
            f"通过飞书（Feishu）群自定义机器人 Webhook 向已配置的群「{group}」发送纯文本。"
            "用户要求使用该群的 Webhook 或群自定义机器人时使用。保持用户指定的发送方式和消息正文，"
            "报告本次发送结果。飞书应用机器人和用户身份使用 feishu_cli。",
            schema({"text": {"type": "string", "minLength": 1, "maxLength": _REQUEST_LIMIT}}, ["text"]),
            risk="external",
            timeout_seconds=_TIMEOUT_SECONDS + 5,
        )

    @classmethod
    def from_local_credentials(cls, data_dir: str | Path) -> FeishuWebhookTool | None:
        store = CredentialStore(FEISHU_WEBHOOK_CREDENTIAL_SERVICE, data_dir=data_dir)
        destination = _destination(store)
        if destination is None:
            return None
        group, _, binding = destination
        return cls(store, group, binding)

    def _bound_path(self) -> str | None:
        destination = _destination(self._store)
        if destination is None or destination[2] != self._binding:
            return None
        return destination[1]

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        if context.cancel.is_set():
            return ToolResult("已取消飞书发送。", True, "cancelled", "not_executed")
        message = arguments.get("text") if isinstance(arguments, dict) and arguments.keys() == {"text"} else None
        if not isinstance(message, str) or not message.strip():
            return ToolResult("请输入要发送到飞书群的纯文本。", True, "validation", "not_executed")
        try:
            payload = (json.dumps({"msg_type": "text", "content": {"text": message}},
                                  ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                       if len(message) <= _REQUEST_LIMIT else b"")
        except UnicodeEncodeError:
            return ToolResult("飞书消息包含无效的 Unicode 字符。", True, "validation", "not_executed")
        if not payload or len(payload) > _REQUEST_LIMIT:
            return ToolResult("飞书消息的 JSON 请求超过了 20 KB。", True, "validation", "not_executed")

        dispatched = False

        async def send() -> ToolResult:
            nonlocal dispatched
            if self._bound_path() is None:
                return ToolResult(_STALE_CONFIGURATION, True, "configuration", "not_executed")
            pinned, host, sni = await pin_url(
                _ORIGIN, set(), dns_fallback=context.services.get("http_dns_fallback", "google"),
            )
            guard = context.services.get("task_guard")
            if guard is not None:
                try:
                    binding = guard()
                except (StateError, KeyError, ValueError, TypeError, OSError):
                    binding = None
                if not binding or binding.get("role") == "reader":
                    return ToolResult("任务的发送权限已失效。", True, "permission", "not_executed")
            path = self._bound_path()
            if path is None:
                return ToolResult(_STALE_CONFIGURATION, True, "configuration", "not_executed")
            if context.cancel.is_set():
                return ToolResult("已取消飞书发送。", True, "cancelled", "not_executed")
            steering = context.services.get("steering")
            if steering is not None and steering.is_set():
                return ToolResult("收到新的用户指令，飞书消息未发送。", True, "steered", "not_executed")
            request = httpx.Request(
                "POST", pinned.copy_with(path=path), content=payload,
                headers={"Host": host, "Content-Type": "application/json", "Accept-Encoding": "identity"},
                extensions={"sni_hostname": sni, "trace": _discard_trace_payload,
                            "timeout": httpx.Timeout(_TIMEOUT_SECONDS).as_dict()},
            )
            # Calling the transport directly avoids HTTPX's full-URL request log.
            transport = httpx.AsyncHTTPTransport(trust_env=False, retries=0)
            try:
                dispatched = True
                response = await transport.handle_async_request(request)
                try:
                    return await _delivery_result(response)
                finally:
                    await response.aclose()
            finally:
                await transport.aclose()

        work = asyncio.create_task(send())
        stopped = asyncio.create_task(context.cancel.wait())
        try:
            done, _ = await asyncio.wait((work, stopped), timeout=_TIMEOUT_SECONDS,
                                         return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                return work.result()
            category = "cancelled" if stopped in done else "timeout"
        except asyncio.CancelledError:
            category = "cancelled"
        except (TimeoutError, httpx.TimeoutException):
            category = "timeout"
        except Exception:  # noqa: BLE001 - network exceptions can contain the webhook URL.
            category = "network"
        finally:
            work.cancel()
            stopped.cancel()
            await asyncio.gather(work, stopped, return_exceptions=True)
        if dispatched:
            return _uncertain(category)
        message = {"cancelled": "已取消飞书发送。", "timeout": "飞书地址验证超时，消息未发送。",
                   "network": "无法验证飞书地址，消息未发送。"}[category]
        return ToolResult(message, True, category, "not_executed")
