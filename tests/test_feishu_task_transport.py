"""Managed Feishu discovery and event handling without a live subscription."""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from contextlib import aclosing, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from aster.feishu import transport as module
from aster.feishu.transport import FeishuEventTransport, FeishuTransportError

_EVENT = {
    "type": "im.message.receive_v1",
    "id": "om_sample",
    "message_id": "om_sample",
    "event_id": "sample-delivery",
    "sender_id": "ou_operator",
    "sender_type": "user",
    "chat_id": "oc_sample",
    "chat_type": "group",
    "message_type": "text",
    "create_time": "1780000000000",
    "content": "@Offline Bot 请处理这个问题",
    "mentions": [{"id": "ou_bot", "key": "@_user_1", "name": "Offline Bot"}],
}

_FIXTURE_CLI = r"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

mode = os.environ['ASTER_TEST_FEISHU_MODE']
argv = sys.argv[1:]
ready = '[event] ready event_key=im.message.receive_v1'
event = json.loads(os.environ['ASTER_TEST_FEISHU_EVENT'])

def emit(value, stream=sys.stdout):
    print(json.dumps(value, ensure_ascii=True), file=stream, flush=True)

def envelope(value, identity='bot'):
    emit({'ok': True, 'identity': identity, 'data': value})

def wait_gate(name):
    gate = Path(os.environ['ASTER_TEST_FEISHU_EXIT']).with_name(name)
    while not gate.exists():
        time.sleep(0.01)

if argv == ['--version']:
    print('lark-cli version ' + ('1.0.99' if mode == 'wrong_version' else '1.0.100'))
elif argv[:2] == ['auth', 'status']:
    emit({'appId': 'cli_wrong' if mode == 'wrong_app' else 'cli_current', 'brand': 'feishu',
          'credentialSource': 'env' if mode == 'override' else 'local',
          'identities': {'bot': {'available': mode != 'no_bot'},
                         'user': {} if mode == 'bot_only' else {'openId': 'ou_operator'}},
          'access_token': 'raw-token-must-not-escape'})
elif argv[:3] == ['api', 'GET', '/open-apis/bot/v3/info']:
    envelope({'open_id': 'ou_bot', 'app_name': 'Offline Bot', 'activate_status': 2},
             'user' if mode == 'wrong_bot_identity' else 'bot')
elif argv[:3] == ['api', 'GET', '/open-apis/im/v1/chats']:
    opts = json.loads(argv[argv.index('--params') + 1])
    if mode == 'group_scope_missing':
        emit({'ok': False, 'error': {'type': 'authorization', 'subtype': 'missing_scope',
                                     'message': 'secret-marker'}}, sys.stderr)
        sys.exit(3)
    if mode == 'oversized_response':
        sys.stdout.buffer.write(b'x' * (513 * 1024))
    elif opts.get('page_token') and mode != 'cursor_loop':
        envelope({'items': [{'chat_id': 'oc_sample', 'name': 'Duplicate', 'chat_mode': 'group'},
                            {'chat_id': 'oc_topic', 'name': '测试 Topic', 'chat_mode': 'topic'}], 'has_more': False})
    else:
        envelope({'items': [{'chat_id': 'oc_sample', 'name': '测试群', 'chat_mode': 'group'},
                            {'chat_id': 'oc_private', 'name': 'Direct chat', 'chat_mode': 'p2p'}],
                  'has_more': True, 'page_token': 'cursor-page-2'})
elif argv[:3] == ['event', 'consume', 'im.message.receive_v1']:
    print('[event] untrusted stderr diagnostic secret-marker', file=sys.stderr, flush=True)
    if mode == 'setup_error':
        print(json.dumps({'ok': False, 'error': {'type': 'authorization', 'subtype': 'missing_scope',
                                                 'message': 'secret-marker'}}, indent=2), file=sys.stderr)
        sys.exit(3)
    if mode == 'no_ready':
        emit(event)
    else:
        print(ready, file=sys.stderr, flush=True)
        if mode == 'oversized_record':
            sys.stdout.buffer.write(b'x' * (513 * 1024) + b'\n')
        elif mode == 'duplicate_json':
            print('{"type":"im.message.receive_v1","content":"secret-marker","content":"duplicate"}')
        elif mode == 'partial_record':
            sys.stdout.write('{"content":"secret-marker"}')
            sys.exit(0)
        elif mode == 'wrong_record_type':
            emit({'ok': True, 'data': {'secret': 'secret-marker'}})
        elif mode == 'backpressure':
            print('WARN: consume backpressure, dropped oldest event secret-marker', file=sys.stderr)
        else:
            if mode == 'descendant':
                child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
                event['fixture_child_pid'] = child.pid
            emit(event)
            if mode == 'reconnect':
                wait_gate('release-disconnect')
                print('[source] feishu-websocket: disconnected (secret-marker)', file=sys.stderr, flush=True)
                wait_gate('release-reconnect')
                print('[source] feishu-websocket: connected', file=sys.stderr, flush=True)
                wait_gate('release-next-event')
                emit({**event, 'id': 'om_next', 'message_id': 'om_next'})
    sys.stdin.buffer.read()
    Path(os.environ['ASTER_TEST_FEISHU_EXIT']).touch()
