"""Bounded native Feishu CLI calls through the Runtime approval and intent path."""
from __future__ import annotations

import dataclasses
import hashlib
import html
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote

from jsonschema import ValidationError, validate

from aster.domain import ToolContext, ToolResult, ToolSpec
from aster.platform.processes import MEMORY_CAPTURE_LIMIT_ERROR

# These flags and shortcut risks were checked against official lark-cli 1.0.100.
# Unknown commands/flags are denied rather than inheriting a read grant.
_COMMANDS = {
    ("docs", "+fetch"): ("read", ("doc doc-format scope detail keyword context-after context-before end-block-id "
                                 "lang max-depth revision-id start-block-id"), "dry-run", False),
    ("docs", "+search"): ("read", "query filter page-size page-token", "dry-run", True),
    ("docs", "+create"): ("external", "doc-format title content parent-token parent-position reference-map", "dry-run", False),
    ("docs", "+update"): ("external", ("doc doc-format command content pattern block-id start-block-id end-block-id "
                           "src-block-ids revision-id reference-map"), "dry-run", False),
    ("im", "+chat-search"): ("read", ("query member-ids chat-modes search-types sort page-size page-token page-limit "
                             "page-delay"), "dry-run page-all disable-search-by-user exclude-muted is-manager", True),
    ("im", "+chat-messages-list"): ("read", "chat-id user-id start end order page-size page-token page-limit page-delay",
                                    "dry-run page-all no-reactions concise", True),
    ("im", "+messages-search"): ("read", ("query chat-id chat-type sender sender-type exclude-sender-type "
                                 "at-chatter-ids include-attachment-type start end page-size page-token page-limit"),
                                 "dry-run page-all is-at-me no-reactions", True),
    ("im", "+messages-send"): ("external", "chat-id user-id text markdown content msg-type idempotency-key",
                               "dry-run", True),
    ("im", "+messages-reply"): ("external", "message-id text markdown content msg-type idempotency-key",
                                "dry-run reply-in-thread", True),
    ("calendar", "+agenda"): ("read", "calendar-id start end", "dry-run", True),
    ("calendar", "+create"): ("external", "calendar-id summary start end description attendee-ids meeting-owner-id rrule",
                               "dry-run", True),
    ("sheets", "+workbook-info"): ("read", "spreadsheet-token url", "dry-run", True),
    ("sheets", "+cells-get"): ("read", "spreadsheet-token url sheet-id sheet-name range include max-chars skip-hidden",
                               "dry-run", True),
    ("sheets", "+cells-set"): ("external", ("spreadsheet-token url sheet-id sheet-name range cells writes "
                               "copy-to-range allow-overwrite"), "dry-run", True),
}
_ROOTS = {path[0] for path in _COMMANDS}
_FILE_VALUES = {"content", "text", "markdown", "description", "filter", "cells", "writes", "reference-map"}
_SECRET_KEY = re.compile(r"^(?:app_?secret|client_?secret|access_?token|refresh_?token|user_?access_?token|"
                         r"tenant_?access_?token|authorization|api_?key|device_?code|password|secret)$", re.IGNORECASE)
_SECRET_TEXT = re.compile(r"(?i)(\b(?:app[_-]?secret|client[_-]?secret|access[_-]?token|refresh[_-]?token|"
                          r"api[_-]?key|device[_-]?code|password)\b[\"']?\s*[:=]\s*)([\"']?)[^\s\"'&,}]+")
_URL_SECRET = re.compile(r"(?i)([?&](?:access_token|refresh_token|app_secret|device_code|code|token|secret)=)[^&#\s]+")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_TOKEN_STATUSES = {"ready", "valid", "expired", "needs_refresh", "not_authorized", "unsupported", "unavailable",
                   "error", "missing", "not_configured", "refresh_expired", "none", "no_token", "invalid"}
