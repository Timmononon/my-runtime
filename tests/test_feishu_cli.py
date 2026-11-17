"""Offline Feishu CLI contract, boundaries and real native process execution."""
import asyncio
import hashlib
import json
import os
import sys

import pytest

from aster.domain import ToolContext
from aster.platform.processes import ProcessResult, ProcessSupervisor
from aster.tools.feishu import FeishuCLITool, cli_environment


class RecordingSupervisor:
    def __init__(self, result=None):
        self.result = result or ProcessResult(0, json.dumps({"ok": True, "identity": "user", "data": {"found": 1}}), pid=42)
        self.calls = []

    async def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return self.result


def context(tmp_path):
    cwd = tmp_path / "workspace"
    cwd.mkdir(exist_ok=True)
    return ToolContext("session", "run", cwd, tmp_path / "artifacts", asyncio.Event())


def data_directory(tmp_path):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    return state


def tool(tmp_path, result=None):
    supervisor = RecordingSupervisor(result)
    return FeishuCLITool(supervisor, data_directory(tmp_path), executable=sys.executable), supervisor


def test_construction_is_offline_and_does_not_create_state(tmp_path, monkeypatch):
    monkeypatch.setattr(FeishuCLITool, "_resolve_executable", lambda *args: pytest.fail("constructor resolved a binary"))
    item = FeishuCLITool(RecordingSupervisor(), tmp_path / "absent")
    assert item.spec.name == "feishu_cli" and "飞书" in item.spec.description
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("argv,risk", [
    (["--help"], "read"), (["docs", "--help"], "read"),
    (["docs", "+fetch", "--doc", "doc_x"], "read"),
    (["docs", "+search", "--query", "work"], "read"),
    (["im", "+messages-search", "--query", "work"], "read"),
    (["im", "+chat-messages-list", "--chat-id", "oc_x"], "read"),
    (["calendar", "+agenda"], "read"),
    (["sheets", "+cells-get", "--range", "A1:B2"], "read"),
    (["docs", "+create", "--content", "hello"], "external"),
    (["docs", "+update", "--command", "overwrite"], "external"),
    (["im", "+messages-send", "--text", "hello"], "external"),
    (["calendar", "+create", "--summary", "event"], "external"),
    (["sheets", "+cells-set", "--cells", '[[{"value":"hello"}]]'], "external"),
])
def test_exact_read_classification_preserves_write_approval(tmp_path, argv, risk):
    item, _ = tool(tmp_path)
    effective = item.classify({"argv": argv})
    assert effective.risk == risk and effective.replay_safe == (risk == "read")
    assert effective.parameters == item.spec.parameters
    assert not effective.concurrency_safe


@pytest.mark.parametrize("argv", [
    ["auth", "login"], ["config", "show"], ["profile", "use", "other"],
    ["api", "POST", "/open-apis/im/v1/messages"], ["event", "subscribe"],
    ["docs", "+fetch", "--as", "bot"], ["docs", "+fetch", "--format", "pretty"],
    ["docs", "+fetch", "--profile", "other"], ["docs", "+fetch", "--jq", ".data"],
    ["docs", "+create", "--content", "@secret.json"],
    ["docs", "+create", "--content", "-"],
    ["docs", "+update", "--yes"],
    ["im", "+chat-messages-list", "--download-resources"],
    ["sheets", "+cells-get", "--output-path", "out.json"],
    ["docs", "+fetch", "--doc", "first", "--doc", "second"],
    ["docs", "+fetch", "--doc", "\0"],
    ["docs", "+fetch", "--help", "--profile", "other"],
])
async def test_unsafe_or_unsupported_invocations_never_start(tmp_path, argv):
    item, runner = tool(tmp_path)
    result = await item.execute({"argv": argv}, context(tmp_path))
    assert result.is_error and result.outcome == "not_executed"
    assert not runner.calls


