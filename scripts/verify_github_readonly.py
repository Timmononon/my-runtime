"""Explicit, isolated, anonymous GitHub GET-only acceptance; never runs a model."""
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from collections import Counter
from pathlib import Path

import httpx

from aster.api import create_app
from aster.customization.credentials import CredentialStore
from aster.github.normalize import repository as checked_repository
from aster.github.transport import GithubTransport
from aster.service import RunCoordinator


async def verify(repository: str, report: Path):
    repository = checked_repository(repository)
    requests, responses, models = [], [], []
    prefix = "/repos/" + repository.casefold()

    async def record_request(request):
        # These assertions run before the real production transport sends bytes.
        # Never record headers or adopt any ambient GitHub credential.
        assert request.method == "GET", "Acceptance forbids all GitHub mutations"
        assert request.url.scheme == "https" and request.url.host == "api.github.com"
        assert request.url.path.casefold() == prefix or request.url.path.casefold().startswith(prefix + "/")
        assert "authorization" not in request.headers, "Acceptance must remain anonymous"
        requests.append({"method": request.method, "path": request.url.path})

    async def record_response(response):
        responses.append({"path": response.request.url.path, "status": response.status_code})

    def github_client():
        return httpx.AsyncClient(
            transport=GithubTransport(), timeout=httpx.Timeout(30, connect=15),
            trust_env=False, follow_redirects=False,
            event_hooks={"request": [record_request], "response": [record_response]},
        )

    def forbidden_model(settings):
        models.append(True)
        raise AssertionError("Read-only acceptance must never prepare a model")

    with tempfile.TemporaryDirectory(prefix="aster-github-readonly-") as directory:
        root = Path(directory)
        data = root / "state"
        coordinator = RunCoordinator(
            data, tools=[], model_factory=forbidden_model,
            credentials=CredentialStore(data_dir=data), github_client_factory=github_client,
        )
        app = create_app(coordinator, token="synthetic-github-readonly-acceptance-token", manage_lifecycle=False)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                headers={"Authorization": "Bearer synthetic-github-readonly-acceptance-token"},
            ) as client:
                capabilities = await client.get("/capabilities")
                assert "github_sources_v1" in capabilities.json()["capabilities"]
                session_response = await client.post("/sessions", json={"cwd": str(root / "workspace")})
                assert session_response.status_code == 201
                session = session_response.json()
                created = await client.post("/github/sources", json={
                    "source_session_id": session["id"], "repository": repository,
                    "enabled": False, "mode": "read_only", "allow_writes": False,
                    "observe_issues": True, "observe_pulls": True,
                    "observe_comments": True, "observe_ci": True,
                })
                assert created.status_code == 201
                source = created.json()
                assert not source["configured"] and source["mode"] == "read_only"
                assert not requests, "Management operations must remain offline"

                checked = await client.post(f"/github/sources/{source['id']}/check", json={})
                check_category = checked.json().get("category", checked.json().get("error"))
                assert checked.status_code == 200, ("GitHub connection check failed: HTTP " + str(checked.status_code)
                                                    + ", category=" + str(check_category))
                facts = checked.json()["repository"]
                assert facts["full_name"].casefold() == repository.casefold()
                assert facts["private"] is False

                polled = await client.post(f"/github/sources/{source['id']}/poll", json={"read_only": True})
                assert polled.status_code == 200, "Read-only event poll failed: HTTP " + str(polled.status_code)
                polling = polled.json()
                assert polling["read_only"] is True and polling["accepted"] == 0
                assert polling["requests"] > 0
                events = await client.get(f"/github/sources/{source['id']}/events?limit=500")
                assert events.status_code == 200
                task_list = await client.get("/tasks")
                assert task_list.status_code == 200 and task_list.json()["tickets"] == []
                saved = await client.get(f"/github/sources/{source['id']}")
                assert saved.status_code == 200
                assert not saved.json()["enabled"] and not saved.json()["allow_writes"]
                assert not saved.json()["configured"]
                assert coordinator.store.connection.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 0
                assert coordinator.store.list_runs() == []
                assert not models
                assert responses and all(row["status"] in {200, 304} for row in responses)

                findings = {
                    "passed": True, "repository": repository, "anonymous": True,
                    "repository_facts": facts, "remote_requests": requests,
                    "remote_responses": responses, "remote_mutations": 0,
                    "poll_requests": polling["requests"], "events_observed": polling["events"],
                    "events_stored": saved.json().get("event_count"),
                    "event_kinds": dict(Counter(item["kind"] for item in events.json()["events"])),
                    "baseline": polling.get("baseline"), "pending_pages": polling.get("pending_pages"),
                    "created_tickets": 0, "model_preparations": len(models), "paid_calls": 0,
                    "user_credentials_used": False, "user_state_modified": False,
                    "runtime_state": "temporary directory removed on completion",
                }
        finally:
            await coordinator.close()
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(findings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(findings, ensure_ascii=False))
    return findings


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        asyncio.run(verify(arguments.repository, arguments.report))
    except Exception as error:  # noqa: BLE001 - no raw network/credential data in acceptance failures.
        print(json.dumps({"passed": False, "error_type": type(error).__name__,
                          "detail": str(error) if isinstance(error, AssertionError) else None}))
        raise SystemExit(1) from None
