"""Fixed-destination Feishu webhook delivery with disposable state and no network."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import closing
from types import SimpleNamespace

import httpx
import pytest

from aster.customization.credentials import CredentialError, CredentialStore
from aster.customization.discovery import ToolSearchTool
from aster.domain import Message, ModelTurn, ToolCall, ToolContext, to_dict
from aster.engine import FakeModel
from aster.engine.dispatcher import Dispatcher
from aster.policy import ApprovalBroker, PermissionPolicy
from aster.service import RunCoordinator
from aster.state import Store
from aster.tools import feishu_webhook
from aster.tools.factory import ToolFactory
from aster.tools.registry import Registry

TOOL_NAME = "feishu_webhook_send"
HOST = "open.feishu.cn"
PREFIX = f"https://{HOST}/open-apis/bot/v2/hook/"
TOKEN = "00000000-0000-4000-8000-000000000001"
REPLACEMENT_TOKEN = "00000000-0000-4000-8000-000000000002"
URL = PREFIX + TOKEN
REPLACEMENT = PREFIX + REPLACEMENT_TOKEN
GROUP = "离线测试群"
MESSAGE = {"text": "仅供测试的飞书消息"}


@pytest.fixture
def offline_feishu(monkeypatch):
    network = SimpleNamespace(resolutions=[], requests=[], after_pin=None, respond=None)

    async def pin(url, allowed_hosts, **kwargs):
        network.resolutions.append((url, allowed_hosts))
        if network.after_pin is not None:
            network.after_pin()
        return httpx.URL("https://8.8.8.8/"), HOST, HOST

    async def receive(request):
        network.requests.append(request)
        if network.respond is not None:
            return network.respond(request)
        return httpx.Response(200, json={"code": 0, "StatusCode": 0, "msg": URL})

    def transport(*, trust_env, retries=0):
        assert trust_env is False and retries == 0
        return httpx.MockTransport(receive)

    def reject_generic_client(*args, **kwargs):
        pytest.fail("Webhook tests must use only the offline pinned transport")

    monkeypatch.setattr(feishu_webhook, "pin_url", pin)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport)
    monkeypatch.setattr(httpx, "AsyncClient", reject_generic_client)
    monkeypatch.setattr(httpx, "Client", reject_generic_client)
    return network


@pytest.fixture
def runtime(tmp_path, offline_feishu):
    data_dir = tmp_path / "state"
    feishu_webhook.configure_feishu_webhook(data_dir, GROUP, URL)
    tool = feishu_webhook.FeishuWebhookTool.from_local_credentials(data_dir)
    assert tool is not None
    store = Store(tmp_path / "runtime.db")
    session_id = store.create_session(tmp_path)["id"]
    run, _ = store.create_run(session_id)
    context = ToolContext(session_id, run["id"], tmp_path, tmp_path / "artifacts", asyncio.Event())
    broker = ApprovalBroker(store, timeout_seconds=3)
    engine = Dispatcher(Registry([tool]), store, PermissionPolicy(), broker)
    try:
        yield data_dir, engine, broker, store, context
    finally:
        context.cancel.set()
        store.close()


async def test_local_cli_setup_registers_discoverable_webhook_and_keeps_secret_private(
        tmp_path, monkeypatch, capsys, offline_feishu):
    from aster.cli import main

    data_dir = tmp_path / "state"
    factory = ToolFactory(data_dir, browser_enabled=False)
    try:
        assert Registry(factory.tools).get(TOOL_NAME) is None
        assert not data_dir.exists()
    finally:
        await factory.close()

    monkeypatch.setattr("aster.cli.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("aster.cli.getpass.getpass", lambda prompt: URL)
    assert main(["--data-dir", str(data_dir), "feishu-webhook", "configure", "--group", GROUP]) == 0
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert not (data_dir / "config.yaml").exists()

    credentials = CredentialStore("aster-feishu-webhook", data_dir=data_dir)
    assert json.loads(credentials.get("destination", use_environment=False)) == {"group": GROUP, "url": URL}
    for resolver, reference in (
        (CredentialStore(data_dir=data_dir), "keyring:aster-feishu-webhook/destination"),
        (CredentialStore(data_dir=data_dir), "keyring:ASTER-FEISHU-WEBHOOK/destination"),
        (credentials, "keyring:destination"),
    ):
        with pytest.raises(CredentialError) as denied:
            resolver.resolve(reference)
        assert TOKEN not in str(denied.value)

    factory = ToolFactory(data_dir, browser_enabled=False)
    try:
        registry = Registry(factory.tools)
        tool = registry.get(TOOL_NAME)
        assert tool is not None
        assert tool.spec.risk == "external" and not tool.spec.replay_safe
        assert set(tool.spec.parameters["properties"]) == {"text"}
        assert tool.spec.parameters["required"] == ["text"]
        assert tool.spec.parameters["additionalProperties"] is False
        assert GROUP in tool.spec.description
        context = ToolContext("offline", "offline", tmp_path, tmp_path / "artifacts", asyncio.Event(),
                              services={"registry": registry, "active_tools": set()})
        discovery = await ToolSearchTool().execute({"query": "select:" + TOOL_NAME}, context)
        assert [item["name"] for item in discovery.data["tools"]] == [TOOL_NAME]
        assert TOKEN not in json.dumps(to_dict([tool.spec, discovery]))
        assert not offline_feishu.resolutions and not offline_feishu.requests
    finally:
        await factory.close()


@pytest.mark.parametrize("setup", ["configured", "absent", "denied", "outside-allowlist", "selected-skill"])
async def test_existing_session_offers_allowed_webhook_on_first_model_request(tmp_path, offline_feishu, setup):
    data_dir = tmp_path / "state"
    prior_call = ToolCall("prior-app-bot", "feishu_cli", {
        "argv": ["im", "+messages-send", "--chat-id", "oc_offline", "--text", "上次通知"],
        "identity": "bot",
    })
    prior_messages = [
        Message("user", "向飞书助手发送通知。"),
        Message("assistant", tool_calls=[prior_call]),
        Message("tool", '{"ok":false,"error":{"code":230002}}', tool_call_id=prior_call.id),
        Message("assistant", "飞书应用机器人不在目标聊天中，消息未发送。"),
    ]
    with closing(Store(data_dir / "runtime.db")) as store:
        session_id = store.create_session(tmp_path)["id"]
        for message in prior_messages:
            store.append_message(session_id, None, message)

    if setup != "absent":
        feishu_webhook.configure_feishu_webhook(data_dir, GROUP, URL)
    policy = {
        "denied": f"permissions:\n  denied_tools: [{TOOL_NAME}]\n",
        "outside-allowlist": "permissions:\n  allowed_tools: [file_read, tool_search]\n",
    }.get(setup)
    if policy:
        (data_dir / "config.yaml").write_text(policy, encoding="utf-8")
    selected_skill = "read-only" if setup == "selected-skill" else None
    if selected_skill:
        skill = data_dir / "skills" / selected_skill / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: read-only\nallowed-tools: [file_read]\n---\nRead only.\n", encoding="utf-8")

    model = FakeModel([ModelTurn("离线验证完成。")])
    coordinator = RunCoordinator(data_dir, model_factory=lambda _: model, browser_enabled=False)
    try:
        run = await coordinator.submit(session_id, "请用群机器人发送这次通知。", selected_skill=selected_skill)
        outcome = await coordinator.wait(run["id"], timeout=5)
        assert outcome.status == "completed", outcome.error
        first_request, = model.calls
        assert any(message.role == "tool" and "230002" in message.content for message in first_request["messages"])
        offered = {spec.name: spec for spec in first_request["tools"]}
        if setup == "configured":
            tool, = (tool for tool in coordinator.base_tools if isinstance(tool, feishu_webhook.FeishuWebhookTool))
            assert offered[TOOL_NAME] == tool.spec
            assert GROUP in tool.spec.description
            assert set(tool.spec.parameters["properties"]) == {"text"}
            assert tool.spec.parameters["properties"]["text"]["type"] == "string"
            assert tool.spec.parameters["required"] == ["text"]
            assert tool.spec.parameters["additionalProperties"] is False
        else:
            assert TOOL_NAME not in offered
        assert TOKEN not in json.dumps(to_dict(first_request))
        assert coordinator.store.tool_executions(run["id"]) == []
        assert not offline_feishu.resolutions and not offline_feishu.requests
    finally:
        await coordinator.close()


@pytest.mark.parametrize(("group", "url"), [
    (GROUP, URL.replace("https://", "http://")),
    (GROUP, URL.replace(HOST, HOST + ".invalid")),
    (GROUP, URL.replace(HOST, HOST + "@elsewhere.invalid")),
    (GROUP, URL.replace(HOST, HOST + ":444")),
    (GROUP, URL + "?forward=1"),
    (GROUP, URL + "#fragment"),
    (GROUP, URL + "/extra"),
    (GROUP, PREFIX + "not-a-webhook-token"),
    (URL, URL),
    (TOKEN, URL),
])
def test_invalid_destination_cannot_replace_valid_credentials(runtime, group, url, offline_feishu):
    data_dir, _, _, _, _ = runtime
    with pytest.raises(ValueError) as error:
        feishu_webhook.configure_feishu_webhook(data_dir, group, url)
    assert TOKEN not in str(error.value)
    value = CredentialStore("aster-feishu-webhook", data_dir=data_dir).get("destination", use_environment=False)
    assert json.loads(value) == {"group": GROUP, "url": URL}
    assert not offline_feishu.resolutions and not offline_feishu.requests


@pytest.mark.parametrize("decision", ["approve", "deny", "replace_during_approval", "replace_during_dns"])
async def test_dispatch_waits_for_approval_and_binds_destination(runtime, offline_feishu, caplog, decision):
    data_dir, engine, broker, store, context = runtime
    context.services["http_allowed_hosts"] = [HOST]
    caplog.set_level(logging.DEBUG)
    validations = (await engine.execute([ToolCall("override", TOOL_NAME, {**MESSAGE, "url": REPLACEMENT})],
                                        context))[0]
    assert validations.error_category == "validation" and validations.outcome == "not_executed"

    waiting = asyncio.Event()
    requests = []

    async def emit(kind, payload):
        if kind == "approval.waiting":
            requests.append(payload)
            waiting.set()

    def accepted(_):
        entry, = store.tool_executions(context.run_id)
        assert entry["state"] == "dispatching"
        assert entry["arguments"] == MESSAGE
        return httpx.Response(200, json={"code": 0, "StatusCode": 0, "msg": URL})

    def replace_destination():
        feishu_webhook.configure_feishu_webhook(data_dir, "另一个离线群", REPLACEMENT)

    offline_feishu.respond = accepted
    context.emit = emit
    task = asyncio.create_task(engine.execute([ToolCall("send", TOOL_NAME, MESSAGE)], context))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        assert not offline_feishu.resolutions and not offline_feishu.requests
        assert store.tool_executions(context.run_id) == []
        request, = requests
        assert request["arguments"] == MESSAGE and request["tool_name"] == TOOL_NAME
        pending = store.get_pending(request["request_id"])
        assert pending["state"] == "pending" and pending["payload"]["arguments"] == MESSAGE
        if decision == "replace_during_approval":
            replace_destination()
        elif decision == "replace_during_dns":
            offline_feishu.after_pin = replace_destination
        assert broker.resolve(request["request_id"], decision != "deny")
        assert not broker.resolve(request["request_id"], True)
        result, = await task
        executions = store.tool_executions(context.run_id)
        if decision == "approve":
            assert not result.is_error and result.outcome == "known", result.content
            assert offline_feishu.resolutions == [(f"https://{HOST}", set())]
            post, = offline_feishu.requests
            assert post.method == "POST" and post.url == httpx.URL(URL).copy_with(host="8.8.8.8")
            assert post.headers["host"] == HOST and post.extensions["sni_hostname"] == HOST
            assert json.loads(post.content) == {"msg_type": "text", "content": MESSAGE}
            entry, = executions
            assert entry["state"] == "committed" and entry["risk"] == "external"
            assert not entry["replay_safe"]
        else:
            assert result.is_error and result.outcome == "not_executed", result.content
            assert not offline_feishu.requests
            assert bool(offline_feishu.resolutions) == (decision == "replace_during_dns")
            if decision == "deny":
                assert executions == []
            else:
                entry, = executions
                assert entry["state"] == "failed_no_effect"
        events = store.events(run_id=context.run_id)
        visible = json.dumps(to_dict([requests, pending, executions, events, validations, result])) + caplog.text
        assert TOKEN not in visible and REPLACEMENT_TOKEN not in visible
        assert not result.artifacts
    finally:
        context.cancel.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(("status", "body", "outcome", "state"), [
    pytest.param(200, {"code": 9499}, "not_executed", "failed_no_effect", id="bad-format"),
    pytest.param(200, {"code": 19021}, "not_executed", "failed_no_effect", id="signature-rejection"),
    pytest.param(200, {"code": 19022}, "not_executed", "failed_no_effect", id="ip-rejection"),
    pytest.param(200, {"code": 19024}, "not_executed", "failed_no_effect", id="keyword-rejection"),
    pytest.param(200, {"code": 11232}, "not_executed", "failed_no_effect", id="rate-limit-rejection"),
    pytest.param(200, {"code": False}, "unknown", "outcome_unknown", id="boolean-success-code"),
    pytest.param(200, {"code": "0"}, "unknown", "outcome_unknown", id="string-success-code"),
    pytest.param(200, {"StatusCode": 0}, "unknown", "outcome_unknown", id="missing-code"),
    pytest.param(200, {"code": 0, "StatusCode": 19024}, "unknown", "outcome_unknown", id="conflicting-codes"),
    pytest.param(200, {"code": 10001}, "unknown", "outcome_unknown", id="unrecognized-code"),
    pytest.param(200, None, "unknown", "outcome_unknown", id="invalid-receipt"),
    pytest.param(307, {"code": 0}, "unknown", "outcome_unknown", id="redirect"),
    pytest.param(400, {"code": 0}, "unknown", "outcome_unknown", id="http-client-failure"),
    pytest.param(502, {"code": 0}, "unknown", "outcome_unknown", id="upstream-failure"),
    pytest.param(None, None, "unknown", "outcome_unknown", id="receive-timeout"),
])
async def test_receipt_classification_controls_retry_barrier(runtime, offline_feishu, status, body, outcome, state):
    _, engine, broker, store, context = runtime
    approvals = []

    async def approve(kind, payload):
        if kind == "approval.waiting":
            approvals.append(payload["request_id"])
            assert broker.resolve(payload["request_id"], True)

    def incomplete(request):
        if status is None:
            raise httpx.ReadTimeout(URL, request=request)
        headers = {"location": REPLACEMENT} if status == 307 else {}
        if body is None:
            return httpx.Response(status, content=f"<html>{URL}</html>", headers=headers)
        return httpx.Response(status, json={**body, "msg": URL}, headers=headers)

    context.emit = approve
    offline_feishu.respond = incomplete
    result, = await engine.execute([ToolCall("first", TOOL_NAME, MESSAGE)], context)
    assert result.is_error and result.outcome == outcome, result.content
    entry, = store.tool_executions(context.run_id)
    assert entry["state"] == state
    assert TOKEN not in json.dumps(to_dict([result, entry]))
    assert len(offline_feishu.requests) == 1 and len(approvals) == 1
    if outcome == "unknown":
        blocked, = await engine.execute([ToolCall("new-id", TOOL_NAME, MESSAGE)], context)
        assert blocked.is_error and blocked.outcome == "not_executed"
        assert len(offline_feishu.requests) == 1 and len(approvals) == 1
        assert store.tool_executions(context.run_id) == [entry]
    else:
        assert not store.has_unresolved(context.session_id)