async def test_identity_json_and_config_directory_are_application_owned(tmp_path, monkeypatch):
    monkeypatch.setenv("LARKSUITE_CLI_APP_SECRET", "synthetic-ambient-secret")
    monkeypatch.setenv("OPENCLAW_HOME", str(tmp_path / "other-agent"))
    item, runner = tool(tmp_path)
    result = await item.execute({"argv": ["calendar", "+agenda"], "identity": "user"}, context(tmp_path))
    assert not result.is_error and result.data["ok"] is True
    argv, options = runner.calls[0]
    assert argv[-4:] == ["--as", "user", "--format", "json"]
    assert options["env"]["LARKSUITE_CLI_CONFIG_DIR"] == str(tmp_path / "state" / "feishu-cli")
    assert options["env"]["LARKSUITE_CLI_APP_SECRET"] == options["env"]["OPENCLAW_HOME"] == ""
    assert options["cwd"] == item.data_dir.resolve()
    assert not options["persist_output"] and not result.artifacts
    assert os.environ["LARKSUITE_CLI_APP_SECRET"] == "synthetic-ambient-secret"


@pytest.mark.parametrize("argv", [
    ["docs", "+fetch", "--doc", "doc_x"],
    ["docs", "+create", "--title", "test", "--content", "hello"],
    ["docs", "+update", "--doc", "doc_x", "--command", "append", "--content", "hello"],
])
async def test_fixed_json_doc_commands_do_not_receive_unsupported_format_flags(tmp_path, argv):
    item, runner = tool(tmp_path)
    result = await item.execute({"argv": argv}, context(tmp_path))
    assert not result.is_error
    assert runner.calls[0][0][-2:] == ["--as", "user"]


@pytest.mark.parametrize("process", [
    ProcessResult(0, '{"code":0,"data":{}}', pid=42),
    ProcessResult(0, '{"ok":true', pid=42),
    ProcessResult(1, '{"ok":true,"data":{}}', pid=42),
    ProcessResult(4, stderr='{"ok":false,"error":{"type":"network","message":"timeout"}}', pid=42),
    ProcessResult(1, stderr='{"ok":false,"error":{"type":"api","message":"second request failed"}}', pid=42),
    ProcessResult(1, stderr='{"ok":false,"error":{"type":"validation","message":"later block invalid"}}', pid=42),
    ProcessResult(None, timed_out=True, pid=42),
    ProcessResult(None, cancelled=True, pid=42),
])
async def test_started_writes_with_ambiguous_outcomes_never_report_known_failure(tmp_path, process):
    item, runner = tool(tmp_path, process)
    result = await item.execute({"argv": ["docs", "+create", "--title", "test", "--content", "hello"]}, context(tmp_path))
    assert result.is_error and result.outcome == "unknown"
    assert len(runner.calls) == 1


async def test_confirmation_gate_is_not_replayed_or_bypassed(tmp_path):
    process = ProcessResult(10, stderr='{"ok":false,"error":{"type":"confirmation","hint":"append --yes"}}', pid=42)
    item, runner = tool(tmp_path, process)
    result = await item.execute({"argv": ["docs", "+update", "--command", "overwrite"]}, context(tmp_path))
    assert result.is_error and result.outcome == "not_executed"
    assert len(runner.calls) == 1 and "--yes" not in runner.calls[0][0]


async def test_read_errors_remain_errors_without_claiming_tool_success(tmp_path):
    process = ProcessResult(3, stderr='{"ok":false,"error":{"type":"config","subtype":"not_configured"}}', pid=42)
    item, _ = tool(tmp_path, process)
    result = await item.execute({"argv": ["calendar", "+agenda"]}, context(tmp_path))
    assert result.is_error and result.outcome == "not_executed" and result.error_category == "config"


async def test_proven_missing_app_preflight_does_not_create_unknown_write_barrier(tmp_path):
    process = ProcessResult(3, stderr='{"ok":false,"error":{"type":"config","subtype":"not_configured"}}', pid=42)
    item, _ = tool(tmp_path, process)
    result = await item.execute({"argv": ["docs", "+create", "--content", "hello"]}, context(tmp_path))
    assert result.is_error and result.outcome == "not_executed" and result.error_category == "config"


