import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from aster.customization.credentials import CredentialStore
from aster.domain import ModelTurn
from aster.engine import FakeModel
from aster.github import GithubService
from aster.github.transport import GithubHTTPError, GithubTransport
from aster.service import RunCoordinator
from aster.state.store import StateError

REPO = "owner/repository"


class Clock:
    now = datetime(2026, 11, 5, 12, tzinfo=UTC).timestamp()

    def __call__(self):
        return self.now

    def at(self, delta=0):
        return datetime.fromtimestamp(self.now + delta, UTC).isoformat().replace("+00:00", "Z")


def issue(clock, *, identity=7, actor="alice", actor_type="User", body="/aster inspect", delta=1, pull=False, state="open"):
    value = {"id": identity + 100, "number": identity, "title": "Inspect", "body": body, "state": state,
             "updated_at": clock.at(delta), "created_at": clock.at(-100), "user": {"login": actor, "type": actor_type},
             "html_url": f"https://github.com/{REPO}/{'pull' if pull else 'issues'}/{identity}"}
    if pull:
        value["pull_request"] = {"url": f"https://api.github.com/repos/{REPO}/pulls/{identity}"}
        value["head"] = {"sha": "a" * 40}
    return value


def comment(clock, *, identity=50, actor="alice", actor_type="User", body="Continue", delta=1):
    return {"id": identity, "body": body, "updated_at": clock.at(delta), "created_at": clock.at(-100),
            "user": {"login": actor, "type": actor_type}, "issue_url": f"https://api.github.com/repos/{REPO}/issues/7",
            "html_url": f"https://github.com/{REPO}/issues/7#issuecomment-{identity}"}


class Remote:
    def __init__(self, clock):
        self.clock, self.requests, self.posted = clock, [], []
        self.issues, self.comments, self.pulls, self.runs = [], [], [], []
        self.override = None

    def __call__(self, request):
        self.requests.append(request)
        if self.override:
            response = self.override(request)
            if response is not None:
                return response
        path = request.url.path
        assert request.url.host == "api.github.com"
        assert request.headers["x-github-api-version"] == "2026-03-10"
        if request.method == "POST":
            value = json.loads(request.content)
            self.posted.append(value)
            if path.endswith("/pulls"):
                return httpx.Response(201, json={"id": 900, "number": 8, "html_url": f"https://github.com/{REPO}/pull/8",
                    "body": value["body"], "title": value["title"], "user": {"login": "alice", "type": "User"},
                    "head": {"ref": value["head"].split(":", 1)[-1], "repo": {"full_name": REPO}},
                    "base": {"ref": value["base"], "repo": {"full_name": REPO}}, "state": "open"})
            value = {"id": 901, "body": value["body"], "html_url": f"https://github.com/{REPO}/issues/7#issuecomment-901",
                     "user": {"login": "alice", "type": "User"}, "updated_at": self.clock.at(2)}
            self.comments.append({**value, "issue_url": f"https://api.github.com/repos/{REPO}/issues/7"})
            return httpx.Response(201, json=value)
        if path == "/user":
            return httpx.Response(200, json={"id": 1, "login": "alice", "type": "User"})
        if path == "/repos/" + REPO:
            return httpx.Response(200, json={"id": 1, "full_name": REPO, "private": False, "default_branch": "main"})
        if path.endswith(("/issues/comments", "/issues/7/comments")):
            return httpx.Response(200, json=self.comments)
        if path.endswith("/issues"):
            return httpx.Response(200, json=self.issues)
        if path.endswith(("/pulls/comments", "/reviews")):
            return httpx.Response(200, json=[])
        if path.endswith("/pulls"):
            return httpx.Response(200, json=self.pulls)
        if path.endswith("/actions/runs"):
            return httpx.Response(200, json={"workflow_runs": self.runs})
        if path.endswith("/issues/7"):
            return httpx.Response(200, json=issue(self.clock, delta=-1))
        if path.endswith("/pulls/7"):
            return httpx.Response(200, json=issue(self.clock, pull=True))
        raise AssertionError("Unexpected GitHub request " + str(request.url))