_CONNECTION_ERROR_SUBTYPES = {"cli_not_installed", "not_started", "capture_limit", "not_completed",
                              "invalid_connection_status", "not_configured", "invalid_config", "not_bound",
                              "token_missing", "token_expired", "token_invalid", "invalid_client", "invalid_grant",
                              "missing_scope", "app_scope_not_applied", "access_denied", "challenge_required",
                              "dpop_key_missing", "dpop_required", "storage", "sdk_error", "timeout", "tls", "dns",
                              "server_error", "transport", "protocol", "unknown"}
_AUTH_RECOVERY = (
    "当前运行上下文无法使用现有凭据，请先用精确 argv=['auth','status'] 检查飞书连接状态。"
    "这不等于用户从未授权；保持当前身份，不要自动改成 bot，也不要在业务工具中运行 auth login。"
    "需要补充授权时应由用户通过人工连接入口处理。"
)
_BOT_CHAT_REJECTION = (
    "飞书拒绝本次发送（230002）：当前 bot 不在目标聊天中，本次消息未发送。"
    "用户身份可见的聊天不等于机器人已加入，请先核实指定目标及用户要求的发送身份。"
    "不要自动切换身份、把机器人加入聊天、扩大 OAuth 授权或重发。"
)
_USER_CHAT_REJECTION = (
    "飞书拒绝了本次以本人身份发送的请求（230027），消息未发送。"
    "该错误码没有指出具体缺失的权限。本地授权检查或 dry-run 仅验证本地条件。"
    "请核实当前应用与目标会话的发送权限，保持当前身份和授权范围，等待用户明确指示后再发送。"
)
_SEND_REJECTIONS = {
    230002: ("bot", "api", "unknown", 1, _BOT_CHAT_REJECTION),
    230027: ("user", "authorization", "user_unauthorized", 3, _USER_CHAT_REJECTION),
}
# Official 1.0.100: calendar/description_rich_images.go reads local Markdown
# images; doc/local_doc_resources.go and html5_block_resources.go read @ images,
# resource path attributes, whiteboard @ bodies and reference-map path entries.
_MARKDOWN_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]*)\)")
_DOC_LOCAL_IMAGE = re.compile(r"!\[[\s\S]*?\]\(\s*(?:<\s*)?\\?@")
_DOC_LOCAL_RESOURCE = re.compile(r"(?is)<(?:img|source|whiteboard|html5-block)\b[^>]*\bpath\s*=")
_DOC_WHITEBOARD_FILE = re.compile(r"(?is)<whiteboard\b[^>]*>\s*@(?!@)")
# Only this audited legacy shortcut has a local scope gate before every
# business phase; other shortcuts may check extra scopes after earlier writes.
_SEND_PREFLIGHT_SCOPES = {"user": {"im:message.send_as_user", "im:message"},
                          "bot": {"im:message:send_as_bot"}}


def _is_send_scope_preflight(argv, identity, result, envelope):
    # Official 1.0.100 common/runner.go:1122 checks scopes before runtime,
    # dry-run and Execute. Local errors have no code/log_id; API classification
    # always retains a nonzero code (internal/errclass/classify.go:51-95).
    error = envelope.get("error")
    if not isinstance(error, dict):
        return False
    if (tuple(argv[:2]) != ("im", "+messages-send") or result.exit_code != 3
            or envelope.get("identity") != identity
            or result.stdout.strip() or error.get("type") != "authorization"
            or error.get("subtype") != "missing_scope" or error.get("identity") != identity
            or "code" in error or "log_id" in error):
        return False
    scopes = error.get("missing_scopes")
    return (isinstance(scopes, list) and bool(scopes)
            and all(isinstance(scope, str) and scope in _SEND_PREFLIGHT_SCOPES[identity] for scope in scopes))


def _unique_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Conflicting Feishu response fields")
        value[key] = item
    return value


