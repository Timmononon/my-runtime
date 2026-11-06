"""Offline GitHub workbench fixture: real Runtime/API, MockTransport GitHub only."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import socket
import tempfile
import time
from pathlib import Path

import httpx
import uvicorn
import yaml
from fastapi import Request
from web_fixture import WorkbenchFixtureModel

from aster.api import create_app
from aster.customization.credentials import CredentialStore
from aster.domain import ModelTurn, Usage
from aster.engine import FakeModel
from aster.platform.filesystem import atomic_write
from aster.service import RunCoordinator


class OfflineGithub:
    def __init__(self):
        self.requests = []
        self.comments = []
        self.pulls = []
        self.ambiguous_next_send = False
        self.omit_reconcile_match = False
        self.fail_next_read = False
        self.issue_body = "离线仓库事件，仅作观察。"
        self.issue_updated = "2026-11-05T10:00:00Z"
        self.triage_decision = "ignore"

    @staticmethod
    def user(login="alice", kind="User"):
        return {"login": login, "type": kind, "id": 10}

    @staticmethod
    def branch(value):
        owner, separator, ref = value.partition(":")
        if not separator:
            owner, ref = "fixture", value
        return {"sha": "a" * 40, "ref": ref, "label": f"{owner}:{ref}",
                "repo": {"id": 42, "name": "repo", "full_name": f"{owner}/repo",
                         "owner": {"login": owner, "type": "Organization", "id": 42}}}

    def issue(self):
        return {"id": 1007, "number": 7, "title": "Offline issue", "body": self.issue_body,
                "user": self.user(), "state": "open", "updated_at": self.issue_updated,
                "created_at": "2026-11-05T09:00:00Z", "html_url": "https://github.com/fixture/repo/issues/7"}

    def pull(self, number=8, **changes):
        return {"id": 2000 + number, "number": number, "title": "Offline pull request", "body": "离线 PR 内容。",
                "user": self.user(), "state": "open", "merged": False, "merged_at": None,
                "updated_at": "2026-11-05T10:00:00Z", "created_at": "2026-11-05T09:00:00Z",
                "html_url": f"https://github.com/fixture/repo/pull/{number}",
                "head": self.branch("feature"), "base": self.branch("main"), **changes}

    def __call__(self, request):
        assert request.url.scheme == "https" and request.url.host == "api.github.com", "Fixture forbids real/custom origins"
        method, path = request.method, request.url.path
        self.requests.append({"method": method, "path": path})
        if self.fail_next_read and method == "GET":
            self.fail_next_read = False
            return httpx.Response(429, headers={"Retry-After": "60", "x-ratelimit-remaining": "0"}, json={"message": "Offline rate limit"})
        if method == "GET":
            if path == "/user":
                return httpx.Response(200, json=self.user("fixture-user"))
            if path == "/repos/fixture/repo":
                return httpx.Response(200, json={"id": 42, "full_name": "fixture/repo", "private": False,
                                                "default_branch": "main", "permissions": {"pull": True, "push": True}})
            if path == "/repos/fixture/repo/issues":
                return httpx.Response(200, json=[self.issue()])
            if path == "/repos/fixture/repo/issues/7":
                return httpx.Response(200, json=self.issue())
            if path == "/repos/fixture/repo/issues/comments" or path == "/repos/fixture/repo/issues/7/comments":
                return httpx.Response(200, json=[] if self.omit_reconcile_match else self.comments)
            if path == "/repos/fixture/repo/pulls":
                return httpx.Response(200, json=[self.pull(), *self.pulls])
            if path.startswith("/repos/fixture/repo/pulls/") and path.endswith(("/reviews", "/comments")):
                return httpx.Response(200, json=[])
            if path == "/repos/fixture/repo/pulls/comments":
                return httpx.Response(200, json=[])
            if path.startswith("/repos/fixture/repo/pulls/") and path.rsplit("/", 1)[-1].isdigit():
                number = int(path.rsplit("/", 1)[-1])
                return httpx.Response(200, json=next((row for row in self.pulls if row["number"] == number), self.pull(number)))
            if path.startswith("/repos/fixture/repo/issues/") and path.rsplit("/", 1)[-1].isdigit():
                number = int(path.rsplit("/", 1)[-1])
                return httpx.Response(200, json={**self.pull(number), "pull_request": {"url": f"https://api.github.com/repos/fixture/repo/pulls/{number}"}})
            if path == "/repos/fixture/repo/actions/runs":
                return httpx.Response(200, json={"total_count": 1, "workflow_runs": [{"id": 300, "name": "Offline CI",
                    "status": "completed", "conclusion": "success", "head_sha": "a" * 40, "run_attempt": 1,
                    "actor": self.user("github-actions[bot]", "Bot"), "pull_requests": [{"number": 8}],
                    "updated_at": "2026-11-05T10:00:00Z", "html_url": "https://github.com/fixture/repo/actions/runs/300"}]})
            if path.startswith(("/repos/fixture/repo/branches/", "/repos/fixture/repo/git/ref/heads/")):
                return httpx.Response(200, json={"name": path.rsplit("/", 1)[-1], "commit": {"sha": "a" * 40}, "object": {"sha": "a" * 40}})
        if method == "POST" and path == "/repos/fixture/repo/issues/7/comments":
            payload = json.loads(request.content)
            row = {"id": 500 + len(self.comments), "body": payload["body"], "user": self.user("fixture-user"),
                   "issue_url": "https://api.github.com/repos/fixture/repo/issues/7",
                   "html_url": f"https://github.com/fixture/repo/issues/7#issuecomment-{500 + len(self.comments)}",
                   "created_at": "2026-11-05T11:00:00Z", "updated_at": "2026-11-05T11:00:00Z"}
            self.comments.append(row)
            if self.ambiguous_next_send:
                self.ambiguous_next_send = False
                raise httpx.ReadTimeout("Offline ambiguous write after remote commit", request=request)
            return httpx.Response(201, json=row)
        if method == "POST" and path == "/repos/fixture/repo/pulls":
            payload = json.loads(request.content)
            row = self.pull(9 + len(self.pulls), title=payload["title"], body=payload["body"], user=self.user("fixture-user"),
                            head=self.branch(payload["head"]), base=self.branch(payload["base"]))
            self.pulls.append(row)
            return httpx.Response(201, json=row)
        return httpx.Response(404, json={"message": "Unimplemented offline GitHub route"})


async def serve(ready_file, stop_file, lifetime):
    with tempfile.TemporaryDirectory(prefix="aster-github-web-fixture-") as temporary:
        root = Path(temporary)
        data, workspace = root / "state", root / "workspace"
        data.mkdir()
        workspace.mkdir()
        (workspace / "fixture-note.txt").write_text("Offline GitHub fixture.\n", encoding="utf-8")
        (data / "config.yaml").write_text(yaml.safe_dump({"cwd": str(workspace), "model": "deepseek-flash",
                                                        "runtime": {"deadline_seconds": 120}}), encoding="utf-8")
        credentials = CredentialStore(data_dir=data)
        credentials.set("deepseek", "offline-synthetic-model-key-no-network")
        remote = OfflineGithub()
        coordinator = RunCoordinator(data, model_factory=lambda settings: WorkbenchFixtureModel(data, settings),
                                     credentials=credentials, browser_enabled=False,
                                     github_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(remote), trust_env=False))
        triage_calls = []

        def triage_response(messages, tools):
            assert tools == [], "Triage fixture may only classify without tools"
            triage_calls.append({"model": "offline", "decision": remote.triage_decision})
            if remote.triage_decision == "error":
                raise RuntimeError("Offline simulated model failure")
            return ModelTurn(json.dumps({"decision": remote.triage_decision,
                "reason": "该请求需要调查。" if remote.triage_decision == "process" else "该讨论无需仓库改动。"}, ensure_ascii=False),
                usage=Usage(input_tokens=100, output_tokens=30))

        coordinator.task_triage_model_factory = lambda config, pin: FakeModel([triage_response])
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        token = secrets.token_urlsafe(36)
        static_root = Path(os.environ["ASTER_WEB_STATIC_ROOT"]) if os.environ.get("ASTER_WEB_STATIC_ROOT") else None
        app = create_app(coordinator, token=token, allowed_origins={origin}, static_root=static_root)

        @app.get("/fixture/github/state")
        async def state():
            calls = data / "fixture-model-calls.jsonl"
            return {"requests": remote.requests, "remote_writes": sum(row["method"] != "GET" for row in remote.requests),
                    "comment_count": len(remote.comments), "pull_count": len(remote.pulls),
                    "triage_calls": len(triage_calls),
                    "model_calls": len(calls.read_text(encoding="utf-8").splitlines()) if calls.exists() else 0}

        @app.post("/fixture/github/control")
        async def control(request: Request):
            body = await request.json()
            for name in ("ambiguous_next_send", "omit_reconcile_match", "fail_next_read"):
                if name in body:
                    setattr(remote, name, body[name] is True)
            for name in ("issue_body", "issue_updated"):
                if name in body and isinstance(body[name], str) and len(body[name]) <= 10000:
                    setattr(remote, name, body[name])
            if body.get("triage_decision") in {"process", "ignore", "error"}:
                remote.triage_decision = body["triage_decision"]
            if "require_publish_approval" in body:
                config_path = data / "config.yaml"
                settings = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                permissions = settings.setdefault("permissions", {})
                names = set(permissions.get("ask_tools", []))
                if body["require_publish_approval"] is True:
                    names.add("github_publish")
                else:
                    names.discard("github_publish")
                permissions["ask_tools"] = sorted(names)
                atomic_write(config_path, yaml.safe_dump(settings).encode("utf-8"))
            return {"configured": True}

        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, proxy_headers=False,
                                              timeout_graceful_shutdown=2))
        running = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            while not server.started:
                if running.done():
                    await running
                    raise RuntimeError("Offline fixture stopped before startup")
                await asyncio.sleep(0.01)
            ready_file.parent.mkdir(parents=True, exist_ok=True)
            ready_file.write_text(json.dumps({"fixture": True, "base_url": origin, "token": token,
                                              "cwd": str(workspace), "data_dir": str(data)}), encoding="utf-8")
            deadline = time.monotonic() + lifetime
            while not stop_file.exists() and time.monotonic() < deadline and not running.done():
                await asyncio.sleep(0.1)
        finally:
            server.should_exit = True
            try:
                await asyncio.wait_for(running, 10)
            finally:
                await coordinator.close()
                listener.close()
                ready_file.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--lifetime", type=float, default=180)
    arguments = parser.parse_args()
    asyncio.run(serve(arguments.ready_file, arguments.stop_file, arguments.lifetime))