def setup(tmp_path, *, fields=None):
    clock = Clock()
    remote = Remote(clock)
    data = tmp_path / "state"
    value = RunCoordinator(data, tools=[], credentials=CredentialStore(data_dir=data),
                           model_factory=lambda settings: FakeModel([ModelTurn("done")]), browser_enabled=False)
    if value.providers.get("deepseek")["revision"] == 1:
        value.providers.update("deepseek", expected_revision=1, api_key="offline-triage-only-key")
    value.task_triage_model_factory = lambda config, pin: FakeModel([ModelTurn(
        json.dumps({"decision": "process", "reason": "Offline authorized work"}))])
    value.github = GithubService(value, transport=httpx.MockTransport(remote), clock=clock)
    value.store._github_service = value.github
    sid = value.create_session(tmp_path / "work")["id"]
    source = value.github.create_source(sid, REPO, **(fields or {}))
    return value, source, remote, clock


async def publish(value, source, *, request_id="draft", body="Reviewed result", kind="comment"):
    draft = value.github.create_draft(source["id"], kind, body, request_id=request_id,
                                     **({"number": 7} if kind == "comment" else {"title": "Reviewed change", "head": "feature", "base": "main"}))
    result = await value.execute_session_tool(source["source_session_id"], "github_publish", {
        "draft_id": draft["id"], "expected_revision": draft["revision"]}, approved=True)
    return draft, result


async def test_defaults_management_reads_and_baseline_never_create_model_work(tmp_path):
    value, source, remote, clock = setup(tmp_path)
    try:
        remote.issues = [issue(clock, delta=-100)]
        assert not source["enabled"] and source["mode"] == "read_only" and not source["allow_writes"]
        value.github.list_sources()
        value.github.get_source(source["id"])
        value.github.events(source["id"])
        value.github.drafts(source["id"])
        assert not remote.requests
        result = await value.github.poll_source(source["id"])
        assert result["read_only"] and not value.tasks.list_tickets()
        assert value.github.events(source["id"])[0]["baseline"]
        assert all(request.method == "GET" for request in remote.requests)
        assert not value.store.list_runs()
    finally:
        await value.close()


async def test_read_only_override_blocks_explicit_listed_mentions(tmp_path):
    value, source, remote, clock = setup(tmp_path, fields={"enabled": True, "mode": "tasks", "allowed_actors": ["alice"]})
    try:
        await value.github.poll_source(source["id"], read_only=True)
        clock.now += 10
        remote.issues = [issue(clock)]
        result = await value.github.poll_source(source["id"], read_only=True)
        assert result["events"] == 1 and not value.tasks.list_tickets() and not value.store.list_runs()
    finally:
        await value.close()


async def test_personal_pat_owner_can_authorize_but_own_marked_reply_cannot_loop(tmp_path):
    value, source, remote, clock = setup(tmp_path, fields={"enabled": True, "mode": "tasks", "allowed_actors": ["alice"], "token": "synthetic-token"})
    try:
        await value.github.poll_source(source["id"])
        assert value.github.get_source(source["id"])["verified_login"] == "alice"
        clock.now += 10
        remote.issues = [issue(clock)]
        await value.github.poll_source(source["id"])
        assert len(value.tasks.list_tickets()) == 1
        draft = value.github.create_draft(source["id"], "comment", "Result", number=7, request_id="result")
        row = value.github._draft_row(draft["id"])
        remote.comments = [comment(clock, body="/aster another task\n" + row["marker"])]
        await value.github.poll_source(source["id"])
        events = value.github.events(source["id"])
        assert any(item["disposition"] == "own_reply" for item in events)
        assert len(value.tasks.list_tickets()) == 1
    finally:
        await value.close()


@pytest.mark.parametrize("actor_type", [None, "Organization", "Unknown", "Bot"])
async def test_only_documented_user_metadata_can_authorize_human_request(tmp_path, actor_type):
    value, source, remote, clock = setup(tmp_path, fields={"enabled": True, "mode": "tasks", "allowed_actors": ["alice"]})
    try:
        await value.github.poll_source(source["id"])
        clock.now += 10
        remote.issues = [issue(clock, actor_type=actor_type)]
        await value.github.poll_source(source["id"])
        assert not value.tasks.list_tickets()
    finally:
        await value.close()