def _text_send_rejection_hint(arguments, result, envelope) -> str | None:
    # Official 1.0.100 im_messages_send.go:187-264 and helpers.go:314-320
    # perform one text-message POST with no preceding upload. Feishu documents
    # HTTP 400 / 230002 (membership) and 230027 (permissions) as send rejections.
    error = envelope.get("error")
    if not isinstance(error, dict) or type(error.get("code")) is not int:
        return None
    rule = _SEND_REJECTIONS.get(error["code"])
    if rule is None:
        return None
    identity, category, subtype, exit_code, hint = rule
    # PermissionError carries identity; the generic APIError may omit it.
    if error.get("identity") != identity and (category != "api" or "identity" in error):
        return None
    if (arguments.get("identity", "user") != identity or envelope.get("identity") != identity
            or envelope.get("ok") is not False
            or type(result.exit_code) is not int or result.exit_code != exit_code or not result.pid
            or result.cancelled or result.timed_out or result.error is not None or result.stdout.strip()
            or error.get("type") != category or error.get("subtype") != subtype
            or not isinstance(error.get("log_id"), str) or not error["log_id"].strip()
            or not isinstance(error.get("message"), str) or not error["message"].strip()):
        return None
    argv = arguments["argv"]
    if tuple(argv[:2]) != ("im", "+messages-send"):
        return None
    values, tokens = {}, iter(argv[2:])
    for token in tokens:
        flag, separator, value = token[2:].partition("=")
        if not token.startswith("--") or flag not in {"chat-id", "text", "idempotency-key"} or flag in values:
            return None
        value = value if separator else next(tokens, "")
        if not value.strip():
            return None
        values[flag] = value
    if (set(values) != {"chat-id", "text", "idempotency-key"}
            or not re.fullmatch(r"oc_[A-Za-z0-9_-]+", values["chat-id"])
            or len(values["idempotency-key"]) > 50):
        return None
    try:
        complete = json.loads(result.stderr.lstrip("\ufeff").strip(), object_pairs_hook=_unique_json_object)
    except (ValueError, TypeError, RecursionError):
        return None
    if complete != envelope:
        return None
    pending = [complete]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if "data" in item or "message_id" in item:
                return None
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return hint


def _reject_implicit_local_media(command, flag, value):
    if command[0] == "calendar" and flag == "description":
        for match in _MARKDOWN_IMAGE.finditer(value):
            source = match[2].strip().lower()
            if (source and not source.startswith(("http://", "https://", "data:"))
                    and (source.startswith("file://") or "://" not in source)):
                raise ValueError("Local calendar media inputs are unavailable")
    elif command[0] == "docs" and flag == "content":
        normalized = unquote(html.unescape(value))
        if (_DOC_LOCAL_IMAGE.search(normalized) or _DOC_LOCAL_RESOURCE.search(normalized)
                or _DOC_WHITEBOARD_FILE.search(normalized)):
            raise ValueError("Local document media inputs are unavailable")
    elif command[0] == "docs" and flag == "reference-map":
        try:
            pending = [json.loads(value)]
        except (ValueError, RecursionError) as error:
            raise ValueError("Document reference-map must be inline JSON") from error
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                for key, entry in item.items():
                    if key.lower() == "path" and isinstance(entry, str) and entry.strip():
                        raise ValueError("Local document reference-map paths are unavailable")
                    pending.append(entry)
            elif isinstance(item, list):
                pending.extend(item)


def windows_registry_metadata():
    result = {"supported": sys.platform == "win32", "key_exists": None, "value_count": None}
    if not result["supported"]:
        return result
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\LarkCli\keychain\lark-cli", 0,
                            winreg.KEY_QUERY_VALUE) as key:
            # Count entries without enumerating, reading or decrypting credentials.
            result.update(key_exists=True, value_count=winreg.QueryInfoKey(key)[1])
    except FileNotFoundError:
        result.update(key_exists=False, value_count=0)
    except OSError:
        pass
    return result