async def test_missing_executable_and_precancellation_do_not_dispatch(tmp_path, monkeypatch):
    # A native file in ambient PATH must not replace the managed, pinned CLI.
    alternative = tmp_path / "ambient-bin"
    alternative.mkdir()
    (alternative / ("lark-cli.exe" if os.name == "nt" else "lark-cli")).write_text("not the managed CLI")
    monkeypatch.setenv("PATH", str(alternative))
    runner = RecordingSupervisor()
    item = FeishuCLITool(runner, tmp_path / "state")
    ctx = context(tmp_path)
    missing = await item.execute({"argv": ["calendar", "+agenda"]}, ctx)
    assert missing.error_category == "configuration" and missing.outcome == "not_executed"
    ctx.cancel.set()
    cancelled = await item.execute({"argv": ["calendar", "+agenda"]}, ctx)
    assert cancelled.error_category == "cancelled" and not runner.calls


async def test_cli_secret_fields_and_bearer_values_never_enter_result(tmp_path):
    process = ProcessResult(4, stderr=json.dumps({"ok": False, "error": {
        "type": "network", "access_token": "synthetic-token-should-disappear", "appSecret": "synthetic-app-secret",
        "message": "Authorization: Bearer synthetic-bearer-secret; quoted={'app_secret':'synthetic-quoted-secret'}",
        "device_code": "synthetic-device-secret",
        "hint": "https://example.test/login?device_code=synthetic-url-secret"}}), pid=42)
    item, _ = tool(tmp_path, process)
    result = await item.execute({"argv": ["calendar", "+agenda"]}, context(tmp_path))
    serialized = json.dumps(result.data) + result.content
    assert "[redacted]" in serialized
    assert all(secret not in serialized for secret in ["synthetic-token-should-disappear", "synthetic-app-secret",
        "synthetic-bearer-secret", "synthetic-device-secret", "synthetic-url-secret", "synthetic-quoted-secret"])
    assert not result.artifacts


async def test_real_native_argv_is_literal_and_raw_output_is_not_persisted(tmp_path):
    ctx = context(tmp_path)
    (data_directory(tmp_path) / "docs").write_text(
        "import json,sys;print(json.dumps({'ok':True,'identity':'user','data':{'argv':sys.argv[1:]}}))",
        encoding="utf-8")
    payload = "literal ; & $(touch injected) 中文\n--profile other"
    runner = ProcessSupervisor()
    item = FeishuCLITool(runner, tmp_path / "state", executable=sys.executable)
    try:
        result = await item.execute({"argv": ["docs", "+create", "--title", "test", "--content", payload]}, ctx)
        assert not result.is_error, result.content
        assert result.data["data"]["argv"] == ["+create", "--title", "test", "--content", payload, "--as", "user"]
        assert not (ctx.cwd / "injected").exists()
        assert not result.artifacts and not list(ctx.artifact_dir.iterdir())
    finally:
        await runner.close()


async def test_real_native_started_write_timeout_is_unknown_and_leaves_no_raw_output(tmp_path):
    ctx = context(tmp_path)
    (data_directory(tmp_path) / "docs").write_text("import time; print('Bearer synthetic-raw-secret',flush=True);time.sleep(30)", encoding="utf-8")
    runner = ProcessSupervisor()
    item = FeishuCLITool(runner, tmp_path / "state", executable=sys.executable)
    try:
        result = await item.execute({"argv": ["docs", "+create", "--content", "hello"], "timeout": 0.15}, ctx)
        assert result.is_error and result.error_category == "timeout" and result.outcome == "unknown"
        assert "synthetic-raw-secret" not in result.content
        assert not result.artifacts and not list(ctx.artifact_dir.iterdir())
    finally:
        await runner.close()


