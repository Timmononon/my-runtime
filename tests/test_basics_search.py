"""Offline basic tools and observed search protocol, without user notifications."""
import asyncio
import base64
import os
import sys
import time

import httpx
import psutil
import pytest

from aster.customization.management import ManagementService
from aster.domain import ToolContext
from aster.engine.fake import FakeModel
from aster.platform.processes import ProcessResult, ProcessSupervisor
from aster.service import RunCoordinator
from aster.tools.basics import BasicTool, NotifyTool, ProcessInspectTool, calculate
from aster.tools.process import ProcessTool
from aster.tools.search import SearchTool


def context(tmp_path, services=None, session="owned"):
    return ToolContext(session, "run", tmp_path, tmp_path / "artifacts", asyncio.Event(), services=services or {})


@pytest.mark.parametrize("expression,expected", [("2 + 3 * 4", 14), ("-(7//2) + 2**5", 29), ("10 % 3 + 0.5", 1.5)])
def test_arithmetic_whitelisted_numeric_operations(expression, expected):
    assert calculate(expression) == expected


@pytest.mark.parametrize("expression", ["__import__('os').system('anything')", "(1).__class__", "[1][0]", "True + 1", "lambda:1", "9**9**9", "1e999", "1/0", "'text'"])
def test_arithmetic_rejects_code_and_unbounded_results(expression):
    with pytest.raises((ValueError, ArithmeticError)):
        calculate(expression)


async def test_time_system_metadata_and_adjustable_arithmetic_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVATE_TOOL_TEST_KEY", "private-should-never-appear")
    ctx = context(tmp_path, {"calculate_budget": {"exponent": 2}})
    result = await BasicTool("calculate").execute({"expression": "2**3"}, ctx)
    assert result.is_error and "budget" in result.content
    result = await BasicTool("current_time").execute({"timezone": "Asia/Shanghai"}, ctx)
    assert result.data["timezone"] == "Asia/Shanghai" and result.data["time"].endswith("+08:00")
    utc = await BasicTool("current_time").execute({}, ctx)
    assert utc.data["timezone"] == "UTC"
    system = await BasicTool("system_info").execute({}, ctx)
    assert not system.is_error and system.data["memory_total"] > 0
    assert "private-should-never-appear" not in system.content and "environment" not in system.data


async def test_managed_process_inspection_identity_kill_and_owner_cleanup(tmp_path):
    supervisor = ProcessSupervisor()
    ctx = context(tmp_path)
    execute = ProcessTool(supervisor)
    try:
        work = asyncio.create_task(execute.execute({"executable": sys.executable,
            "argv": ["-c", "import socket,time; s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); time.sleep(60)"]}, ctx))
        deadline = time.monotonic() + 5
        while not supervisor.inspect(ctx.session_id) and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        row = supervisor.inspect(ctx.session_id)[0]
        assert "cmdline" not in row and "env" not in row
        ports = await ProcessInspectTool(supervisor, "ports").execute({}, ctx)
        assert "cmdline" not in ports.content
        other = context(tmp_path, session="unrelated")
        assert (await ProcessInspectTool(supervisor, "list").execute({}, other)).data["processes"] == []
        denied = await ProcessInspectTool(supervisor, "kill").execute({"pid": row["pid"], "expected_create_time": row["create_time"]}, other)
        assert denied.is_error and psutil.pid_exists(row["pid"])
        reused = await ProcessInspectTool(supervisor, "kill").execute({"pid": row["pid"], "expected_create_time": row["create_time"] - 1}, ctx)
        assert reused.is_error and psutil.pid_exists(row["pid"])
        self_kill = await ProcessInspectTool(supervisor, "kill").execute({"pid": os.getpid(), "expected_create_time": psutil.Process().create_time()}, ctx)
        assert self_kill.is_error
        stopped = await ProcessInspectTool(supervisor, "kill").execute({"pid": row["pid"], "expected_create_time": row["create_time"]}, ctx)
        assert stopped.data["stop_requested"] and stopped.data["confirmed_exited"] is False
        result = await asyncio.wait_for(work, 10)
        assert result.data["cancelled"] and not psutil.pid_exists(row["pid"])
        assert supervisor.inspect(ctx.session_id) == []
    finally:
        await supervisor.close()


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="Windows notification submission preparation")
async def test_notification_uses_safe_payload_and_reports_submission_not_delivery(tmp_path):
    calls = []

    class OfflineSubmitter:
        async def run(self, argv, **kwargs):
            calls.append((argv, kwargs))
            script = base64.b64decode(argv[-1]).decode("utf-16-le")
            assert "ToastNotificationManager" in script and "Show($toast)" in script
            assert "operator private" not in script
            return ProcessResult(0, stdout="ASTER_NOTIFICATION_SUBMITTED\n", pid=1234)

    result = await NotifyTool().execute({"title": "Aster", "message": "operator private <message> & text"},
                                      context(tmp_path, {"process_supervisor": OfflineSubmitter()}))
    assert result.data == {"submitted": True, "delivered": None, "read": None}
    assert len(calls) == 1
    payload = next((tmp_path / "artifacts").glob("*-notification.xml")).read_text()
    assert "&lt;message&gt; &amp; text" in payload