def cli_environment(data_dir: Path) -> dict[str, str]:
    """Aster's CLI profile directory; no process-global environment mutation."""
    names = ["LARKSUITE_CLI_APP_ID", "LARKSUITE_CLI_APP_SECRET", "LARKSUITE_CLI_USER_ACCESS_TOKEN", "LARKSUITE_CLI_TENANT_ACCESS_TOKEN", "LARKSUITE_CLI_TENANT_ACCESS_TOKEN_SOURCE", "LARKSUITE_CLI_DEFAULT_AS", "LARKSUITE_CLI_PROFILE", "LARKSUITE_CLI_AUTH_PROXY", "LARKSUITE_CLI_PROXY_KEY", "OPENCLAW_CLI", "OPENCLAW_HOME", "OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH", "OPENCLAW_SERVICE_MARKER", "OPENCLAW_SERVICE_VERSION", "OPENCLAW_GATEWAY_PORT", "OPENCLAW_SHELL", "HERMES_HOME", "HERMES_QUIET", "HERMES_EXEC_ASK", "HERMES_GATEWAY_TOKEN", "HERMES_SESSION_KEY", "LARK_CHANNEL"]
    return {**dict.fromkeys(names, ""), "LARKSUITE_CLI_CONFIG_DIR": str(Path(data_dir).resolve() / "feishu-cli")}


