"""Translate API metadata into repository-qualified immutable event versions."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit

from aster.github.transport import GithubHTTPError
from aster.state.store import dumps


def repository(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", value):
        raise ValueError("Repository must be an owner/repository name")
    if value.split("/")[1] in {".", ".."}:
        raise ValueError("Repository name is invalid")
    return value


def number(value):
    if type(value) is not int or value < 1:
        raise ValueError("GitHub thread number must be a positive integer")
    return value


def repo_url(value, repo):
    if not isinstance(value, str):
        raise GithubHTTPError("invalid_body")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.username or parsed.password
            or parsed.query or "%" in parsed.path or "\\" in value
            or not parsed.path.casefold().startswith("/" + repo.casefold() + "/")):
        raise GithubHTTPError("invalid_repository_url")
    return value


def event(kind, row, repo, *, thread=None):
    if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0:
        raise GithubHTTPError("invalid_body")
    actor = row.get("user") or row.get("actor") or {}
    if not isinstance(actor, dict) or not isinstance(actor.get("login"), str):
        raise GithubHTTPError("invalid_actor")
    login = actor["login"]
    actor_type = ("bot" if actor.get("type") == "Bot" or login.endswith("[bot]")
                  else "human" if actor.get("type") == "User" else "unknown")
    if kind == "ci":
        actor_type = "automation"
    body = row.get("body") or (row.get("name") or "") if kind == "ci" else row.get("body") or ""
    if not isinstance(body, str) or len(body) > 100000:
        raise GithubHTTPError("invalid_body")
    at = row.get("updated_at") or row.get("submitted_at") or row.get("created_at")
    if not isinstance(at, str) or len(at) > 50:
        raise GithubHTTPError("invalid_timestamp")
    num = thread or row.get("number")
    thread_url = row.get("issue_url") or row.get("pull_request_url")
    if num is None and isinstance(thread_url, str):
        path = urlsplit(thread_url)
        expected = "/repos/" + repo + ("/pulls/" if row.get("pull_request_url") else "/issues/")
        if path.scheme != "https" or path.netloc != "api.github.com" or not path.path.casefold().startswith(expected.casefold()):
            raise GithubHTTPError("invalid_thread")
        tail = path.path.rsplit("/", 1)[-1]
        num = int(tail) if tail.isdigit() else None
    numbers = [number(num)] if num is not None else []
    if kind == "ci":
        associations = row.get("pull_requests", [])
        if not isinstance(associations, list):
            raise GithubHTTPError("invalid_body")
        numbers = [number(item["number"]) for item in associations if isinstance(item, dict) and "number" in item]
    url = repo_url(row.get("html_url"), repo)
    state = "merged" if row.get("merged") or row.get("merged_at") else row.get("state") or row.get("status")
    result = {"remote_id": row["id"], "kind": "pull_request" if kind == "issue" and row.get("pull_request") else kind,
              "number": numbers[0] if numbers else None, "numbers": numbers, "actor": login, "actor_type": actor_type,
              "at": at, "body": body, "title": row.get("title", ""), "url": url, "state": state,
              "conclusion": row.get("conclusion"), "head_sha": row.get("head_sha") or (row.get("head") or {}).get("sha"),
              "attempt": row.get("run_attempt"), "review_state": row.get("state") if kind == "review" else None}
    result["id"] = result["kind"] + ":" + str(row["id"]) + ":" + hashlib.sha256(dumps(result).encode()).hexdigest()[:24]
    return result
