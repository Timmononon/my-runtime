"""Real anonymous GitHub Worker reads plus a broker-approved LOCAL draft.

All Runtime state and conversation history are temporary. Remote methods other
than GET and ambient provider/GitHub credentials are forbidden. No model turn,
publication, branch mutation, clone or push is performed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import socket
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import httpx

from aster.api import create_app
from aster.engine import FakeModel
from aster.github.normalize import event, number
from aster.github.normalize import repository as checked_repository
from aster.github.transport import GithubTransport
from aster.service import RunCoordinator


class NoCredentials:
    """Never consult a user file, environment credential, or OS keychain."""

    storage = "isolated-no-credentials"

    def get(self, *args, **kwargs):
        return None

    def resolve(self, reference):
        raise ValueError("Live read-only verification has no credentials")

    def set(self, *args, **kwargs):
        raise AssertionError("Live read-only verification cannot save credentials")

    def delete(self, *args, **kwargs):
        raise AssertionError("Live read-only verification cannot mutate credentials")


class ReadOnlyGithubTransport(GithubTransport):
    async def handle_async_request(self, request):
        assert request.method == "GET", "Remote writes are forbidden"
        assert "authorization" not in request.headers, "GitHub reads must be anonymous"
        return await super().handle_async_request(request)


async def browser_evidence(app, coordinator, workspace, worker_title, screenshot, port):
    """Render actual persisted tool messages; no history/API interception."""
    import uvicorn
    from playwright.async_api import async_playwright, expect

    base_url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False))
    serving = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(0.02)
        async with httpx.AsyncClient(base_url=base_url, trust_env=False,
                                     headers={"Authorization": "Bearer " + app.state.verifier_token}) as client:
            launched = await client.post("/ui/tickets", json={"cwd": str(workspace)})
            assert launched.status_code == 201, "Temporary UI ticket creation failed"
            launch_ticket = launched.json()["ticket"]
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 1440, "height": 1400})
            try:
                page = await context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(type(error).__name__))
                await page.goto(base_url + "/#ticket=" + launch_ticket)
                await expect(page.get_by_role("textbox", name="输入消息")).to_be_visible()
                await page.get_by_role("button", name=worker_title, exact=True).click()
                await expect(page.get_by_text("已返回", exact=True)).to_have_count(4)
                await expect(page.get_by_text("返回错误", exact=True)).to_have_count(0)
                for summary in await page.locator("details.tool-group > summary").all():
                    await summary.click()
                await expect(page.locator("details.tool-group[open]")).to_have_count(4)
                for returned in await page.get_by_text("已返回", exact=True).all():
                    await expect(returned).to_be_visible()
                assert not errors, "Temporary UI encountered an error"
                assert not await page.evaluate("document.documentElement.scrollWidth > innerWidth + 1")
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(screenshot), full_page=True, animations="disabled")
                return {"passed": True, "actual_canonical_history": True, "history_intercepted": False,
                        "tool_success_labels": 4, "tool_error_labels": 0, "screenshot": str(screenshot)}
            finally:
                await context.close()
                await browser.close()
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, timeout=10)
        assert coordinator.github._runner is None and coordinator.tasks._runner is None


async def verify(repository, issue_number, report, *, static_root=None, screenshot=None):
    repository = checked_repository(repository)
    issue_number = number(issue_number)
    findings = {"passed": False, "repository": repository, "issue_number": issue_number,
                "checked_at": datetime.now(UTC).isoformat(), "anonymous": True,
                "user_credentials_used": False, "user_state_modified": False,
                "remote_mutations": 0, "model_requests": 0, "paid_calls": 0,
                "remote_requests": [], "remote_responses": [], "tool_results": [],
                "runtime_state": "fully temporary; removed on completion"}
    stage = "initialize"
    generate_calls = []
    prefix = "/repos/" + repository.casefold()

    async def request_guard(request):
        assert request.method == "GET", "Live verification forbids all remote mutations"
        assert request.url.scheme == "https" and request.url.host == "api.github.com"
        assert request.url.path.casefold() == prefix or request.url.path.casefold().startswith(prefix + "/")
        assert "authorization" not in request.headers, "Live verification must remain anonymous"
        findings["remote_requests"].append({"method": request.method, "path": request.url.path})

    async def observe_response(response):
        findings["remote_responses"].append({"path": response.request.url.path, "status": response.status_code})

    def github_client():
        return httpx.AsyncClient(transport=ReadOnlyGithubTransport(), trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(30, connect=15),
            event_hooks={"request": [request_guard], "response": [observe_response]})

    def forbidden_generation(messages, tools):
        generate_calls.append(True)
        raise AssertionError("Live read-only verification must never generate a model turn")

    try:
        with tempfile.TemporaryDirectory(prefix="aster-github-worker-live-readonly-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            coordinator = RunCoordinator(root / "state", tools=[], credentials=NoCredentials(),
                model_factory=lambda settings: FakeModel([forbidden_generation]), github_client_factory=github_client,
                browser_enabled=False)
            token = secrets.token_urlsafe(32)
            browser_port = None
            if screenshot:
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    browser_port = listener.getsockname()[1]
            origins = {f"http://127.0.0.1:{browser_port}"} if browser_port else None
            app = create_app(coordinator, token=token, manage_lifecycle=False, static_root=static_root,
                             allowed_origins=origins)
            app.state.verifier_token = token
            action = None
            try:
                owner = coordinator.create_session(workspace, title="Temporary anonymous GitHub verification")
                source = coordinator.github.create_source(owner["id"], repository, enabled=True, mode="tasks",
                                                          allow_writes=False)
                assert not source["configured"]
                # Read real API metadata before binding; no model or source polling.
                stage = "read_real_issue_metadata"
                observed = await coordinator.github.read(source["id"], "issue", issue_number)
                raw_issue = observed["data"]
                assert raw_issue["number"] == issue_number and raw_issue["user"]["type"] == "User"
                incoming = event("issue", raw_issue, repository)
                source = coordinator.github.update_source(source["id"], source["revision"],
                                                          allowed_actors=[incoming["actor"]])
                worker_title = "GitHub 工具真实只读验收"
                task = coordinator.tasks.create_ticket(owner["id"], "Live GitHub tool verification",
                    "Read the configured repository and Issue; prepare only a local review draft.",
                    workers=[{"title": worker_title, "goal": "Read GitHub and prepare a local draft only."}],
                    request_id="live-readonly-verification", unattended=True,
                    admission=coordinator.github.admission(coordinator.github._lease(source["id"]), incoming, explicit=True))
                worker = task["workers"][0]
                worker_id = worker["session_id"]

                async def record_tool(name, arguments):
                    result = await coordinator.execute_session_tool(worker_id, name, arguments, unattended=True)
                    findings["tool_results"].append({"tool": name, "resource": arguments.get("resource"),
                        "success": not result.is_error, "outcome": result.outcome,
                        "error_category": result.error_category})
                    assert not result.is_error, name + " returned an error"
                    return result

                stage = "worker_source_discovery"
                discovery = await record_tool("github_read", {"resource": "sources"})
                assert discovery.data == {"sources": [{"source_id": source["id"], "repository": repository,
                                                        "numbers": [issue_number]}]}
                stage = "worker_repository_read"
                repository_read = await record_tool("github_read", {"resource": "repository"})
                assert repository_read.data["data"]["full_name"].casefold() == repository.casefold()
                assert repository_read.data["data"]["private"] is False
                stage = "worker_issue_read"
                issue_read = await record_tool("github_read", {"resource": "issue", "number": issue_number})
                assert issue_read.data["data"]["number"] == issue_number
                stage = "local_draft_approval"
                before_prepare = len(findings["remote_requests"])
                action = asyncio.create_task(coordinator.execute_session_tool(worker_id, "github_prepare", {
                    "kind": "comment", "number": issue_number,
                    "body": "Local-only read-only verification draft. Never published."}, unattended=True))
                async with asyncio.timeout(10):
                    while not coordinator.store.pending_requests() and not action.done():
                        await asyncio.sleep(0.02)
                pending = coordinator.store.pending_requests()
                assert len(pending) == 1 and pending[0]["kind"] == "approval", "Local draft did not request approval"
                assert pending[0]["payload"]["tool_name"] == "github_prepare"
                assert coordinator.github.drafts(source["id"]) == [], "Draft was prepared before approval"
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                    headers={"Authorization": "Bearer " + token}) as control:
                    decision = await control.post(f"/approvals/{pending[0]['id']}/decision", json={"approved": True})
                    assert decision.status_code == 200 and decision.json()["accepted"] is True
                result = await asyncio.wait_for(action, timeout=10)
                findings["tool_results"].append({"tool": "github_prepare", "resource": None,
                    "success": not result.is_error, "outcome": result.outcome, "error_category": result.error_category})
                assert not result.is_error, "Local draft preparation failed after explicit temporary approval"
                draft = result.data
                assert draft["source_id"] == source["id"] and draft["ticket_id"] == task["id"]
                assert draft["state"] == "draft" and draft["url"] is None
                assert len(findings["remote_requests"]) == before_prepare, "Local draft unexpectedly contacted GitHub"
                executions = [entry for run in coordinator.store.list_runs(worker_id)
                              for entry in coordinator.store.tool_executions(run["id"])]
                assert len(executions) == 4 and all(entry["state"] in {"committed", "checkpointed"} for entry in executions)
                assert all(entry["result"] and not entry["result"]["is_error"] for entry in executions)
                assert coordinator.store.connection.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 0
                assert not generate_calls and not coordinator.store.has_unresolved(worker_id)
                assert coordinator.github._runner is None and coordinator.tasks._runner is None
                findings.update(local_draft_only=True, local_draft_approval="temporary ASGI approval decision",
                    durable_tool_executions=len(executions), source_binding_verified=True,
                    repository_private=False, repository_default_branch=repository_read.data["data"]["default_branch"],
                    issue_read_verified=True, source_id_omitted_and_resolved=True)
                if screenshot:
                    stage = "actual_history_browser_screenshot"
                    findings["browser"] = await browser_evidence(app, coordinator, workspace, worker_title, screenshot, browser_port)
                findings["passed"] = True
                findings["stage"] = "complete"
            finally:
                if action and not action.done():
                    action.cancel()
                    await asyncio.gather(action, return_exceptions=True)
                await coordinator.close()
    except Exception as error:  # noqa: BLE001 - never expose raw HTTP errors, issue text or credential values.
        findings.update(passed=False, stage=stage, error_type=type(error).__name__)
        if isinstance(error, AssertionError):
            findings["detail"] = str(error)
    findings["model_requests"] = len(generate_calls)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(findings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(findings, ensure_ascii=False))
    return findings


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--static-root", type=Path)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    # This standalone verifier owns its process. No Runtime process environment,
    # user configuration, credential file or user conversation is ever changed.
    for name in list(os.environ):
        if name.endswith("_API_KEY") or name in {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
                                                "ASTER_API_TOKEN", "ASTER_DATA_DIR", "ASTER_HOME", "ASTER_WEB_STATIC_ROOT"}:
            os.environ.pop(name, None)
    outcome = asyncio.run(verify(args.repository, args.issue, args.report,
                                static_root=args.static_root, screenshot=args.screenshot))
    raise SystemExit(0 if outcome["passed"] else 1)