async def test_baseline_pagination_continues_without_advancing_watermark(tmp_path):
    value, source, remote, clock = setup(tmp_path)
    try:
        (value.data_dir / "config.yaml").write_text("github:\n  max_pages_per_stream: 1\n", encoding="utf-8")

        def pages(request):
            if request.url.path.endswith("/issues"):
                page = int(request.url.params.get("page", 1))
                rows = [issue(clock, identity=page, delta=-100)]
                headers = {"Link": f'<https://api.github.com/repos/{REPO}/issues?page=2>; rel="next"'} if page == 1 else {}
                return httpx.Response(200, json=rows, headers=headers)
            return None

        remote.override = pages
        first = await value.github.poll_source(source["id"])
        cursor = value.github._stream_state(source["id"], "issues")
        assert first["pending_pages"] and cursor["page"] == 2 and cursor["since"] is None
        await value.github.poll_source(source["id"])
        cursor = value.github._stream_state(source["id"], "issues")
        assert cursor["page"] == 1 and cursor["since"] is not None and not cursor["baseline"]
        assert [request.url.params["page"] for request in remote.requests if request.url.path.endswith("/issues")] == ["1", "2"]
    finally:
        await value.close()


async def test_foreign_pagination_link_never_dispatches_or_commits_page(tmp_path):
    value, source, remote, clock = setup(tmp_path)
    try:
        remote.override = lambda request: httpx.Response(200, json=[issue(clock)], headers={
            "Link": '<https://attacker.example/repos/owner/repository/issues?page=2>; rel="next"'}) if request.url.path.endswith("/issues") else None
        with pytest.raises(GithubHTTPError):
            await value.github.poll_source(source["id"])
        assert len(remote.requests) == 1
        assert value.github._stream_state(source["id"], "issues")["page"] == 1
        assert not value.github.events(source["id"])
    finally:
        await value.close()


async def test_retry_after_stops_repeated_http_and_rate_budget_throttles(tmp_path):
    value, source, remote, clock = setup(tmp_path)
    try:
        remote.override = lambda request: httpx.Response(429, json={"message": "slow"}, headers={"Retry-After": "600"})
        with pytest.raises(GithubHTTPError):
            await value.github.poll_source(source["id"])
        assert value.github.get_source(source["id"])["health"] == "rate_limited"
        before = len(remote.requests)
        await value.github.poll_source(source["id"])
        assert len(remote.requests) == before
        clock.now += 601
        remote.override = lambda request: httpx.Response(200, json=[] if not request.url.path.endswith("/actions/runs") else {"workflow_runs": []},
            headers={"x-ratelimit-remaining": "10", "x-ratelimit-reset": str(int(clock.now + 3600))})
        await value.github.poll_source(source["id"])
        assert value.github.get_source(source["id"])["next_poll_at"] > clock.now + 300
    finally:
        await value.close()


async def test_token_and_source_generation_rechecked_after_wait_before_ingestion(tmp_path):
    value, source, remote, clock = setup(tmp_path, fields={"token": "synthetic-token"})
    try:
        changed = False

        def rotate(request):
            nonlocal changed
            if not changed:
                changed = True
                row = value.github._row(source["id"])
                value.credentials.set(row["credential_ref"], "manual-file-rotation")
            return httpx.Response(200, json=[issue(clock)])

        remote.override = rotate
        with pytest.raises(StateError):
            await value.github.poll_source(source["id"])
        assert not value.github.events(source["id"])
        raw = value.store.connection.execute("SELECT document FROM github_sources WHERE id=?", (source["id"],)).fetchone()[0]
        assert "synthetic-token" not in raw and "manual-file-rotation" not in raw
    finally:
        await value.close()


@pytest.mark.parametrize("status,foreign", [(202, False), (201, True)])
async def test_malformed_publish_success_is_unknown_and_never_reposted(tmp_path, status, foreign):
    value, source, remote, _clock = setup(tmp_path, fields={"enabled": True, "mode": "tasks", "allow_writes": True, "token": "synthetic-token"})
    try:
        def malformed(request):
            if request.method == "POST":
                payload = json.loads(request.content)
                return httpx.Response(status, json={"id": 9, "body": payload["body"], "user": {"login": "alice"},
                    "html_url": f"https://github.com/{'other/repo' if foreign else REPO}/issues/7#issuecomment-9"})
            return None

        remote.override = malformed
        draft, result = await publish(value, source)
        assert result.outcome == "unknown"
        assert value.github.get_draft(draft["id"])["state"] == "outcome_unknown"
        with pytest.raises(StateError):
            value.github.update_draft(draft["id"], value.github.get_draft(draft["id"])["revision"], body="try again")
        count = sum(request.method == "POST" for request in remote.requests)
        again = await value.github.publish(draft["id"], 0, None)
        assert again.outcome == "unknown" and sum(request.method == "POST" for request in remote.requests) == count
    finally:
        await value.close()