def _sanitize(value):
    if isinstance(value, dict):
        return {key: "[redacted]" if _SECRET_KEY.fullmatch(key.replace("-", "_")) else _sanitize(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        value = _BEARER.sub("Bearer [redacted]", value)
        value = _SECRET_TEXT.sub(lambda match: match[1] + "[redacted]", value)
        return _URL_SECRET.sub(lambda match: match[1] + "[redacted]", value)
    return value


def _json_envelope(text):
    try:
        value = json.loads(text.lstrip("\ufeff").strip())
        if isinstance(value, dict) and type(value.get("ok")) is bool:
            return value
    except (ValueError, TypeError, RecursionError):
        pass
    # Progress/upgrade notices may precede a complete one-line error envelope.
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
            if isinstance(value, dict) and type(value.get("ok")) is bool:
                return value
        except (ValueError, TypeError, RecursionError):
            continue
    return None


def _empty_connection(*, installed=True):
    return {"cli_installed": installed, "app_configured": False,
            "user": {"available": False, "token_status": None, "scope_count": None, "scopes": []},
            "bot": {"available": False, "token_status": None},
            "business_access_verified": False, "diagnostic_only": True}


def _connection_projection(raw):
    """Project the CLI management JSON; account IDs and recovery text stay private."""
    identities = raw.get("identities") if isinstance(raw, dict) else None
    if not isinstance(identities, dict):
        raise TypeError("Feishu status is missing identity diagnostics")
    user, bot = identities.get("user"), identities.get("bot")
    if (not isinstance(user, dict) or not isinstance(bot, dict)
            or type(user.get("available")) is not bool or type(bot.get("available")) is not bool):
        raise ValueError("Feishu status identity diagnostics are invalid")
    result = _empty_connection()
    app_id = raw.get("appId")
    configured = isinstance(app_id, str) and bool(app_id.strip())
    result["app_configured"] = configured
    scope = user.get("scope")
    scopes = sorted({part for part in scope.split() if part == "offline_access"
                     or re.fullmatch(r"[a-z][a-z0-9_.]*(?::[a-z0-9_.]+)+", part)}) if isinstance(scope, str) else []
    for name, identity in (("user", user), ("bot", bot)):
        status = identity.get("tokenStatus") or identity.get("status")
        result[name].update(available=identity["available"], token_status=status if status in _TOKEN_STATUSES else None)
    result["user"].update(scopes=scopes, scope_count=len(scopes) if isinstance(scope, str) and scope.strip() else None)
    if configured:
        result["account_fingerprint"] = hashlib.sha256(app_id.encode("utf-8")).hexdigest()[:16]
        user_id = user.get("openId")
        if isinstance(user_id, str) and user_id:
            result["user_binding_fingerprint"] = hashlib.sha256((app_id + ":" + user_id).encode("utf-8")).hexdigest()[:16]
    return result


class FeishuCLITool:
    """Only the application's trusted constructor chooses an executable/profile."""

    def __init__(self, supervisor, data_dir: Path, *, executable=None):
        self.supervisor, self.data_dir = supervisor, Path(data_dir)
        self.executable = Path(executable) if executable is not None else None
        self.spec = ToolSpec(
            "feishu_cli",
            "Use 飞书 Feishu CLI for docs, messages, calendar and sheets. Start with argv=['--help']; "
            "exact argv=['auth','status'] reads local connection diagnostics. Default identity=user accesses personal "
            "resources; use bot only for explicitly requested application resources. For a requested group custom-bot "
            "Webhook (群自定义机器人), use feishu_webhook_send when offered. Keep the requested delivery channel and "
            "supplied message text. Never switch identity after an auth error. Feishu's built-in 飞书助手 is distinct "
            "from a custom app bot. +chat-search returns group/topic chats; "
            "it cannot by itself identify that built-in assistant. Message-search hits do not verify the requested recipient. "
            "Verify the exact target before sending; if unverified, explain the limitation and ask for a target, never guess. "
            "Writes require Runtime approval; auth login/config and raw API are unavailable.",
            {"type": "object", "properties": {
                "argv": {"type": "array", "minItems": 1, "maxItems": 100,
                         "items": {"type": "string", "minLength": 1, "maxLength": 16000}},
                "identity": {"enum": ["user", "bot"]},
                "timeout": {"type": "number", "minimum": 0.1, "maximum": 300},
            }, "required": ["argv"], "additionalProperties": False},
            risk="external", timeout_seconds=315, resource="feishu_cli",
        )

    def _invocation(self, arguments):
        validate(arguments, self.spec.parameters)
        argv = list(arguments["argv"])
        if any("\0" in item for item in argv) or sum(len(item) for item in argv) > 24000:
            raise ValueError("Feishu argv contains NUL or exceeds the bounded command size")
        if argv == ["auth", "status"]:
            return "read", "connection", argv
        if argv in (["--help"], ["--version"]):
            return "read", "metadata", argv
        if len(argv) == 2 and argv[0] in _ROOTS and argv[1] == "--help":
            return "read", "metadata", argv
        if len(argv) == 3 and tuple(argv[:2]) in _COMMANDS and argv[2] == "--help":
            return "read", "metadata", argv
        if argv[0] == "schema" and len(argv) <= 4 and all(
                re.fullmatch(r"[a-z][a-z0-9_.-]*", token) for token in argv[1:]):
            return "read", "metadata", argv
        command = _COMMANDS.get(tuple(argv[:2]))
        if command is None:
            raise ValueError("Unsupported Feishu command; use feishu_cli argv=['--help'] for the supported list")
        risk, value_flags, bool_flags, format_flag = command
        values, switches = set(value_flags.split()), set(bool_flags.split())
        seen = set()
        index = 2
        while index < len(argv):
            token = argv[index]
            if not token.startswith("--"):
                raise ValueError("Feishu business parameters must use supported long flags")
            flag, separator, value = token[2:].partition("=")
            if flag in seen or flag not in values | switches:
                raise ValueError("Unsupported or duplicate Feishu flag; credentials, identity and format are application-owned")
            seen.add(flag)
            if flag in values:
                if not separator:
                    index += 1
                    if index >= len(argv) or argv[index].startswith("--"):
                        raise ValueError("Missing Feishu flag value; use --name=value for text beginning with --")
                    value = argv[index]
                if flag in _FILE_VALUES and (value.startswith("@") or value == "-"):
                    raise ValueError("Feishu file/stdin inputs are unavailable; supply inline text or JSON")
                _reject_implicit_local_media(tuple(argv[:2]), flag, value)
            elif separator and value not in {"true", "false"}:
                raise ValueError("Feishu switch values must be true or false")
            index += 1
        identity = arguments.get("identity", "user")
        if tuple(argv[:2]) == ("docs", "+search") and identity != "user":
            raise ValueError("Feishu docs search requires user identity")
        argv += ["--as", identity]
        if format_flag:
            argv += ["--format", "json"]
        return risk, "business", argv

    def classify(self, arguments):
        try:
            risk, _, _ = self._invocation(arguments)
        except (ValueError, TypeError, ValidationError):
            risk = "external"
        return dataclasses.replace(self.spec, risk=risk, replay_safe=risk == "read")

    def _resolve_executable(self):
        name = "lark-cli.exe" if os.name == "nt" else "lark-cli"
        managed = self.data_dir / "tools" / "feishu-cli" / name
        path = self.executable or managed
        path = Path(path).resolve(strict=True)
        if not path.is_file() or path.suffix.lower() in {".cmd", ".bat", ".ps1", ".js"}:
            raise ValueError("Feishu requires the native lark-cli executable, without a shell or npm wrapper")
        return path

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        if context.cancel.is_set():
            if isinstance(arguments, dict) and arguments.get("argv") == ["auth", "status"]:
                managed = self.data_dir / "tools" / "feishu-cli" / ("lark-cli.exe" if os.name == "nt" else "lark-cli")
                return self._connection_failure("cancelled", "not_started",
                                                installed=(self.executable or managed).is_file(), outcome="not_executed")
            return ToolResult("Cancelled before Feishu execution", True, "cancelled", "not_executed")
        kind = None
        try:
            risk, kind, argv = self._invocation(arguments)
            executable = self._resolve_executable()
        except FileNotFoundError:
            if kind == "connection":
                return self._connection_failure("configuration", "cli_not_installed", installed=False,
                                                outcome="not_executed")
            return ToolResult("Feishu CLI is not installed; run scripts/Install-FeishuCLI.ps1 first",
                              True, "configuration", "not_executed")
        except (ValueError, TypeError, ValidationError, OSError):
            return ToolResult("Invalid or unsupported Feishu invocation. Use inline values, supported commands and "
                              "flags; auth/config, custom identity flags, raw API, files and confirmation bypasses are unavailable.",
                              True, "validation", "not_executed")
        try:
            result = await self.supervisor.run(
                [str(executable), *argv], cwd=self.data_dir.resolve(), artifact_dir=context.artifact_dir,
                cancel=context.cancel, env=cli_environment(self.data_dir), timeout=arguments.get("timeout", 60),
                session_id=context.session_id, persist_output=False,
            )
        except (OSError, RuntimeError, ValueError):
            if kind == "connection":
                return self._connection_failure("process", "not_started", outcome="not_executed")
            return ToolResult("Feishu process could not be started", True, "process", "not_executed")
        started = bool(result.pid)
        outcome = "unknown" if risk != "read" and started else "known" if started else "not_executed"
        if result.error == MEMORY_CAPTURE_LIMIT_ERROR:
            if kind == "connection":
                return self._connection_failure("output_limit", "capture_limit", outcome=outcome)
            return ToolResult("Feishu output exceeded the bounded capture budget. Narrow the query or use pagination; "
                              "verify remote writes before retrying.", True, "output_limit", outcome)
        if result.cancelled or result.timed_out or result.error:
            category = "cancelled" if result.cancelled else "timeout" if result.timed_out else "process"
            if kind == "connection":
                return self._connection_failure(category, "not_completed", outcome=outcome)
            return ToolResult("Feishu command did not complete. " + (
                "Its remote effects require verification before retrying." if outcome == "unknown"
                else "No successful response was received."), True, category, outcome)
        if kind == "connection":
            if result.exit_code == 0:
                try:
                    summary = _connection_projection(json.loads(result.stdout.lstrip("\ufeff").strip()))
                except (ValueError, TypeError, AttributeError, RecursionError):
                    return self._connection_failure("protocol", "invalid_connection_status", outcome=outcome)
                return ToolResult(json.dumps(summary, ensure_ascii=False), data=summary)
            failure = _json_envelope(result.stderr)
            error = (failure or {}).get("error") or {}
            category = error.get("type") if isinstance(error, dict) else None
            subtype = error.get("subtype") if isinstance(error, dict) else None
            return self._connection_failure(category, subtype, outcome=outcome)
        if kind == "metadata" and result.exit_code == 0:
            supported = "Supported Aster shortcuts: " + ", ".join(" ".join(path) for path in _COMMANDS) + ".\n"
            return ToolResult(supported + _sanitize(result.stdout), data={"identity": arguments.get("identity", "user")})
        envelope = _json_envelope(result.stdout) if result.exit_code == 0 else _json_envelope(result.stderr)
        if result.exit_code == 0 and envelope and envelope["ok"] is True:
            if envelope.get("identity", arguments.get("identity", "user")) != arguments.get("identity", "user"):
                return ToolResult("Feishu response identity did not match the requested identity", True, "protocol", outcome)
            safe = _sanitize(envelope)
            return ToolResult(json.dumps(safe, ensure_ascii=False), data=safe)
        if envelope and envelope.get("ok") is False:
            error = envelope.get("error") or {}
            category = error.get("type") if isinstance(error, dict) else "protocol"
            send_rejection_hint = _text_send_rejection_hint(arguments, result, envelope)
            # Fixed CLI 1.0.100 resolves Config before shortcut execution; its
            # not_configured subtype and confirmation gate are pre-dispatch.
            # Other errors can follow partial multi-request writes.
            if ((category == "confirmation" and result.exit_code == 10)
                    or (category == "config" and error.get("subtype") == "not_configured"
                        and result.exit_code == 3)
                    or (isinstance(error, dict) and _is_send_scope_preflight(
                        argv, arguments.get("identity", "user"), result, envelope))
                    or send_rejection_hint is not None):
                outcome = "not_executed"
            safe = _sanitize(envelope)
            if send_rejection_hint is not None:
                safe["error"]["hint"] = send_rejection_hint
                safe["recovery_action"] = "verify_message_target"
            elif isinstance(safe.get("error"), dict) and (category in {"authentication", "authorization"}
                    or (category == "config" and error.get("subtype") == "invalid_client")):
                original_hint = safe["error"].pop("hint", None)
                if original_hint:
                    safe["error"]["human_recovery"] = original_hint
                safe["error"]["hint"] = _AUTH_RECOVERY
                safe["recovery_action"] = "check_connection"
            return ToolResult(json.dumps(safe, ensure_ascii=False), True,
                              category if category in {"validation", "authentication", "authorization", "config",
                                                       "network", "api", "policy", "internal", "confirmation"} else "protocol",
                              outcome, data=safe)
        return ToolResult("Feishu returned an incomplete or invalid JSON envelope. Narrow the query or page the "
                          "read; verify remote writes before retrying.", True, "protocol", outcome)

    async def connection_status(self, context: ToolContext) -> ToolResult:
        """Explicit local check for the workbench; no model or business API call."""
        return await self.execute({"argv": ["auth", "status"], "identity": "user"}, context)

    @staticmethod
    def _connection_failure(category, subtype, *, installed=True, outcome="known"):
        safe = _empty_connection(installed=installed)
        safe["error_type"] = category if isinstance(category, str) and category in {
            "configuration", "config", "process", "protocol", "output_limit", "cancelled", "timeout",
            "authentication", "authorization", "internal", "policy", "network", "api", "validation",
        } else "protocol"
        # Never copy an arbitrary diagnostic field into the public status.
        safe["error_subtype"] = subtype if isinstance(subtype, str) and subtype in _CONNECTION_ERROR_SUBTYPES else None
        return ToolResult(json.dumps(safe, ensure_ascii=False), True, safe["error_type"], outcome, data=safe)