async def test_real_document_json_larger_than_display_preview_is_complete(tmp_path):
    ctx = context(tmp_path)
    (data_directory(tmp_path) / "docs").write_text(
        "import json;print(json.dumps({'ok':True,'identity':'user','data':{'document':{'content':'x'*75000}}}))",
        encoding="utf-8")
    runner = ProcessSupervisor()
    item = FeishuCLITool(runner, tmp_path / "state", executable=sys.executable)
    try:
        result = await item.execute({"argv": ["docs", "+fetch", "--doc", "doc_x", "--doc-format", "markdown"]}, ctx)
        assert not result.is_error, result.content
        assert len(result.data["data"]["document"]["content"]) == 75000
        assert not list(ctx.artifact_dir.iterdir()) and not result.artifacts
    finally:
        await runner.close()


async def test_in_memory_capture_limit_rejects_truncated_json_without_claiming_success(tmp_path):
    ctx = context(tmp_path)
    (data_directory(tmp_path) / "docs").write_text(
        "import json;print(json.dumps({'ok':True,'identity':'user','data':{'text':'x'*3000}}))", encoding="utf-8")
    runner = ProcessSupervisor(output_bytes=1024)
    item = FeishuCLITool(runner, tmp_path / "state", executable=sys.executable)
    try:
        result = await item.execute({"argv": ["docs", "+fetch", "--doc", "doc_x"]}, ctx)
        assert result.is_error and result.error_category == "output_limit" and result.outcome == "known"
        assert "data" not in result.data and not list(ctx.artifact_dir.iterdir())
    finally:
        await runner.close()


def test_cli_environment_is_independent_of_process_cwd(tmp_path):
    environment = cli_environment(tmp_path / "state")
    assert environment["LARKSUITE_CLI_CONFIG_DIR"] == str((tmp_path / "state" / "feishu-cli").resolve())
    assert environment["LARKSUITE_CLI_TENANT_ACCESS_TOKEN_SOURCE"] == ""


def status_payload(*, available=True):
    return {
        "appId": "cli_private_app_fixture", "appSecret": "synthetic-private-app-secret",
        "userName": "synthetic-private-user-name", "verification_url": "https://example.test/private-auth-link",
        "identities": {
            "user": {"available": available, "status": "ready" if available else "not_authorized",
                     "tokenStatus": "valid" if available else "expired", "openId": "ou_private_user_fixture",
                     "userName": "synthetic-private-user-name", "accessToken": "synthetic-private-access-token",
                     "scope": "calendar:calendar.event:read docx:document:readonly calendar:calendar.event:read",
                     "hint": "https://example.test/private-user-auth-link"},
            "bot": {"available": True, "status": "ready", "appName": "synthetic-private-app-name",
                    "hint": "https://example.test/private-bot-auth-link"},
        },
    }


def test_only_exact_connection_status_is_read_classified(tmp_path):
    item, _ = tool(tmp_path)
    assert item.classify({"argv": ["auth", "status"]}).risk == "read"
    assert item.classify({"argv": ["auth", "status"]}).replay_safe
    assert item.classify({"argv": ["auth", "status", "--verify"]}).risk == "external"


@pytest.mark.parametrize("argv", [
    ["auth", "login"], ["auth", "logout"], ["auth", "check", "--scope", "calendar:calendar.event:read"],
    ["auth", "status", "--verify"], ["auth", "status", "--json"], ["auth", "status", "--as", "bot"],
    ["auth", "status", "--profile", "other"], ["auth", "status", "--help"],
])
async def test_connection_entry_never_opens_other_auth_actions_or_extra_flags(tmp_path, argv):
    item, supervisor = tool(tmp_path)
    result = await item.execute({"argv": argv}, context(tmp_path))
    assert result.is_error and result.outcome == "not_executed" and not supervisor.calls