async def test_positive_comment_and_pull_request_receipts_are_verified(tmp_path):
    value, source, remote, _clock = setup(tmp_path, fields={"enabled": True, "mode": "tasks", "allow_writes": True, "token": "synthetic-token"})
    try:
        for kind in ("comment", "pull_request"):
            draft, result = await publish(value, source, request_id=kind, kind=kind)
            assert not result.is_error and result.outcome == "known"
            assert value.github.get_draft(draft["id"])["state"] == "sent"
        assert len(remote.posted) == 2
    finally:
        await value.close()


async def test_default_port_transport_pins_dns_and_checks_generation_before_inner(monkeypatch):
    from aster.github import transport as module
    calls = []

    async def dns(value, allowed_hosts, *, dns_fallback):
        assert value.startswith("https://api.github.com/") and not allowed_hosts
        assert dns_fallback == "google"
        return httpx.URL(value).copy_with(host="140.82.112.5"), "api.github.com", "api.github.com"

    async def inner(request):
        calls.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(module, "pin_url", dns)
    transport = GithubTransport()
    await transport.inner.aclose()
    transport.inner = httpx.MockTransport(inner)
    request = httpx.Request("GET", "https://api.github.com/repos/owner/repository")
    response = await transport.handle_async_request(request)
    assert response.status_code == 200 and calls[0].url.host == "140.82.112.5"
    assert calls[0].headers["host"] == "api.github.com" and calls[0].extensions["sni_hostname"] == "api.github.com"
    for url in ("http://api.github.com/repos/x/y", "https://evil.example/repos/x/y", "https://api.github.com:444/repos/x/y"):
        with pytest.raises(GithubHTTPError):
            await transport.handle_async_request(httpx.Request("GET", url))
    await transport.aclose()


async def test_fake_ip_dns_fallback_and_global_opt_out_are_enforced(monkeypatch):
    from aster.tools import http as http_module

    async def fake_dns(*args, **kwargs):
        return [(2, 1, 6, "", ("198.18.0.54", 443))]

    resolved = []

    async def public_dns(host, **kwargs):
        resolved.append(host)
        return ["140.82.112.5"]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_dns)
    monkeypatch.setattr(http_module, "public_dns_addresses", public_dns)
    transport = GithubTransport()
    await transport.inner.aclose()
    transport.inner = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    await transport.handle_async_request(httpx.Request("GET", "https://api.github.com/repos/owner/repository"))
    assert resolved == ["api.github.com"]
    transport.dns_fallback = "off"
    with pytest.raises(PermissionError):
        await transport.handle_async_request(httpx.Request("GET", "https://api.github.com/repos/owner/repository"))
    assert resolved == ["api.github.com"]
    await transport.aclose()


async def test_source_wide_unknown_barrier_blocks_owner_after_worker_delivery(tmp_path):
    value, source, remote, _clock = setup(tmp_path, fields={"enabled": True, "mode": "tasks", "allow_writes": True, "token": "synthetic-token"})
    try:
        ticket = value.tasks.create_ticket(source["source_session_id"], "Linked work", "inspect")
        await value.github.bind_thread(source["id"], ticket["id"], 7)
        worker_id = ticket["workers"][0]["session_id"]
        first = value.github.create_draft(source["id"], "comment", "First result", number=7, ticket_id=ticket["id"], request_id="first")

        def timeout(request):
            if request.method == "POST":
                raise httpx.ReadTimeout("synthetic timeout after remote acceptance", request=request)

        remote.override = timeout
        result = await value.execute_session_tool(worker_id, "github_publish", {"draft_id": first["id"], "expected_revision": 0}, approved=True)
        assert result.outcome == "unknown"
        posts = sum(request.method == "POST" for request in remote.requests)
        second, result = await publish(value, source, request_id="second")
        assert result.is_error and result.outcome == "not_executed"
        assert sum(request.method == "POST" for request in remote.requests) == posts
        assert value.github.get_draft(second["id"])["state"] == "draft"
        reconciled = await value.github.reconcile(first["id"])
        assert not reconciled["verified"] and reconciled["draft"]["state"] == "outcome_unknown"
    finally:
        await value.close()


