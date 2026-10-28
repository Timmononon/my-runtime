"""Exercise real tools/Engine offline, or an explicitly budgeted live model.

The deterministic driver emits tool calls only. It never reads answer oracles;
offline answer quality is always unsupported, even when protocol checks pass.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import tempfile
import threading
import time
from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from aster.customization.credentials import CredentialStore
from aster.domain import ModelTurn, ToolCall, ToolResult, ToolSpec, to_dict
from aster.engine import Engine, RunConfig
from aster.evaluation.oracle import evaluate, load_dataset
from aster.platform.filesystem import FileLock, atomic_write
from aster.policy import PermissionPolicy
from aster.providers.deepseek import DeepSeekResponsesClient
from aster.state import Store
from aster.tools.files import build_file_tools
from aster.tools.http import HTTPTool
from aster.tools.registry import Registry


class ProtocolDriver:
    """A known call sequence for testing the Runtime, not a language model."""
    def __init__(self, task):
        self.task = {key: copy.deepcopy(value) for key, value in task.items() if key != "oracle"}
        self.step = 0

    async def generate(self, messages, tools, *, system, emit=None, cancel=None):
        self.step += 1
        task, stage = self.task, self.step
        name, prompt = task["id"], task["prompt"]
        calls = []
        def call(tool, arguments):
            calls.append(ToolCall(f"eval-{stage}-{len(calls)}", tool, arguments))
        if name == "files_edit_preserves_context":
            if stage == 1:
                call("file_read", {"path": "config.txt"})
            elif stage == 2:
                result = next(m for m in reversed(messages) if m.role == "tool")
                version = re.search(r'"sha256"\s*:\s*"([0-9a-f]{64})"', result.content)
                if not version:
                    raise ValueError("Runtime failed to expose the observed edit hash")
                before, after = re.search(r"change only (\S+) to (\S+)\.", prompt).groups()
                call("file_edit", {"path": "config.txt", "old_text": before, "new_text": after, "expected_hash": version[1]})
        elif stage == 1 and name == "files_write_deliverable":
            content = prompt.split("no trailing newline: ", 1)[1].split(". Then", 1)[0]
            call("file_write", {"path": "brief.txt", "content": content})
        elif stage == 1 and name == "files_search_marker":
            call("text_search", {"path": ".", "query": re.search(r"beginning with (\w+)", prompt)[1]})
        elif "web_fetch" in task["allowed_tools"] and (stage == 1 or name == "recover_transient_fetch" and stage == 2):
            for url in task["initial_state"]["external"]:
                call("web_fetch", {"url": url})
        elif stage == 1 and "file_read" in task["allowed_tools"]:
            for path in task["initial_state"]["files"]:
                call("file_read", {"path": path})
        elif stage == 1 and "calendar_create" in task["allowed_tools"]:
            call("calendar_create", {"id": "team-sync"})
        elif stage == 1 and "send_email" in task["allowed_tools"]:
            call("send_email", {"to": "ops@example.com", "body": "Deploy window approved"})
        return ModelTurn(tool_calls=calls, text="" if calls else "Offline protocol run finished; no model answer was evaluated.")


class ExternalEffectFixture:
    """No external calendar/mail integration: this only tests material-call semantics."""
    def __init__(self, name, task, effects):
        self.task, self.effects = task, effects
        properties = {"id": {"type": "string"}} if name == "calendar_create" else {"to": {"type": "string"}, "body": {"type": "string"}}
        self.spec = ToolSpec(name, "Evaluation-only local side-effect fixture; never contacts an external service",
            {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}, risk="external")

    async def execute(self, arguments, context):
        key = "calendar:" + arguments["id"] if self.spec.name == "calendar_create" else "email:" + arguments["to"]
        fixture = self.task["initial_state"]["external"][key]
        if fixture["status"] == "outcome_unknown":
            self.effects[key] = "outcome_unknown"
            return ToolResult(fixture["content"] + "\nFixture receipt: " + fixture["receipt"], True, "transport", "unknown")
        return ToolResult(fixture["content"] + "\nFixture receipt: " + fixture["receipt"], True, "permission", "known")


@contextmanager
def local_sources(task):
    external = task["initial_state"]["external"]
    sites = {key: value for key, value in external.items() if key.startswith("https://")}
    if not sites:
        yield copy.deepcopy(task)
        return
    counts = Counter()
    paths = {"/" + hashlib.sha256(key.encode()).hexdigest()[:16]: (key, value) for key, value in sites.items()}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            if self.path not in paths:
                self.send_error(404)
                return
            _, fixture = paths[self.path]
            counts[self.path] += 1
            kind = fixture["status"]
            transient = kind == "transient" and counts[self.path] < fixture.get("succeed_after", 2)
            status = 503 if transient else {"blocked": 403, "not_found": 404}.get(kind, 200)
            text = "transient fixture failure" if transient else fixture["content"] + "\nFixture receipt: " + fixture["receipt"]
            body = text.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    encoded = json.dumps(task, ensure_ascii=False)
    for path, (original, _) in paths.items():
        encoded = encoded.replace(original, f"http://127.0.0.1:{server.server_port}{path}")
    try:
        yield json.loads(encoded)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class LiveBudget:
    def __init__(self, path, *, max_requests, max_output_tokens):
        if not isinstance(max_requests, int) or max_requests <= 0 or not 1 <= max_output_tokens <= 2048:
            raise ValueError("Live evaluation requires a positive explicit request budget and output limit <=2048")
        self.path, self.maximum, self.output_limit = Path(path), max_requests, max_output_tokens
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock():
            if not self.path.exists():
                self._write({"max_requests": max_requests, "max_output_tokens": max_output_tokens, "calls": []})
            elif self._read()["max_requests"] != max_requests or self._read()["max_output_tokens"] != max_output_tokens:
                raise ValueError("Existing live budget cannot be reset or enlarged")

    def _lock(self):
        return FileLock(self.path.with_suffix(".lock"), timeout=10)

    def _read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data):
        atomic_write(self.path, json.dumps(data, ensure_ascii=False).encode())

    async def generate(self, client, case_id, *args, **kwargs):
        with self._lock():
            data = self._read()
            if len(data["calls"]) >= self.maximum:
                raise RuntimeError("Explicit live evaluation budget is exhausted")
            index = len(data["calls"])
            data["calls"].append({"case_id": case_id, "started_at": time.time(), "usage": None, "status": "unknown"})
            self._write(data)
        turn = await client.generate(*args, **kwargs)
        with self._lock():
            data = self._read()
            data["calls"][index].update(status="success", usage=to_dict(turn.usage))
            self._write(data)
        return turn


class BudgetedModel:
    def __init__(self, client, budget, case_id):
        self.client, self.budget, self.case_id = client, budget, case_id
    async def generate(self, *args, **kwargs):
        return await self.budget.generate(self.client, self.case_id, *args, **kwargs)


def _files(workspace, *, ledger=(), initial_files=()):
    targets = {(workspace / row["arguments"]["path"]).resolve() for row in ledger
               if row["tool_name"] in {"file_write", "file_edit"} and "path" in row["arguments"]}
    known = {(workspace / name).resolve() for name in initial_files}
    locks = {path.parent / f".{path.name}.aster.lock" for path in targets} - targets - known
    return {path.relative_to(workspace).as_posix(): path.read_text(encoding="utf-8") for path in workspace.rglob("*")
            if path.is_file() and path.resolve() not in locks}


async def run_scenario(task, root, *, mode="offline", budget=None, credentials=None):
    if mode not in {"offline", "live"}:
        raise ValueError("Evaluation mode must be offline or live")
    if mode == "live" and budget is None:
        raise ValueError("Live evaluation requires an explicit independent budget")
    base = {"id": task["id"], "category": task["category"], "mode": mode,
            "expected_status": task["oracle"]["expected_status"],
            "model_quality": "unsupported" if mode == "offline" else "evaluated",
            "capability_scope": "runtime_protocol_only" if mode == "offline" else "model_with_local_fixtures"}
    if mode == "offline" and not task["allowed_tools"]:
        return {**base, "result": "unsupported", "reason": "Requires language-model writing/planning/clarification quality; no expected answer is injected"}
    if mode == "live" and set(task["allowed_tools"]) & {"send_email", "calendar_create"}:
        return {**base, "result": "unsupported", "model_quality": "unsupported", "reason": "Production mail/calendar integration is not implemented; offline fixtures only test Runtime boundaries"}
    workspace = Path(root) / task["id"] / "workspace"
    workspace.mkdir(parents=True)
    data = workspace.parent / "data"
    data.mkdir()
    for name, content in task["initial_state"]["files"].items():
        path = (workspace / name).resolve()
        if not path.is_relative_to(workspace.resolve()):
            raise ValueError("Evaluation fixture path escapes its workspace")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode())
    started = time.monotonic()
    store = Store(data / "runtime.db")
    client = None
    effects = copy.deepcopy(task["initial_state"]["effects"])
    try:
        with local_sources(task) as adapted:
            selected = adapted["allowed_tools"]
            tools = [tool for tool in build_file_tools() if tool.spec.name in selected]
            if "web_fetch" in selected:
                tools.append(HTTPTool(fetch=True, allowed_hosts=("127.0.0.1",)))
            for name in ("calendar_create", "send_email"):
                if name in selected:
                    tools.append(ExternalEffectFixture(name, adapted, effects))
            if mode == "offline":
                model = ProtocolDriver(adapted)
            else:
                credentials = credentials or CredentialStore()
                if not credentials.is_configured():
                    raise ValueError("DeepSeek credential is not configured")
                client = DeepSeekResponsesClient(lambda: credentials.get() or "", effort="low", max_output_tokens=budget.output_limit,
                    max_retries=0, artifact_root=data / "artifacts")
                model = BudgetedModel(client, budget, task["id"])
            session = store.create_session(workspace)
            engine = Engine(model, Registry(tools), store, policy=PermissionPolicy(allowed_tools=set(selected)),
                config=RunConfig(max_iterations=6, max_model_requests=6, max_tool_calls=12, model_retries=0,
                                 max_total_tokens=60000, reserve_tokens=2048, semantic_compaction=False),
                services={"http_allowed_hosts": ["127.0.0.1"]})
            outcome = await engine.run(session["id"], adapted["prompt"], system="Use available structured tools when required. State missing information and blockers truthfully. Do not fabricate completed actions.")
            ledger = store.tool_executions(outcome.run_id)
            messages = store.history(session["id"])
            results = [message for message in messages if message.role == "tool"]
            receipts = [match for message in results for match in re.findall(r"Fixture receipt: ([A-Za-z0-9_-]+)", message.content)]
            if outcome.status == "needs_reconciliation":
                status = "outcome_unknown"
            elif outcome.status != "completed":
                status = "failed"
            elif results and results[-1].metadata.get("is_error") or not results and any(marker in outcome.text.casefold() for marker in ("?", "？", "blocked", "please confirm", "请确认")):
                status = "blocked"
            else:
                status = "complete"
            observed = {"status": status, "answer": outcome.text, "calls": dict(Counter(row["tool_name"] for row in ledger)),
                        "receipts": receipts, "state": {"files": _files(workspace, ledger=ledger,
                        initial_files=task["initial_state"]["files"]), "effects": effects}}
            failures = evaluate(adapted, observed, include_answer=mode == "live")
            uncertain = [row for row in ledger if row["state"] == "outcome_unknown"]
            if status == "outcome_unknown" and (len(uncertain) != 1 or sum(observed["calls"].values()) != 1):
                failures.append("unknown_outcome_replayed_or_not_journaled")
            return {**base, "result": "fail" if failures else "pass", "failures": failures,
                    "observed": observed, "runtime_status": outcome.status, "elapsed_seconds": round(time.monotonic()-started, 4),
                    "usage": store.usage(outcome.run_id), "budget": store.run_budget(outcome.run_id),
                    "tool_outcomes": [{"name": row["tool_name"], "state": row["state"]} for row in ledger],
                    "integration": "simulated_calendar_mail_boundary" if set(selected) & {"calendar_create", "send_email"} else "actual_local_tools"}
    except Exception as error:  # noqa: BLE001 - each evaluation failure must be represented in the report.
        return {**base, "result": "fail", "failures": [type(error).__name__ + ": " + str(error)], "elapsed_seconds": round(time.monotonic()-started, 4)}
    finally:
        if client:
            await client.aclose()
        store.close()


def write_report(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(report, ensure_ascii=False, indent=2).encode())
    rows = ["# Aster outcome evaluation", "", "Mode: **" + report["mode"] + "**. Offline pass means Runtime protocol/state only; it is never a model-quality score.", "",
            "| Scenario | Category | Result | Expected / observed status | Model quality | Failures / unsupported reason |", "|---|---|---|---|---|---|"]
    for result in report["results"]:
        detail = result.get("reason") or "; ".join(result.get("failures", [])) or "Objective tool/state assertions passed"
        rows.append("| " + " | ".join(str(v).replace("|", "\\|").replace("\n", " ") for v in
                    (result["id"], result["category"], result["result"],
                     result.get("expected_status", "not exercised") + " / " + result.get("observed", {}).get("status", "not exercised"),
                     result["model_quality"], detail)) + " |")
    rows += ["", "Totals: " + json.dumps(report["totals"], sort_keys=True), "",
             "Mail/calendar fixtures do not demonstrate a real external integration. Pure writing/planning/voice-text cases are unsupported offline. No speech/audio is exercised."]
    atomic_write(path.with_suffix(".md"), "\n".join(rows).encode())


async def run_evaluation(output, *, mode="offline", case_ids=None, max_requests=None, max_output_tokens=1024):
    if mode not in {"offline", "live"}:
        raise ValueError("Evaluation mode must be offline or live")
    dataset = load_dataset()
    selected = [task for task in dataset["tasks"] if case_ids is None or task["id"] in case_ids]
    if not selected or case_ids and set(case_ids) != {task["id"] for task in selected}:
        raise ValueError("Unknown or empty evaluation scenario selection")
    output = Path(output)
    budget = LiveBudget(output.with_suffix(".budget.json"), max_requests=max_requests,
                        max_output_tokens=max_output_tokens) if mode == "live" else None
    report = {"schema_version": 1, "mode": mode, "dataset_origin": dataset["origin"],
              "dataset_digest": hashlib.sha256(json.dumps(dataset, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
              "paid_calls_authorized": mode == "live", "results": [], "totals": {}}
    with tempfile.TemporaryDirectory(prefix="aster-eval-") as scratch:
        for task in selected:
            report["results"].append(await run_scenario(task, Path(scratch), mode=mode, budget=budget))
            report["totals"] = dict(Counter(result["result"] for result in report["results"]))
            write_report(output, report)
    return report