@pytest.mark.parametrize("available", [True, False])
async def test_raw_management_status_is_projected_without_private_identity_or_auth_links(tmp_path, available):
    raw = status_payload(available=available)
    item, supervisor = tool(tmp_path, ProcessResult(0, json.dumps(raw), pid=42))
    result = await item.connection_status(context(tmp_path))
    assert not result.is_error, result.content
    assert result.data == json.loads(result.content)
    assert result.data["cli_installed"] and result.data["app_configured"]
    assert result.data["user"] == {
        "available": available, "token_status": "valid" if available else "expired", "scope_count": 2,
        "scopes": ["calendar:calendar.event:read", "docx:document:readonly"],
    }
    assert result.data["bot"] == {"available": True, "token_status": "ready"}
    assert not result.data["business_access_verified"] and result.data["diagnostic_only"]
    assert result.data["account_fingerprint"] == hashlib.sha256(raw["appId"].encode()).hexdigest()[:16]
    assert result.data["user_binding_fingerprint"] == hashlib.sha256(
        (raw["appId"] + ":" + raw["identities"]["user"]["openId"]).encode()).hexdigest()[:16]
    assert all(text not in result.content for text in [
        "cli_private_app_fixture", "ou_private_user_fixture", "synthetic-private", "example.test", "verification_url",
    ])
    argv, options = supervisor.calls[0]
    assert argv[1:] == ["auth", "status"]
    assert not options["persist_output"] and len(supervisor.calls) == 1


async def test_connection_status_without_scope_metadata_does_not_invent_grants(tmp_path):
    raw = status_payload()
    del raw["identities"]["user"]["scope"]
    raw["identities"]["user"]["tokenStatus"] = "synthetic_private_status_value"
    item, _ = tool(tmp_path, ProcessResult(0, json.dumps(raw), pid=42))
    result = await item.connection_status(context(tmp_path))
    assert result.data["user"]["scope_count"] is None and result.data["user"]["scopes"] == []
    assert result.data["user"]["token_status"] is None
    assert "synthetic_private_status_value" not in result.content


@pytest.mark.parametrize("process,error_type,error_subtype", [
    (ProcessResult(0, '{"appId":"private-app","identities":{}}', pid=42), "protocol", "invalid_connection_status"),
    (ProcessResult(0, '{"ok":true,"data":{"secret":"private-response"}}', pid=42), "protocol", "invalid_connection_status"),
    (ProcessResult(3, stderr='{"ok":false,"error":{"type":"config","subtype":"not_configured",'
                               '"hint":"https://example.test/private-auth-link"}}', pid=42), "config", "not_configured"),
    (ProcessResult(5, stderr='{"ok":false,"error":{"type":"internal","subtype":"private_user_name",'
                               '"appSecret":"private-secret"}}', pid=42), "internal", None),
    (ProcessResult(5, stderr='{"ok":false,"error":{"type":{"name":"private-secret"},"subtype":[]}}', pid=42),
     "protocol", None),
    (ProcessResult(None, timed_out=True, pid=42), "timeout", "not_completed"),
])
async def test_connection_failures_return_only_safe_diagnostic_contract(tmp_path, process, error_type, error_subtype):
    item, _ = tool(tmp_path, process)
    result = await item.connection_status(context(tmp_path))
    assert result.is_error and result.data["diagnostic_only"] and not result.data["business_access_verified"]
    assert result.data["error_type"] == error_type and result.data["error_subtype"] == error_subtype
    assert not any(text in result.content for text in ["private-app", "private-response", "example.test", "private-secret", "private_user_name"])


async def test_missing_cli_connection_status_uses_safe_contract(tmp_path):
    item = FeishuCLITool(RecordingSupervisor(), tmp_path / "state")
    result = await item.connection_status(context(tmp_path))
    assert result.is_error and result.outcome == "not_executed"
    assert result.data["cli_installed"] is False and result.data["error_subtype"] == "cli_not_installed"