async def test_sent_draft_unknown_ledger_can_reconcile_after_ack_crash(tmp_path):
    value, source, remote, _clock = setup(tmp_path, fields={"enabled": True, "mode": "tasks", "allow_writes": True, "token": "synthetic-token"})
    try:
        draft, result = await publish(value, source)
        assert not result.is_error
        receipt = value.github._draft_row(draft["id"])
        with value.store.transaction() as db:
            db.execute("UPDATE tool_executions SET state='outcome_unknown' WHERE id=?", (receipt["execution_id"],))
        posts = len(remote.posted)
        result = await value.github.reconcile(draft["id"])
        assert result["verified"] and len(remote.posted) == posts
        assert value.store.tool_execution(receipt["execution_id"])["state"] == "committed"
        assert value.github.get_draft(draft["id"])["state"] == "sent"
    finally:
        await value.close()


async def test_policy_generation_and_unlisted_bot_cannot_resume_existing_work(tmp_path):
    value, source, remote, clock = setup(tmp_path, fields={"enabled": True, "mode": "tasks", "allowed_actors": ["alice"], "observe_issues": True})
    try:
        value.tasks.update_settings(0, "auto", [{"id": "inspect", "enabled": True, "contains": ["Inspect"]}])
        await value.github.poll_source(source["id"])
        clock.now += 10
        remote.issues = [issue(clock, body="Inspect this implementation")]
        await value.github.poll_source(source["id"])
        ticket = value.tasks.list_tickets()[0]
        assert value.github._bindings(ticket["id"])[0]["explicit"] == 0
        remote.comments = [comment(clock, actor="unknown-bot[bot]", actor_type="Bot", body="/aster continue")]
        await value.github.poll_source(source["id"])
        assert not value.store.connection.execute("SELECT 1 FROM local_task_inputs WHERE ticket_id=?", (ticket["id"],)).fetchone()
        policy = value.tasks.settings()
        value.tasks.update_settings(policy["revision"], "observe", policy["rules"])
        with pytest.raises(StateError, match="policy changed"):
            value.github.guard_ticket(ticket["id"])
    finally:
        await value.close()


async def test_stop_requires_exact_listed_human_command(tmp_path):
    value, source, remote, clock = setup(tmp_path, fields={"enabled": True, "mode": "tasks", "allowed_actors": ["alice"]})
    try:
        await value.github.poll_source(source["id"])
        clock.now += 10
        remote.issues = [issue(clock)]
        await value.github.poll_source(source["id"])
        ticket = value.tasks.list_tickets()[0]
        remote.comments = [comment(clock, body="/aster stopping is not a command")]
        await value.github.poll_source(source["id"])
        assert not value.tasks.get_ticket(ticket["id"])["cancel_requested"]
        remote.comments.append(comment(clock, identity=51, body="/aster stop"))
        await value.github.poll_source(source["id"])
        assert value.tasks.get_ticket(ticket["id"])["cancel_requested"]
    finally:
        await value.close()


async def test_unsent_draft_can_be_explicitly_reviewed_after_enabling_writes(tmp_path):
    value, source, remote, _clock = setup(tmp_path, fields={"token": "synthetic-token"})
    try:
        draft = value.github.create_draft(source["id"], "comment", "Review me", number=7, request_id="draft")
        current = value.github.update_source(source["id"], 0, enabled=True, mode="tasks", allow_writes=True)
        assert current["identity_generation"] != draft["identity_generation"]
        reviewed = value.github.update_draft(draft["id"], 0, body="Reviewed me")
        assert reviewed["identity_generation"] == current["identity_generation"]
        result = await value.execute_session_tool(source["source_session_id"], "github_publish", {
            "draft_id": draft["id"], "expected_revision": reviewed["revision"]}, approved=True)
        assert not result.is_error and len(remote.posted) == 1
    finally:
        await value.close()


async def test_configured_token_is_redacted_from_remote_fields_reads_and_rejected_in_drafts(tmp_path):
    token = "synthetic-reflected-token"
    value, source, remote, clock = setup(tmp_path, fields={"token": token})
    try:
        record = issue(clock, delta=-5)
        record.update(title="Reflected " + token, body="Body " + token)
        remote.issues = [record]
        await value.github.poll_source(source["id"], read_only=True)
        assert token not in json.dumps(value.github.events(source["id"]))
        assert token not in "\n".join(value.store.connection.iterdump())
        remote.override = lambda request: httpx.Response(200, json={"description": token, token: "reflected"}) if request.url.path == "/repos/" + REPO else None
        result = await value.github.read(source["id"])
        assert token not in json.dumps(result)
        with pytest.raises(ValueError, match="credential"):
            value.github.create_draft(source["id"], "comment", "Publish " + token, number=7, request_id="no")
    finally:
        await value.close()