else:
    sys.exit(2)
"""


@pytest.fixture
def fixture_cli(tmp_path, monkeypatch):
    data = tmp_path / "runtime"
    exe = data / "tools" / "feishu-cli" / ("lark-cli.exe" if os.name == "nt" else "lark-cli")
    config = data / "feishu-cli" / "config.json"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"Stat fixture, process runs the offline Python CLI")
    config.parent.mkdir()
    profile = {
        "currentApp": "selected-demo",
        "apps": [{"appId": "cli_other", "brand": "feishu"},
                 {"name": "selected-demo", "appId": "cli_current", "brand": "feishu",
                  "appSecret": "local-fixture-value", "users": [{"userOpenId": "ou_operator"}]}],
    }
    config.write_text(json.dumps(profile), encoding="utf-8")
    done = tmp_path / "stdin-eof"
    monkeypatch.setenv("ASTER_TEST_FEISHU_EVENT", json.dumps(_EVENT))
    monkeypatch.setenv("ASTER_TEST_FEISHU_MODE", "normal")
    monkeypatch.setenv("ASTER_TEST_FEISHU_EXIT", str(done))
    monkeypatch.setenv("LARKSUITE_CLI_APP_ID", "cli_ambient")
    monkeypatch.setenv("LARKSUITE_CLI_CONFIG_DIR", str(tmp_path / "ambient"))
    monkeypatch.setenv("LARKSUITE_CLI_NEW_OVERRIDE", "ambient")
    monkeypatch.setenv("OPENCLAW_HOME", str(tmp_path / "openclaw"))
    calls = []
    pids = []
    owned_process = module._owned_process
    runner = Path(sys.base_prefix) / "python.exe" if os.name == "nt" else Path(sys.executable)

    @asynccontextmanager
    async def fixture_process(argv, *, cwd, env, streaming=False):
        # Preserve actual pipe, cancellation and Job behavior. Replace only
        # the native program's bytes with a fixture CLI in disposable state.
        assert Path(argv[0]) == exe and Path(cwd) == data
        assert env["LARKSUITE_CLI_CONFIG_DIR"] == str(config.parent)
        assert env["LARKSUITE_CLI_APP_ID"] == env["OPENCLAW_HOME"] == ""
        assert "LARKSUITE_CLI_NEW_OVERRIDE" not in env
        if os.name == "nt":
            assert env["SYSTEMROOT"] == os.environ["SYSTEMROOT"]
        calls.append(argv[1:])
        async with owned_process([str(runner), "-u", "-c", _FIXTURE_CLI, *argv[1:]], cwd=cwd,
                                 env=env, streaming=streaming) as process:
            pids.append(process.pid)
            yield process

    monkeypatch.setattr(module, "_owned_process", fixture_process)
    return SimpleNamespace(transport=FeishuEventTransport(data), data=data, config=config, profile=profile,
                           calls=calls, pids=pids, stdin_eof=done, runner=runner)


def _identity(fixture) -> dict:
    return {"app_id": "cli_current", "bot_open_id": "ou_bot", "generation": fixture.transport.current_generation()}


def test_generation_is_offline_scoped_and_invalidates_on_managed_profile_change(fixture_cli):
    fixture = fixture_cli
    first = fixture.transport.current_generation()
    assert re.fullmatch(r"[a-f0-9]{64}", first)
    assert not fixture.calls and not fixture.stdin_eof.exists()
    fixture.profile["currentApp"] = "cli_other"
    fixture.config.write_text(json.dumps(fixture.profile), encoding="utf-8")
    assert fixture.transport.current_generation() != first
    fixture.profile["currentApp"] = "nonexistent"
    fixture.config.write_text(json.dumps(fixture.profile), encoding="utf-8")
    with pytest.raises(FeishuTransportError) as failure:
        fixture.transport.current_generation()
    assert failure.value.code == "profile_invalid"


def test_missing_cli_and_missing_app_config_have_different_actions(tmp_path):
    transport = FeishuEventTransport(tmp_path)
    with pytest.raises(FeishuTransportError) as no_cli:
        transport.current_generation()
    assert no_cli.value.code == "cli_unavailable"
    assert "Install-FeishuCLI.ps1" in str(no_cli.value)
    executable = tmp_path / "tools" / "feishu-cli" / ("lark-cli.exe" if os.name == "nt" else "lark-cli")
    executable.parent.mkdir(parents=True)
    executable.touch()
    with pytest.raises(FeishuTransportError) as no_config:
        transport.current_generation()
    assert no_config.value.code == "profile_invalid"
    assert "config.json" in str(no_config.value)


@pytest.mark.parametrize(("entry_count", "reason"), [(0, "windows_credentials_empty"),
                         (4, "bot_credentials_unavailable"), (None, "bot_credentials_unavailable")])
async def test_unavailable_bot_is_reported_before_any_group_or_subscription_call(fixture_cli, monkeypatch,
                                                                                  entry_count, reason):
    monkeypatch.setenv("ASTER_TEST_FEISHU_MODE", "no_bot")
    monkeypatch.setattr(module, "windows_registry_metadata", lambda: {"value_count": entry_count})
    with pytest.raises(FeishuTransportError) as failure:
        await fixture_cli.transport.discover()
    assert failure.value.code == reason and failure.value.status == "setup_required"
    assert "凭据" in str(failure.value) and "订阅" not in str(failure.value)
    assert "raw-token" not in str(failure.value)
    assert [args[0] for args in fixture_cli.calls] == ["--version", "auth"]
    if entry_count == 0:
        assert "Windows 用户" in str(failure.value) and "退出并停止后台" in str(failure.value)


@pytest.mark.parametrize("mode", ["normal", "bot_only"])
async def test_discovery_pins_current_bot_and_projects_verified_joined_groups(fixture_cli, monkeypatch, mode):
    fixture = fixture_cli
    monkeypatch.setenv("ASTER_TEST_FEISHU_MODE", mode)
    result = await fixture.transport.discover()
    assert result == {
        "identity": {**_identity(fixture), "bot_name": "Offline Bot",
                     "user_open_id": None if mode == "bot_only" else "ou_operator"},
        "groups": [{"chat_id": "oc_sample", "name": "测试群"}, {"chat_id": "oc_topic", "name": "测试 Topic"}],
    }
    assert fixture.calls == [
        ["--version"],
        ["auth", "status", "--profile=cli_current"],
        ["api", "GET", "/open-apis/bot/v3/info", "--as", "bot", "--profile=cli_current"],
        ["api", "GET", "/open-apis/im/v1/chats", "--as", "bot", "--params",
         '{"page_size": 100}', "--profile=cli_current"],
        ["api", "GET", "/open-apis/im/v1/chats", "--as", "bot", "--params",
         '{"page_size": 100, "page_token": "cursor-page-2"}', "--profile=cli_current"],
    ]
    assert set(fixture.data.rglob("*")) == {
        fixture.config, fixture.config.parent, fixture.data / "tools",
        fixture.data / "tools/feishu-cli", fixture.data / "tools/feishu-cli" / (
            "lark-cli.exe" if os.name == "nt" else "lark-cli"
        ),
    }


@pytest.mark.parametrize(("mode", "status", "calls"), [
    ("wrong_version", "setup_required", 1), ("wrong_app", "setup_required", 2),
    ("override", "setup_required", 2), ("wrong_bot_identity", "disconnected", 3),
    ("group_scope_missing", "setup_required", 4),
    ("cursor_loop", "disconnected", 5), ("oversized_response", "disconnected", 4),
])
async def test_discovery_fails_closed_with_bounded_reads(fixture_cli, monkeypatch, mode, status, calls):
    monkeypatch.setenv("ASTER_TEST_FEISHU_MODE", mode)
    with pytest.raises(FeishuTransportError) as failure:
        await fixture_cli.transport.discover()
    assert failure.value.status == status
    assert "raw-token" not in str(failure.value)
    if mode == "group_scope_missing":
        assert failure.value.code == "scope_missing" and "权限" in str(failure.value)
        assert "订阅" not in str(failure.value) and "secret-marker" not in str(failure.value)
    elif mode in ("wrong_app", "override"):
        assert failure.value.code == "profile_mismatch"
    assert len(fixture_cli.calls) == calls


async def test_discovery_checks_generation_after_await(fixture_cli, monkeypatch):
    fixture = fixture_cli
    bot_get = fixture.transport._bot_get

    async def rotate(*args):
        data = await bot_get(*args)
        fixture.profile["currentApp"] = "cli_other"
        fixture.config.write_text(json.dumps(fixture.profile), encoding="utf-8")
        return data

    monkeypatch.setattr(fixture.transport, "_bot_get", rotate)
    with pytest.raises(FeishuTransportError) as failure:
        await fixture.transport.discover()
    assert failure.value.code == "identity_changed"
    assert len(fixture.calls) == 3  # Never query groups under an invalidated binding.


@pytest.mark.parametrize(("mode", "status"), [
    ("no_ready", "disconnected"), ("setup_error", "setup_required"),
    ("oversized_record", "disconnected"), ("duplicate_json", "disconnected"),
    ("partial_record", "disconnected"), ("wrong_record_type", "disconnected"), ("backpressure", "disconnected"),
])
async def test_stream_errors_are_bounded_and_do_not_expose_cli_output(fixture_cli, monkeypatch, caplog, mode, status):
    monkeypatch.setenv("ASTER_TEST_FEISHU_MODE", mode)
    monkeypatch.setattr(module, "_STARTUP_TIMEOUT", 1)
    states = []
    events = fixture_cli.transport.events(_identity(fixture_cli), lambda *state: states.append(state))
    async with aclosing(events):
        with pytest.raises(FeishuTransportError) as failure:
            await asyncio.wait_for(anext(events), timeout=8)
    assert states[0] == ("connecting", None)
    assert states[-1][0] == failure.value.status == status
    assert "secret-marker" not in str(failure.value) + str(states) + caplog.text
    if mode in {"setup_error", "no_ready"}:
        assert "listening" not in [state[0] for state in states]
    if mode == "setup_error":
        assert failure.value.code == "scope_missing" and states[-1][1] == "scope_missing"
    assert all(not psutil.pid_exists(pid) for pid in fixture_cli.pids)


async def test_idle_stream_stops_on_profile_change(fixture_cli, monkeypatch):
    fixture = fixture_cli
    monkeypatch.setattr(module, "_GENERATION_INTERVAL", 0.05)
    states = []
    events = fixture.transport.events(_identity(fixture), lambda *state: states.append(state))
    async with aclosing(events):
        assert await asyncio.wait_for(anext(events), timeout=5) == _EVENT
        fixture.profile["currentApp"] = "cli_other"
        fixture.config.write_text(json.dumps(fixture.profile), encoding="utf-8")
        with pytest.raises(FeishuTransportError) as failure:
            await asyncio.wait_for(anext(events), timeout=5)
    assert failure.value.code == "identity_changed"
    assert [state[0] for state in states] == ["connecting", "listening", "setup_required"]
    assert fixture.stdin_eof.exists()


async def test_stream_tracks_connection_changes_without_restarting_the_cli(fixture_cli, monkeypatch):
    fixture = fixture_cli
    monkeypatch.setenv("ASTER_TEST_FEISHU_MODE", "reconnect")
    states = asyncio.Queue()
    events = fixture.transport.events(_identity(fixture), lambda *state: states.put_nowait(state))
    async with aclosing(events):
        assert await asyncio.wait_for(anext(events), timeout=5) == _EVENT
        assert [states.get_nowait()[0] for _ in range(2)] == ["connecting", "listening"]
        pending = asyncio.create_task(anext(events))
        try:
            (fixture.stdin_eof.with_name("release-disconnect")).touch()
            assert await asyncio.wait_for(states.get(), timeout=5) == ("disconnected", module._DISCONNECTED)
            assert not pending.done()
            (fixture.stdin_eof.with_name("release-reconnect")).touch()
            assert await asyncio.wait_for(states.get(), timeout=5) == ("listening", None)
            (fixture.stdin_eof.with_name("release-next-event")).touch()
            assert (await asyncio.wait_for(pending, timeout=5))["message_id"] == "om_next"
        finally:
            await module._cancel_tasks(pending)
    assert len(fixture.calls) == 2  # One version check and one consume process.


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="Windows Job ownership of detached descendants")
async def test_windows_cancellation_closes_stdin_and_kills_only_owned_processes(fixture_cli, monkeypatch):
    fixture = fixture_cli
    monkeypatch.setenv("ASTER_TEST_FEISHU_MODE", "descendant")
    unrelated = await asyncio.create_subprocess_exec(str(fixture.runner), "-c", "import time;time.sleep(60)",
                                                     creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        states = []
        events = fixture.transport.events(_identity(fixture), lambda *state: states.append(state))
        async with aclosing(events):
            last = await asyncio.wait_for(anext(events), timeout=5)
            child = last.pop("fixture_child_pid")
            assert last == _EVENT
            assert psutil.pid_exists(child)
            pending = asyncio.create_task(anext(events))
            await asyncio.sleep(0)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        assert fixture.calls[-1] == ["event", "consume", _EVENT["type"], "--as", "bot", "--profile=cli_current"]
        assert states == [("connecting", None), ("listening", None)]
        assert fixture.stdin_eof.exists()
        async with asyncio.timeout(5):
            while psutil.pid_exists(child):
                await asyncio.sleep(0.025)
        assert all(not psutil.pid_exists(pid) for pid in fixture.pids)
        assert unrelated.returncode is None
    finally:
        unrelated.terminate()
        await asyncio.wait_for(unrelated.wait(), timeout=5)