async def test_connection_precancellation_is_safe_and_never_spawns(tmp_path):
    item, supervisor = tool(tmp_path)
    ctx = context(tmp_path)
    ctx.cancel.set()
    result = await item.connection_status(ctx)
    assert result.is_error and result.outcome == "not_executed"
    assert result.data["error_type"] == "cancelled" and result.data["diagnostic_only"]
    assert not supervisor.calls


async def test_connection_keeps_public_offline_access_scope(tmp_path):
    payload = status_payload()
    payload["identities"]["user"]["scope"] = "offline_access calendar:calendar.event:read"
    item, _ = tool(tmp_path, ProcessResult(0, json.dumps(payload), pid=42))
    result = await item.connection_status(context(tmp_path))
    assert result.data["user"]["scope_count"] == 2
    assert result.data["user"]["scopes"] == ["calendar:calendar.event:read", "offline_access"]


async def test_real_native_management_json_is_private_and_not_persisted(tmp_path):
    ctx = context(tmp_path)
    (data_directory(tmp_path) / "auth").write_text("print(" + repr(json.dumps(status_payload())) + ")", encoding="utf-8")
    supervisor = ProcessSupervisor()
    item = FeishuCLITool(supervisor, tmp_path / "state", executable=sys.executable)
    try:
        result = await item.connection_status(ctx)
        assert not result.is_error and result.data["user"]["available"]
        assert result.data["diagnostic_only"] and not result.data["business_access_verified"]
        assert not any(value in result.content for value in ["cli_private_app_fixture", "ou_private_user_fixture",
                                                            "synthetic-private", "example.test"])
        assert not result.artifacts and not list(ctx.artifact_dir.iterdir())
    finally:
        await supervisor.close()


@pytest.mark.parametrize("category,subtype,code", [
    ("authentication", "token_missing", None), ("authorization", "missing_scope", None),
    ("config", "invalid_client", 20140),
])
async def test_business_auth_failure_stays_error_and_guides_local_check_without_identity_fallback(tmp_path, category, subtype, code):
    error = {"type": category, "subtype": subtype, "hint": "run lark-cli auth login --scope calendar:calendar.event:read"}
    if code is not None:
        error["code"] = code
    item, supervisor = tool(tmp_path, ProcessResult(3, stderr=json.dumps({"ok": False, "identity": "user", "error": error}), pid=42))
    result = await item.execute({"argv": ["calendar", "+agenda"], "identity": "user"}, context(tmp_path))
    assert result.is_error and result.data["ok"] is False and result.error_category == category
    assert result.data["error"]["subtype"] == subtype and result.data["error"].get("code") == code
    assert result.data["recovery_action"] == "check_connection"
    assert "当前运行上下文" in result.data["error"]["hint"] and "不要自动改成 bot" in result.data["error"]["hint"]
    assert "auth login --scope" in result.data["error"]["human_recovery"]
    assert len(supervisor.calls) == 1 and supervisor.calls[0][0][-4:] == ["--as", "user", "--format", "json"]


async def test_project_cwd_never_changes_business_or_connection_process_context(tmp_path):
    item, supervisor = tool(tmp_path)
    contexts = []
    for name in ["project-a", "project-b"]:
        project = tmp_path / name
        project.mkdir()
        contexts.append(ToolContext("s", "r", project, tmp_path / "artifacts", asyncio.Event()))
    for ctx in contexts:
        supervisor.result = ProcessResult(0, '{"ok":true,"identity":"user","data":[]}', pid=42)
        business = await item.execute({"argv": ["calendar", "+agenda"], "identity": "user"}, ctx)
        supervisor.result = ProcessResult(0, json.dumps(status_payload()), pid=42)
        connection = await item.connection_status(ctx)
        assert not business.is_error and not connection.is_error
    assert len(supervisor.calls) == 4
    assert all(options["cwd"] == item.data_dir.resolve() for _, options in supervisor.calls)
    assert all(options["env"] == cli_environment(item.data_dir) for _, options in supervisor.calls)
    assert supervisor.calls[0][0] == supervisor.calls[2][0]
    assert supervisor.calls[1][0] == supervisor.calls[3][0]