async def test_background_shutdown_cancels_only_observational_poll(tmp_path):
    value, _source, remote, _clock = setup(tmp_path, fields={"enabled": True})
    entered = asyncio.Event()

    async def wait_forever(request):
        entered.set()
        await asyncio.Event().wait()

    remote.override = wait_forever
    try:
        await value.github.start()
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(value.github.close(), 1)
        assert value.github._runner is None
        assert not value.store.list_runs()
    finally:
        await value.close()


async def test_closed_historical_prs_do_not_starve_current_open_prs(tmp_path):
    value, source, remote, clock = setup(tmp_path)
    try:
        (value.data_dir / "config.yaml").write_text("github:\n  max_requests_per_poll: 7\n", encoding="utf-8")
        remote.pulls = [issue(clock, identity=1, pull=True, delta=-100)]

        def details(request):
            parts = request.url.path.rsplit("/", 1)
            if parts[0].endswith("/pulls") and parts[1].isdigit():
                selected = next(row for row in remote.pulls if row["number"] == int(parts[1]))
                return httpx.Response(200, json=selected)
            return None

        remote.override = details
        await value.github.poll_source(source["id"])
        clock.now += 10
        remote.pulls = [issue(clock, identity=1, pull=True, state="closed", delta=-2),
                        issue(clock, identity=7, pull=True, delta=-1), issue(clock, identity=8, pull=True, delta=-1)]
        await value.github.poll_source(source["id"])
        remote.requests.clear()
        await value.github.poll_source(source["id"])
        await value.github.poll_source(source["id"])
        paths = [request.url.path for request in remote.requests]
        assert "/repos/" + REPO + "/pulls/1" not in paths
        assert "/repos/" + REPO + "/pulls/7" in paths and "/repos/" + REPO + "/pulls/8" in paths
        assert value.store.connection.execute("SELECT state FROM github_threads WHERE source_id=? AND number=1", (source["id"],)).fetchone()[0] == "closed"
    finally:
        await value.close()


@pytest.mark.parametrize("field", ["title", "head", "base", "repository", "number", "comment_fragment"])
async def test_success_receipt_must_match_exact_reviewed_target(tmp_path, field):
    value, source, remote, _clock = setup(tmp_path, fields={"enabled": True, "mode": "tasks", "allow_writes": True, "token": "synthetic-token"})
    try:
        def wrong_receipt(request):
            if request.method != "POST":
                return None
            data = json.loads(request.content)
            row = {"id": 90, "number": 8, "body": data["body"], "title": data.get("title"),
                   "user": {"login": "alice", "type": "User"}, "html_url": f"https://github.com/{REPO}/pull/8",
                   "head": {"ref": data.get("head"), "repo": {"full_name": REPO}},
                   "base": {"ref": data.get("base"), "repo": {"full_name": REPO}}}
            if field == "title":
                row["title"] = "unreviewed"
            elif field in {"head", "base"}:
                row[field]["ref"] = "unreviewed"
            elif field == "repository":
                row["head"]["repo"]["full_name"] = "other/repo"
            elif field == "number":
                row["number"] = -8
                row["html_url"] = f"https://github.com/{REPO}/pull/-8"
            else:
                row["html_url"] = f"https://github.com/{REPO}/issues/7#issuecomment-123"
            return httpx.Response(201, json=row)

        remote.override = wrong_receipt
        _draft, result = await publish(value, source, kind="comment" if field == "comment_fragment" else "pull_request")
        assert result.outcome == "unknown"
    finally:
        await value.close()


async def test_changed_repository_reconcile_never_sends_current_credential_to_old_target(tmp_path):
    value, source, remote, _clock = setup(tmp_path, fields={"enabled": True, "mode": "tasks", "allow_writes": True, "token": "synthetic-token"})
    try:
        remote.override = lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("synthetic", request=request)) if request.method == "POST" else None
        draft, result = await publish(value, source)
        assert result.outcome == "unknown"
        value.github.update_source(source["id"], 0, repository="other/repository")
        requests = len(remote.requests)
        with pytest.raises(StateError, match="repository"):
            await value.github.reconcile(draft["id"])
        assert len(remote.requests) == requests
    finally:
        await value.close()