@pytest.mark.parametrize("backend", ["brave", "searxng"])
async def test_real_search_protocol_returns_only_observed_links_and_snippets(backend, tmp_path):
    requests = []
    secret = "offline-search-secret"
    base = "https://api.search.brave.com" if backend == "brave" else "https://search.example"

    def handler(request):
        requests.append(request)
        assert request.url.params["q"] == "test subject"
        assert request.url.path == ("/res/v1/web/search" if backend == "brave" else "/search")
        assert request.headers["X-Subscription-Token" if backend == "brave" else "Authorization"] == (secret if backend == "brave" else "Bearer " + secret)
        rows = [{"title": "Observed source", "url": "https://example.org/paper", "description": "Real snippet " + secret, "content": "Real snippet " + secret},
                {"title": "Other", "url": "https://example.org/second", "description": "Other snippet", "content": "Other snippet"}]
        return httpx.Response(200, json={"web": {"results": rows}} if backend == "brave" else {"results": rows})

    ctx = context(tmp_path, {"web_search_config": {"backend": backend, "base_url": base,
        "credential_ref": "keyring:aster/search", "result_limit": 1}, "http_credential_origins": {"keyring:aster/search": [base]},
        "secret_resolver": lambda ref: secret, "search_client_factory": lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))})
    result = await SearchTool().execute({"query": "test subject"}, ctx)
    assert not result.is_error and len(requests) == 1
    assert result.data["results"][0]["url"] == "https://example.org/paper"
    assert result.data["results"][0]["title"] == "Observed source" and result.data["truncated"]
    assert secret not in result.content and "[redacted]" in result.content


@pytest.mark.parametrize("status", [302, 401, 429, 503])
async def test_search_http_failure_no_echo_no_redirect_no_retry(status, tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"location": "https://attacker.invalid"}, json={"error": "private-search-key Authorization secret query"})

    ctx = context(tmp_path, {"web_search_config": {"backend": "searxng", "base_url": "https://search.example"},
        "search_client_factory": lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))})
    result = await SearchTool().execute({"query": "private secret query"}, ctx)
    assert result.is_error and len(calls) == 1 and "private-search-key" not in result.content
    assert "private secret query" not in result.content


async def test_search_disabled_or_changed_origin_never_sends_old_key(tmp_path):
    calls = []
    disabled = await SearchTool().execute({"query": "test"}, context(tmp_path))
    assert disabled.error_category == "unavailable"
    ctx = context(tmp_path, {"web_search_config": {"backend": "searxng", "base_url": "https://changed.example", "credential_ref": "keyring:aster/search"},
        "http_credential_origins": {"keyring:aster/search": ["https://old.example"]}, "secret_resolver": lambda ref: "old-private-key",
        "search_client_factory": lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda req: calls.append(req)))})
    result = await SearchTool().execute({"query": "test"}, ctx)
    assert result.is_error and calls == [] and "old-private-key" not in result.content


async def test_search_cancellation_closes_stream_and_does_not_retry(tmp_path):
    began, closed = asyncio.Event(), asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            began.set()
            try:
                await asyncio.Event().wait()
                yield b"never"
            finally:
                closed.set()

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=Stream())

    tool = SearchTool(client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    ctx = context(tmp_path, {"web_search_config": {"backend": "searxng", "base_url": "https://search.example"}})
    work = asyncio.create_task(tool.execute({"query": "test"}, ctx))
    await asyncio.wait_for(began.wait(), 1)
    ctx.cancel.set()
    result = await asyncio.wait_for(work, 1)
    assert result.error_category == "cancelled" and closed.is_set() and len(calls) == 1


async def test_real_search_dispatcher_management_binding_and_journal_never_contain_key(tmp_path):
    coordinator = RunCoordinator(tmp_path / "data", model_factory=lambda settings: FakeModel([]), browser_enabled=False)
    calls = []
    secret = "offline-real-search-key"
    encoded_secret = "".join(f"%{ord(char):02X}" for char in secret)

    def handler(request):
        calls.append(request)
        assert request.headers["x-subscription-token"] == secret
        return httpx.Response(200, json={"web": {"results": [
            {"title": "Literal URL leak", "url": "https://example.org/?ordinary=" + secret},
            {"title": "Encoded URL query leak", "url": "https://example.org/?ordinary=" + encoded_secret},
            {"title": "Encoded URL path leak", "url": "https://example.org/" + encoded_secret},
            {"title": "Observed " + secret + " " + encoded_secret, "url": "https://example.org/source",
             "description": "Useful " + secret + " " + encoded_secret},
        ]}})

    try:
        manager = ManagementService(coordinator)
        credential = manager.save_credential(manager.revision(), "search", secret)
        manager.update(manager.revision(), {"web_search": {"backend": "brave", "base_url": "https://api.search.brave.com",
                                                         "credential_ref": credential["credential_ref"]}})
        tool = next(tool for tool in coordinator.base_tools if tool.spec.name == "web_search")
        tool.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
        sid = coordinator.create_session(tmp_path)["id"]
        denied = await coordinator.execute_session_tool(sid, "web_search", {"query": "test"}, approved=False)
        assert denied.is_error and calls == []
        accepted = await coordinator.execute_session_tool(sid, "web_search", {"query": "test"}, approved=True)
        assert not accepted.is_error and len(calls) == 1
        assert accepted.data["results"][0]["url"] == "https://example.org/source"
        assert len(accepted.data["results"]) == 1
        from aster.state.store import dumps
        assert secret not in dumps(coordinator.store.events(session_id=sid))
        assert secret not in dumps(coordinator.store.history(sid))
        run = coordinator.store.list_runs(sid)[0]
        assert coordinator.store.tool_executions(run["id"])[0]["risk"] == "external"
        assert secret not in dumps(coordinator.store.tool_executions(run["id"]))
        for records in (accepted.content, dumps(coordinator.store.events(session_id=sid)),
                        dumps(coordinator.store.history(sid)), dumps(coordinator.store.tool_executions(run["id"]))):
            assert encoded_secret not in records and secret not in records
    finally:
        await coordinator.close()