async def test_real_native_business_call_uses_existing_trusted_directory_across_projects(tmp_path):
    state = data_directory(tmp_path)
    (state / "calendar").write_text(
        "import json,os;print(json.dumps({'ok':True,'identity':'user','data':"
        "{'cwd':os.getcwd(),'config_dir':os.environ.get('LARKSUITE_CLI_CONFIG_DIR')}}))", encoding="utf-8")
    supervisor = ProcessSupervisor()
    item = FeishuCLITool(supervisor, state, executable=sys.executable)
    try:
        observed = []
        for name in ["project-a", "project-b"]:
            project = tmp_path / name
            project.mkdir()
            ctx = ToolContext("s", "r", project, tmp_path / "artifacts", asyncio.Event())
            result = await item.execute({"argv": ["calendar", "+agenda"]}, ctx)
            assert not result.is_error, result.content
            observed.append(result.data["data"])
        assert observed == [{"cwd": str(state.resolve()), "config_dir": str(state / "feishu-cli")}] * 2
        assert not list((tmp_path / "artifacts").iterdir())
    finally:
        await supervisor.close()


@pytest.mark.parametrize("argv", [
    ["calendar", "+create", "--description", "A calendar image ![photo](./private.png)"],
    ["calendar", "+create", "--description", "A calendar image ![photo](file:///private.png)"],
    ["docs", "+create", "--doc-format", "markdown", "--content", "A document image ![photo](@private.png)"],
    ["docs", "+update", "--content", '<img path="@private.png"/>'],
    ["docs", "+update", "--content", '<source path="@credentials.yaml" name="copy.yaml"/>'],
    ["docs", "+create", "--content", '<whiteboard type="svg" path="@private.svg"/>'],
    ["docs", "+create", "--content", '<whiteboard type="svg">@private.svg</whiteboard>'],
    ["docs", "+create", "--doc-format", "markdown", "--content", r'![a[b]c](\@private.png)'],
    ["docs", "+create", "--doc-format", "markdown", "--content", '![photo](&#64;private.png)'],
    ["docs", "+create", "--doc-format", "markdown", "--content", '![photo](%40private.png)'],
    ["docs", "+create", "--content", "Inline", "--reference-map", '{"html5_block":{"r1":{"path":"@private.html"}}}'],
    ["docs", "+update", "--content", "Inline", "--reference-map", '{"html5_block":{"r1":{"PATH":"private.html"}}}'],
])
async def test_implicit_local_media_never_reads_from_trusted_runtime_directory(tmp_path, argv):
    item, supervisor = tool(tmp_path)
    result = await item.execute({"argv": argv}, context(tmp_path))
    assert result.is_error and result.outcome == "not_executed" and not supervisor.calls


@pytest.mark.parametrize("argv", [
    ["calendar", "+create", "--description", "![remote](https://example.test/image.png)"],
    ["calendar", "+create", "--description", "Read [ordinary](./notes.md) and [web](https://example.test)"],
    ["docs", "+create", "--doc-format", "markdown", "--content", "![remote](https://example.test/image.png)"],
    ["docs", "+create", "--content", '<img href="https://example.test/image.png"/>'],
    ["docs", "+create", "--doc-format", "markdown", "--content", "Read [ordinary](@notes.md), then [web](https://example.test)"],
    ["docs", "+create", "--content", "Inline", "--reference-map", '{"html5_block":{"r1":{"data":"<p>Inline HTML</p>"}}}'],
])
async def test_inline_text_and_supported_remote_media_keep_existing_command_support(tmp_path, argv):
    item, supervisor = tool(tmp_path)
    result = await item.execute({"argv": argv}, context(tmp_path))
    assert not result.is_error and len(supervisor.calls) == 1
    assert supervisor.calls[0][1]["cwd"] == item.data_dir.resolve()
