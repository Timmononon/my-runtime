"""Local tickets, immutable worker assignments, admission and durable delivery.

No transaction spans an await or coordinator call. Work is queued before any
directory is created, and all model/tool execution stays in RunCoordinator.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

from aster.config import load_config, read_yaml, safe_identifier
from aster.engine import RunConfig
from aster.state.store import TERMINAL, SessionBusy, StateError, dumps, identifier
from aster.tasks.policy import checked_rules, evaluate


def _text(value, name, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be nonempty text within {limit} characters")
    return value.strip()


def _narrow_permissions(parent, target):
    result = {}
    left, right = parent.get("allowed_tools"), target.get("allowed_tools")
    result["allowed_tools"] = (sorted(set(left) & set(right)) if left is not None and right is not None
                               else copy.deepcopy(left if left is not None else right))
    for name in ("denied_tools", "ask_tools"):
        result[name] = sorted(set(parent.get(name, [])) | set(target.get(name, [])))
    auto = set(parent.get("auto_approve_tools", [])) & set(target.get("auto_approve_tools", []))
    if result["allowed_tools"] is not None:
        auto &= set(result["allowed_tools"])
    result["auto_approve_tools"] = sorted(auto - set(result["denied_tools"]) - set(result["ask_tools"]))
    return result


def _authority(settings):
    fields = {key: settings.get(key) for key in ("permissions", "allowed_roots", "http_allowed_hosts",
                                               "http_credential_origins", "http_dns_fallback", "mcp_servers", "hooks", "skills")}
    return hashlib.sha256(dumps(fields).encode()).hexdigest()


def _narrow_settings(parent, target):
    parent["permissions"] = _narrow_permissions(parent.get("permissions", {}), target.get("permissions", {}))
    left = [Path(root).resolve() for root in parent["allowed_roots"]]
    right = [Path(root).resolve() for root in target["allowed_roots"]]
    parent["allowed_roots"] = sorted({str(root) for root in left if any(root.is_relative_to(base) for base in right)}
                                     | {str(root) for root in right if any(root.is_relative_to(base) for base in left)})
    parent["http_allowed_hosts"] = sorted(set(parent.get("http_allowed_hosts", [])) & set(target.get("http_allowed_hosts", [])))
    if parent.get("http_dns_fallback") == "off" or target.get("http_dns_fallback") == "off":
        parent["http_dns_fallback"] = "off"
    original, current = parent.get("http_credential_origins", {}), target.get("http_credential_origins", {})
    parent["http_credential_origins"] = {key: sorted(set(value) & set(current.get(key, []))) for key, value in original.items()}
    current_servers = {item["name"]: item for item in target.get("mcp_servers", [])}
    parent["mcp_servers"] = [item for item in parent.get("mcp_servers", []) if current_servers.get(item["name"]) == item]
    if parent.get("hooks", []) != target.get("hooks", []):
        raise StateError("Task delegation cannot omit or import different executable hooks")
    if target.get("skills") is not None:
        parent["skills"] = ([name for name in parent["skills"] if name in target["skills"]]
                            if parent.get("skills") is not None else copy.deepcopy(target["skills"]))
    defaults = RunConfig()
    left_budget, right_budget = parent.get("runtime", {}), target.get("runtime", {})
    for name in ("max_model_requests", "max_total_tokens", "max_tool_calls", "max_iterations", "deadline_seconds"):
        parent.setdefault("runtime", {})[name] = min(left_budget.get(name, getattr(defaults, name)), right_budget.get(name, getattr(defaults, name)))


class TaskService:
    def __init__(self, coordinator, *, poll_seconds=0.25):
        self.coordinator, self.store = coordinator, coordinator.store
        self.poll_seconds = poll_seconds
        self._runner = None
        self._stop = asyncio.Event()
        self._tick_lock = asyncio.Lock()
        with self.store.transaction() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS local_task_settings(
                id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, mode TEXT NOT NULL, rules_json TEXT NOT NULL);
              INSERT OR IGNORE INTO local_task_settings VALUES(1,0,'observe','[]');
              CREATE TABLE IF NOT EXISTS local_task_tickets(
                id TEXT PRIMARY KEY, source_session_id TEXT NOT NULL, parent_run_id TEXT,
                title TEXT NOT NULL, goal TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL,
                summary TEXT NOT NULL, attention TEXT, policy_revision INTEGER NOT NULL,
                request_id TEXT, unattended INTEGER NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
                origin TEXT NOT NULL, matched_rules_json TEXT NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(source_session_id,request_id));
              CREATE TABLE IF NOT EXISTS local_task_workers(
                id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, position INTEGER NOT NULL,
                title TEXT NOT NULL, goal TEXT NOT NULL, agent_id TEXT NOT NULL, role TEXT NOT NULL,
                state TEXT NOT NULL, session_id TEXT NOT NULL UNIQUE, cwd TEXT NOT NULL, run_id TEXT,
                snapshot_json TEXT NOT NULL, answer TEXT NOT NULL, error TEXT,
                attempt INTEGER NOT NULL DEFAULT 0, prompt TEXT NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL);
              CREATE INDEX IF NOT EXISTS local_task_worker_ticket ON local_task_workers(ticket_id,position);
              CREATE TABLE IF NOT EXISTS local_task_bindings(
                session_id TEXT PRIMARY KEY, binding_json TEXT NOT NULL, retired INTEGER NOT NULL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS local_task_candidates(
                id TEXT PRIMARY KEY, source_session_id TEXT NOT NULL, request_id TEXT NOT NULL,
                source TEXT NOT NULL, text TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL,
                ticket_id TEXT, policy_revision INTEGER NOT NULL, created_at REAL NOT NULL,
                UNIQUE(source_session_id,request_id));
              CREATE TABLE IF NOT EXISTS local_task_inputs(
                id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, request_id TEXT NOT NULL,
                text TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(ticket_id,request_id));
              CREATE TABLE IF NOT EXISTS local_task_input_deliveries(
                input_id TEXT NOT NULL, worker_id TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(input_id,worker_id));
            """)
            for row in db.execute("SELECT session_id,snapshot_json FROM local_task_workers").fetchall():
                db.execute("INSERT OR IGNORE INTO local_task_bindings(session_id,binding_json) VALUES(?,?)",
                           (row["session_id"], dumps(json.loads(row["snapshot_json"])["snapshot"]["_local_task"])))

    def _limits(self):
        config = read_yaml(self.coordinator.data_dir / "config.yaml").get("local_tasks", {})
        values = {"max_workers_per_ticket": config.get("max_workers_per_ticket", 8),
                  "max_open_tickets": config.get("max_open_tickets", 64),
                  "max_attempts_per_worker": config.get("max_attempts_per_worker", 3)}
        if any(type(value) is not int or value < 1 for value in values.values()):
            raise ValueError("Local task limits must be positive integers")
        return values

    def settings(self):
        with self.store._lock:
            row = self.store.connection.execute("SELECT * FROM local_task_settings WHERE id=1").fetchone()
        return {"revision": row["revision"], "mode": row["mode"], "rules": json.loads(row["rules_json"]),
                "max_workers": self._limits()["max_workers_per_ticket"]}

    def update_settings(self, expected_revision, mode, rules):
        if mode not in {"off", "observe", "auto"}:
            raise ValueError("Task mode must be off, observe or auto")
        rules = checked_rules(rules)
        with self.store.transaction() as db:
            changed = db.execute("UPDATE local_task_settings SET revision=revision+1,mode=?,rules_json=? "
                                 "WHERE id=1 AND revision=?", (mode, dumps(rules), expected_revision))
            if not changed.rowcount:
                raise StateError("Task settings changed; reload before saving")
        return self.settings()

    def _capture(self, session_id, parent_run_id=None, execution_snapshot=None):
        source = self.store.get_session(session_id)
        self.store.require_session_writable(session_id)
        if self.worker_binding(session_id):
            raise StateError("Workers cannot recursively delegate tasks")
        if parent_run_id:
            run = self.store.get_run(parent_run_id)
            if run["session_id"] != session_id:
                raise StateError("The originating run belongs to another conversation")
            payload = self.store.run_request(parent_run_id)
            from aster.service.coordinator import _SYSTEM_PREAMBLE
            captured = {key: copy.deepcopy(payload[key]) for key in
                        ("snapshot", "version", "instructions", "agent_id", "project_id", "selection", "selected_skill_snapshot")}
            system = _SYSTEM_PREAMBLE + captured["instructions"]
            if captured["selected_skill_snapshot"]:
                system += "\n\nSelected skill workflow:\n" + captured["selected_skill_snapshot"]["content"]
            captured.update(system=system, system_without_session_notes=system, source_session_id=session_id)
        elif execution_snapshot is not None:
            captured = copy.deepcopy(execution_snapshot)
            if (captured["agent_id"] != source["agent_id"] or captured["project_id"] != source["project_id"]
                    or Path(captured["snapshot"]["cwd"]).resolve() != Path(source["cwd"]).resolve()):
                raise StateError("Task candidate snapshot no longer matches its source")
        else:
            captured = self.coordinator.capture_automation_execution(
                session_id, selection=self.coordinator.session_selection(session_id))
        # Unknown outcomes are never turned into another worker, including when
        # the caller's own delegation intent is still dispatching.
        sessions = [session_id, *self.store.session_ancestors(session_id)]
        marks = ",".join("?" for _ in sessions)
        with self.store._lock:
            unknown = self.store.connection.execute(
                f"SELECT 1 FROM tool_executions WHERE session_id IN ({marks}) AND state='outcome_unknown' LIMIT 1", sessions).fetchone()
        if unknown:
            raise StateError("Reconcile source actions before creating a task")
        if captured["snapshot"].get("_provider"):
            self.coordinator.providers.validate_pin(captured["snapshot"]["_provider"])
        return source, captured

    def _prepare(self, source_session_id, title, goal, workers, request_id, parent_run_id,
                 unattended, *, execution_snapshot=None, origin="explicit", matches=(), tool_allowance=None):
        title, goal = _text(title, "Task title", 200), _text(goal, "Task goal", 100000)
        if request_id is not None:
            request_id = _text(request_id, "Request ID", 200)
        limits = self._limits()
        workers = [{"title": title, "goal": goal}] if workers is None else workers
        if not isinstance(workers, list) or not 1 <= len(workers) <= limits["max_workers_per_ticket"]:
            raise ValueError("Worker count exceeds the configured local task limit")
        source, captured = self._capture(source_session_id, parent_run_id, execution_snapshot)
        if parent_run_id:
            unattended = bool(unattended or self.store.run_request(parent_run_id).get("unattended"))
        ticket_id, now = identifier("task"), time.time()
        settings = self.settings()
        item = {"id": ticket_id, "source_session_id": source_session_id, "parent_run_id": parent_run_id,
                "title": title, "goal": goal, "state": "queued", "revision": 0, "summary": "", "attention": None,
                "policy_revision": settings["revision"], "request_id": request_id, "unattended": bool(unattended),
                "origin": origin, "matches": list(matches), "created_at": now, "updated_at": now, "workers": []}
        source_settings = captured["snapshot"]
        current_source = load_config(self.coordinator.data_dir, agent_id=source["agent_id"], project_id=source["project_id"], cwd=Path(source["cwd"]))
        current_source_settings = self.coordinator._scope_settings(current_source, source)
        source_authority = _authority(current_source_settings)
        _narrow_settings(source_settings, current_source_settings)
        if tool_allowance is not None:
            ceiling = set(tool_allowance)
            allowed = source_settings["permissions"].get("allowed_tools")
            source_settings["permissions"]["allowed_tools"] = sorted(ceiling if allowed is None else ceiling & set(allowed))
            source_settings["permissions"]["auto_approve_tools"] = sorted(
                ceiling & set(source_settings["permissions"].get("auto_approve_tools", [])))
        roots = [Path(root).resolve() for root in source_settings.get("allowed_roots", [source["cwd"]])]
        if not roots:
            raise StateError("Source grants no task workspace")
        source_cwd = Path(source["cwd"]).resolve()
        workspace = source_cwd if any(source_cwd.is_relative_to(root) for root in roots) else roots[0]
        for position, worker in enumerate(workers):
            if not isinstance(worker, dict) or set(worker) - {"title", "goal", "agent_id", "role"}:
                raise ValueError("Worker fields are invalid")
            worker_title = _text(worker.get("title"), "Worker title", 200)
            worker_goal = _text(worker.get("goal"), "Worker goal", 100000)
            role, agent_id = worker.get("role", "worker"), safe_identifier(worker.get("agent_id", source["agent_id"]))
            if role not in {"worker", "reader"}:
                raise ValueError("Worker role must be worker or reader")
            wid, sid = identifier("worker"), identifier("ses")
            cwd = (workspace / ".aster" / "work" / ticket_id / wid).resolve()
            if not cwd.is_relative_to(workspace) or not any(cwd.is_relative_to(root) for root in roots):
                raise StateError("Task directory escaped its source workspace")
            pin = copy.deepcopy(captured)
            data = pin["snapshot"]
            target_hash = None
            if agent_id != source["agent_id"]:
                target = load_config(self.coordinator.data_dir, agent_id=agent_id, project_id=source["project_id"], cwd=source_cwd)
                target_settings = self.coordinator._scope_settings(target, {**source, "agent_id": agent_id})
                target_hash = _authority(target_settings)
                _narrow_settings(data, target_settings)
                if not any(cwd.is_relative_to(Path(root)) for root in data["allowed_roots"]):
                    raise StateError("Selected Agent does not grant the task directory")
                # Additional executable extension authority cannot be imported
                # from another Agent. Its instructions and denials may narrow.
                pin["instructions"] += "\n\nAdditional worker Agent instructions:\n" + target.instructions
                pin["system_without_session_notes"] = pin.get("system_without_session_notes", pin["system"]) + "\n\n" + target.instructions
            pin["agent_id"] = agent_id
            data["cwd"] = str(cwd)
            data["_local_task"] = {"ticket_id": ticket_id, "worker_id": wid, "source_session_id": source_session_id,
                                   "parent_run_id": parent_run_id, "role": role,
                                   "source_authority": source_authority, "target_authority": target_hash,
                                   "source_agent_id": source["agent_id"], "source_cwd": source["cwd"]}
            run_config = self.coordinator._run_config(data)
            runtime = data.setdefault("runtime", {})
            for name in ("max_model_requests", "max_total_tokens", "max_tool_calls", "max_iterations"):
                available = getattr(run_config, name)
                if available < len(workers):
                    raise ValueError("Task worker count exceeds its shared execution budget")
                runtime[name] = available // len(workers)
            suffix = (f"\n\nLocal task {ticket_id}. Assigned work: {worker_goal}\n"
                      f"Original workspace: {source['cwd']}. Own working directory: {cwd}. "
                      "Keep writes within your assigned scope; coordinate shared files through the task owner. "
                      "Do not delegate another task. Report actual results and incomplete work honestly.")
            if role == "reader":
                suffix += " This is a reader assignment: only observational tools are allowed."
            pin["system_without_session_notes"] = pin.get("system_without_session_notes", pin["system"]) + suffix
            pin["system"] = pin["system_without_session_notes"]
            item["workers"].append({"id": wid, "session_id": sid, "title": worker_title, "goal": worker_goal,
                                    "agent_id": agent_id, "role": role, "cwd": str(cwd), "snapshot": pin})
        return item

    def _insert(self, db, item, admission=None):
        self.store._require_session_writable(db, item["source_session_id"])
        count = db.execute("SELECT COUNT(*) FROM local_task_tickets WHERE state NOT IN ('completed','failed','cancelled')").fetchone()[0]
        if count >= self._limits()["max_open_tickets"]:
            raise SessionBusy("Configured open local task limit reached")
        db.execute("INSERT INTO local_task_tickets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (item["id"], item["source_session_id"], item["parent_run_id"], item["title"], item["goal"],
                    "queued", 0, "", None, item["policy_revision"], item["request_id"], int(item["unattended"]), 0,
                    item["origin"], dumps(item["matches"]), item["created_at"], item["updated_at"]))
        source = self.store.get_session(item["source_session_id"])
        for worker in item["workers"]:
            # Empty sessions and task bindings commit together. Directory creation
            # happens later, so a crash leaves a retryable known queued intent.
            db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?)", (worker["session_id"], worker["cwd"], worker["agent_id"],
                       source["project_id"], worker["title"], item["created_at"], item["created_at"]))
            db.execute("INSERT INTO local_task_workers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (worker["id"], item["id"], item["workers"].index(worker), worker["title"], worker["goal"], worker["agent_id"],
                        worker["role"], "queued", worker["session_id"], worker["cwd"], None, dumps(worker["snapshot"]), "", None,
                        0, worker["goal"], item["created_at"], item["created_at"]))
            db.execute("INSERT INTO local_task_bindings(session_id,binding_json) VALUES(?,?)",
                       (worker["session_id"], dumps(worker["snapshot"]["snapshot"]["_local_task"])))
        if admission is not None:
            # Trusted source adapters may bind external authority atomically
            # with runnable Worker rows. The callback is pure SQLite: no await,
            # network, credential access or filesystem work inside this fence.
            admission(db, item["id"])
        self.store._event(db, item["source_session_id"], item["parent_run_id"], "task.created", {"ticket_id": item["id"], "workers": len(item["workers"])})

    def _existing(self, session_id, request_id):
        if request_id is None:
            return None
        with self.store._lock:
            row = self.store.connection.execute("SELECT id FROM local_task_tickets WHERE source_session_id=? AND request_id=?",
                                                (session_id, request_id)).fetchone()
        return self.get_ticket(row[0]) if row else None

    def _source_unknown(self, session_id):
        return self._source_unknown_at(session_id) is not None

    def _source_unknown_at(self, session_id):
        sessions = [session_id, *self.store.session_ancestors(session_id)]
        with self.store._lock:
            return self.store.connection.execute("SELECT MIN(updated_at) FROM tool_executions WHERE session_id IN (" +
                    ",".join("?" for _ in sessions) + ") AND state='outcome_unknown'", sessions).fetchone()[0]

    def create_ticket(self, source_session_id, title, goal, workers=None, request_id=None, parent_run_id=None, unattended=False,
                      tool_allowance=None, admission=None):
        if admission is not None and not callable(admission):
            raise ValueError("Task admission must be an internal source callback")
        if existing := self._existing(source_session_id, request_id):
            return existing
        item = self._prepare(source_session_id, title, goal, workers, request_id, parent_run_id, unattended, tool_allowance=tool_allowance)
        with self.store.transaction() as db:
            if existing := self._existing(source_session_id, request_id):
                return existing
            self._insert(db, item, admission=admission)
        return self.get_ticket(item["id"])

    def evaluate_candidate(self, source_session_id, text, request_id, source="local", execution_snapshot=None, admission=None):
        if admission is not None and not callable(admission):
            raise ValueError("Task admission must be an internal source callback")
        text, request_id = _text(text, "Candidate text", 100000), _text(request_id, "Request ID", 200)
        if source not in {"local", "schedule", "watcher", "heartbeat", "github"}:
            raise ValueError("Unsupported local candidate source")
        with self.store._lock:
            prior = self.store.connection.execute("SELECT * FROM local_task_candidates WHERE source_session_id=? AND request_id=?",
                                                  (source_session_id, request_id)).fetchone()
        if prior:
            return dict(prior)
        self.store.require_session_writable(source_session_id)
        policy = self.settings()
        decision, reason, matches = evaluate(text, policy["mode"], policy["rules"])
        item = None
        if decision == "accepted":
            item = self._prepare(source_session_id, text[:200], text, None, "candidate:" + hashlib.sha256(request_id.encode()).hexdigest(), None, True,
                                 execution_snapshot=execution_snapshot, origin="inferred", matches=matches)
        identity, now = identifier("candidate"), time.time()
        with self.store.transaction() as db:
            current = db.execute("SELECT revision FROM local_task_settings WHERE id=1").fetchone()[0]
            if current != policy["revision"]:
                raise StateError("Task policy changed during candidate admission")
            prior = db.execute("SELECT * FROM local_task_candidates WHERE source_session_id=? AND request_id=?",
                               (source_session_id, request_id)).fetchone()
            if prior:
                return dict(prior)
            if item:
                self._insert(db, item, admission=admission)
            db.execute("INSERT INTO local_task_candidates VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (identity, source_session_id, request_id, source, text, decision, reason,
                        item["id"] if item else None, policy["revision"], now))
            self.store._event(db, source_session_id, None, "task.candidate", {"candidate_id": identity, "decision": decision})
        return {"id": identity, "source_session_id": source_session_id, "request_id": request_id, "source": source,
                "decision": decision, "reason": reason, "ticket_id": item["id"] if item else None,
                "policy_revision": policy["revision"], "created_at": now}

    def list_candidates(self, source_session_id=None):
        with self.store._lock:
            rows = self.store.connection.execute("SELECT * FROM local_task_candidates" +
                    (" WHERE source_session_id=?" if source_session_id else "") + " ORDER BY created_at DESC",
                    (source_session_id,) if source_session_id else ()).fetchall()
        return [dict(row) for row in rows]

    def _workers(self, ticket_id):
        with self.store._lock:
            return [dict(row) for row in self.store.connection.execute(
                "SELECT * FROM local_task_workers WHERE ticket_id=? ORDER BY position", (ticket_id,)).fetchall()]

    def worker_binding(self, session_id):
        with self.store._lock:
            row = self.store.connection.execute("SELECT * FROM local_task_bindings WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            return None
        binding = json.loads(row["binding_json"])
        return {**binding, "reader": binding["role"] == "reader", "retired": bool(row["retired"])}

    def worker_execution(self, session_id):
        self.worker_guard(session_id)
        with self.store._lock:
            row = self.store.connection.execute("SELECT snapshot_json FROM local_task_workers WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise StateError("The conversation is not a task worker")
        return json.loads(row[0])

    def interaction_guard(self, session_id, run_id=None):
        """A real task owner can answer its Workbench inbox, without new grants."""
        binding = self.worker_guard(session_id, run_id)
        if binding is None:
            raise StateError("A task interaction channel requires a durable Worker binding")
        return binding

    def runtime_context(self, session_id):
        """Return current trusted routing metadata, never quoted event authority."""
        binding = self.worker_guard(session_id)
        if binding is None:
            return None
        worker = self.store.get_session(session_id)
        value = {"ticket_id": binding["ticket_id"], "worker_id": binding["worker_id"],
                 "role": binding["role"], "workspace": binding["source_cwd"], "cwd": worker["cwd"],
                 # Platform hints must not block worker admission on Windows WMI.
                 "host_platform": {"win32": "Windows", "darwin": "Darwin", "linux": "Linux"}.get(sys.platform, sys.platform),
                 "available_powershell_backends": [backend for executable, backend in
                    (("pwsh", "powershell7"), ("powershell", "powershell51")) if shutil.which(executable)],
                 "shell_exec_parameters": ["backend", "script", "cwd"],
                 "guidance": ["The Worker directory is separate from the original workspace; inspect existing files before assuming they exist.",
                    "Use the exact GitHub source_id below, or github_read resource=sources to discover authorized sources. A repository name or Issue ID is not a source ID.",
                    "Use an available PowerShell backend on Windows; WSL requires an explicit distribution and absolute linux_cwd.",
                    "Tool permissions are unchanged. Required approvals and questions appear in the supervising task's human inbox."]}
        github = getattr(self.coordinator, "github", None)
        if github is not None:
            metadata = github.worker_context(session_id)
            if metadata is not None:
                value["github"] = metadata
        # Large multi-source tasks fall back to explicit scoped discovery. This
        # limits prompt overhead without hiding or widening any source authority.
        if len(dumps(value)) > 16000:
            value.pop("github", None)
            value["github_discovery"] = {"tool": "github_read", "resource": "sources"}
        return value

    def worker_guard(self, session_id, run_id=None):
        binding = self.worker_binding(session_id)
        if binding is None:
            return None
        if binding.get("retired"):
            raise StateError("This worker's task owner was permanently removed")
        with self.store._lock:
            ticket = self.store.connection.execute("SELECT * FROM local_task_tickets WHERE id=?", (binding["ticket_id"],)).fetchone()
        if ticket is None or ticket["cancel_requested"]:
            raise StateError("The local task has been cancelled")
        github = getattr(self.coordinator, "github", None)
        if github is not None:
            github.guard_ticket(binding["ticket_id"])
        source_id = binding["source_session_id"]
        source = self.store.get_session(source_id)
        self.store.require_session_writable(source_id)
        if self.store.has_unresolved(source_id):
            raise StateError("Source conversation has unresolved material effects")
        parent = binding.get("parent_run_id")
        if parent and self.store.get_run(parent)["status"] == "cancelled":
            raise StateError("The originating run was cancelled")
        current = load_config(self.coordinator.data_dir, agent_id=source["agent_id"], project_id=source["project_id"], cwd=Path(source["cwd"]))
        current_settings = self.coordinator._scope_settings(current, source)
        if _authority(current_settings) != binding["source_authority"]:
            raise StateError("Source task authority changed; create a new task under the current policy")
        worker = self.store.get_session(session_id)
        pin = self._worker_execution_unchecked(session_id)
        owned = Path(worker["cwd"])
        if owned.resolve() != owned or not any(owned.resolve().is_relative_to(Path(root).resolve())
                                              for root in pin["snapshot"]["allowed_roots"]):
            raise StateError("Worker directory was redirected outside its captured ownership")
        if binding.get("target_authority"):
            target = load_config(self.coordinator.data_dir, agent_id=worker["agent_id"], project_id=worker["project_id"], cwd=Path(source["cwd"]))
            target_settings = self.coordinator._scope_settings(target, {**source, "agent_id": worker["agent_id"]})
            if _authority(target_settings) != binding["target_authority"]:
                raise StateError("Worker Agent authority changed; create a new task")
        return binding

    def _worker_execution_unchecked(self, session_id):
        with self.store._lock:
            row = self.store.connection.execute("SELECT snapshot_json FROM local_task_workers WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise StateError("The task worker assignment was removed")
        return json.loads(row[0])

    def _outstanding_permission_denials(self, run_id):
        """Use canonical structured outcomes, never classify an assistant's prose."""
        with self.store._lock:
            rows = self.store.connection.execute("SELECT message_json FROM messages WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
        pending, calls = set(), {}
        for row in rows:
            message = json.loads(row[0])
            if message.get("role") == "assistant":
                for call in message.get("tool_calls", []):
                    calls[call["id"]] = (call["name"], dumps(call.get("arguments", {})))
            if message.get("role") != "tool":
                continue
            metadata = message.get("metadata", {})
            tool = calls.get(message.get("tool_call_id"), (metadata.get("tool_name") or "unknown_tool", message.get("tool_call_id")))
            if metadata.get("is_error") and metadata.get("error_category") == "permission":
                pending.add(tool)
            elif not metadata.get("is_error"):
                pending.discard(tool)
        return sorted({tool[0] for tool in pending})

    def _public_worker(self, worker):
        value = {key: worker[key] for key in ("id", "title", "goal", "agent_id", "role", "state", "session_id", "run_id", "cwd", "answer", "error")}
        value["pending_requests"] = []
        try:
            self.store.get_session(worker["session_id"])
        except KeyError:
            value.update(state="cancelled", error="工作对话已删除。", removed=True)
            return value
        if worker["run_id"]:
            try:
                run = self.store.get_run(worker["run_id"])
            except KeyError:
                value.update(state="failed", error="Worker conversation was removed")
                return value
            value["pending_requests"] = self.store.pending_requests(run["id"])
            value["answer"], value["error"] = run["answer"], run["error"]
            state = run["status"]
            value["run_status"] = state
            if state in {"running", "cancelling"}:
                value["state"] = "needs_reconciliation" if worker["state"] == "needs_reconciliation" else "working"
            elif state in {"waiting_approval", "waiting_input"}:
                value["state"] = "waiting"
            elif state in {"timed_out", "budget_exhausted"}:
                value["state"] = "failed"
            else:
                value["state"] = state
            if self.store.has_unresolved(worker["session_id"]) and state in TERMINAL:
                value["state"] = "needs_reconciliation"
            elif state == "completed" and self._outstanding_permission_denials(run["id"]):
                value["state"] = "failed"
                value["error"] = "任务仍有未获授权的操作，需要检查权限或明确剩余工作后重试。"
        return value

    @staticmethod
    def _aggregate(ticket, workers):
        states = {worker["state"] for worker in workers}
        if "needs_reconciliation" in states or ticket["state"] == "needs_reconciliation" and not ticket["cancel_requested"]:
            return "needs_reconciliation", "执行或副作用需要核验，请打开工作对话处理后重试。"
        if ticket["cancel_requested"]:
            if states & {"queued", "working", "waiting"}:
                return "working", "正在取消并等待已开始的操作结束。"
            return "cancelled", None
        if "waiting" in states:
            return "waiting", "工作任务需要你的批准或回答。"
        if "working" in states:
            return "working", None
        if "queued" in states:
            return "queued", ticket["attention"]
        if states <= {"completed"}:
            return "completed", None
        if "failed" in states:
            return "failed", "部分工作未完成，请检查错误与已有结果。"
        return "cancelled", None

    def get_ticket(self, ticket_id):
        with self.store._lock:
            row = self.store.connection.execute("SELECT * FROM local_task_tickets WHERE id=?", (ticket_id,)).fetchone()
        if row is None:
            raise KeyError(ticket_id)
        value = dict(row)
        value.pop("matched_rules_json")
        workers = [self._public_worker(worker) for worker in self._workers(ticket_id)]
        value["state"], value["attention"] = self._aggregate(value, workers)
        with self.store._lock:
            pending_input = self.store.connection.execute("SELECT 1 FROM local_task_inputs i JOIN local_task_input_deliveries d "
                    "ON d.input_id=i.id WHERE i.ticket_id=? AND d.delivered=0 LIMIT 1", (ticket_id,)).fetchone()
        if pending_input and not value["cancel_requested"] and value["state"] in {"completed", "failed", "cancelled"}:
            value["state"], value["attention"] = "queued", None
        unknown_at = self._source_unknown_at(value["source_session_id"])
        overlapped_completion = unknown_at is not None and any(
            worker["run_id"] and not worker.get("removed")
            and self.store.get_run(worker["run_id"])["updated_at"] >= unknown_at for worker in workers)
        if unknown_at is not None and (value["state"] in {"queued", "working", "waiting"} or overlapped_completion):
            value["state"], value["attention"] = "needs_reconciliation", "来源对话有结果未知的操作，需要核验后再继续任务。"
        value["workers"] = workers
        value["unattended"] = bool(value["unattended"])
        labels = {"queued": "排队中", "working": "执行中", "waiting": "等待处理", "completed": "已完成",
                  "failed": "执行失败", "cancelled": "已取消", "needs_reconciliation": "结果待核验"}
        value["summary"] = "\n\n".join(f"{worker['title']} [{labels.get(worker['state'], '状态待核验')}]\n"
                                           f"{worker['answer'] or worker['error'] or '尚无结果'}" for worker in workers)
        value["can_cancel"] = not value["cancel_requested"] and value["state"] in {"queued", "working", "waiting"}
        unresolved = self.store.has_unresolved(value["source_session_id"]) or any(
            self.store.has_unresolved(worker["session_id"]) for worker in workers if not worker.get("removed"))
        value["can_retry"] = (value["state"] in {"failed", "needs_reconciliation"} and not value["cancel_requested"]
                              and not unresolved and not any(worker.get("removed") for worker in workers))
        value["can_add_input"] = (not value["cancel_requested"] and value["state"] != "needs_reconciliation"
                                  and not unresolved and not any(worker.get("removed") for worker in workers))
        github = getattr(self.coordinator, "github", None)
        if github is not None and (projection := github.task_projection(ticket_id)):
            value["github"] = projection
            if projection.get("blocked"):
                value["can_retry"] = value["can_add_input"] = False
                value["attention"] = projection.get("attention") or "GitHub 来源授权已变化，请核验来源后创建新的任务。"
                if value["state"] in {"queued", "working", "waiting"}:
                    value["state"] = "needs_reconciliation"
        return value

    def list_tickets(self, source_session_id=None):
        with self.store._lock:
            rows = self.store.connection.execute("SELECT id FROM local_task_tickets" +
                    (" WHERE source_session_id=?" if source_session_id else "") + " ORDER BY created_at DESC",
                    (source_session_id,) if source_session_id else ()).fetchall()
        return [self.get_ticket(row[0]) for row in rows]

    def overview(self):
        tickets = self.list_tickets()
        return {"tickets": tickets, "counts": {"total": len(tickets),
                "active": sum(item["state"] in {"queued", "working", "waiting"} for item in tickets),
                "attention": sum(bool(item["attention"]) for item in tickets),
                "completed": sum(item["state"] == "completed" for item in tickets)}}

    def _check_revision(self, ticket, revision):
        if revision is not None and ticket["revision"] != revision:
            raise StateError("Task changed; reload before changing it")

    async def cancel_ticket(self, ticket_id, expected_revision=None):
        ticket = self.get_ticket(ticket_id)
        self._check_revision(ticket, expected_revision)
        self.store.require_session_writable(ticket["source_session_id"])
        if not ticket["can_cancel"] and not ticket["cancel_requested"]:
            return ticket
        for worker in self._workers(ticket_id):
            self._attach_existing(worker)
        with self.store.transaction() as db:
            db.execute("UPDATE local_task_tickets SET cancel_requested=1,revision=revision+1,updated_at=? WHERE id=?", (time.time(), ticket_id))
            db.execute("UPDATE local_task_workers SET state='cancelled' WHERE ticket_id=? AND run_id IS NULL", (ticket_id,))
        for worker in self._workers(ticket_id):
            if worker["run_id"] and self.store.get_run(worker["run_id"])["status"] not in TERMINAL:
                self.coordinator.cancel(worker["run_id"])
        # Runtime cancellation owns draining material calls; ticket status remains
        # working while they settle, never prematurely claims cancellation.
        return self.get_ticket(ticket_id)

    def _remaining_snapshot(self, worker):
        pin = json.loads(worker["snapshot_json"])
        runtime = pin["snapshot"]["runtime"]
        runs = self.store.list_runs(worker["session_id"])
        used_requests = sum(len(self.store.usage(run["id"])) for run in runs)
        used_tools = sum(len(self.store.tool_executions(run["id"])) for run in runs)
        used_tokens = sum((entry.get("input_tokens") or 0) + (entry.get("output_tokens") or 0)
                          for run in runs for entry in self.store.usage(run["id"]))
        for name, used in (("max_model_requests", used_requests), ("max_tool_calls", used_tools), ("max_total_tokens", used_tokens)):
            runtime[name] -= used
            if runtime[name] < 1:
                raise StateError("This task's shared execution budget is exhausted; create a new authorized task")
        return pin

    async def retry_ticket(self, ticket_id, expected_revision=None):
        ticket = self.get_ticket(ticket_id)
        self._check_revision(ticket, expected_revision)
        if not ticket["can_retry"]:
            raise StateError("Only failed or reconciled tasks can be retried")
        changed = []
        for worker in self._workers(ticket_id):
            self.worker_guard(worker["session_id"])
            if self.store.has_unresolved(worker["session_id"]):
                raise StateError("Reconcile worker effects before retrying")
            if worker["run_id"] and self.store.get_run(worker["run_id"])["status"] not in TERMINAL:
                raise SessionBusy("Recover or cancel the interrupted worker run first")
            if self._public_worker(worker)["state"] != "completed":
                if worker["attempt"] >= self._limits()["max_attempts_per_worker"]:
                    raise StateError("Configured worker attempt limit reached")
                self._remaining_snapshot(worker)
                changed.append(worker["id"])
        with self.store.transaction() as db:
            db.execute("UPDATE local_task_tickets SET state='queued',attention=NULL,revision=revision+1,updated_at=? WHERE id=?", (time.time(), ticket_id))
            for wid in changed:
                db.execute("UPDATE local_task_workers SET state='queued',run_id=NULL,error=NULL,prompt=? WHERE id=?",
                           ("Inspect the previous attempt and actual state, then continue the remaining assigned work. Do not repeat confirmed side effects.", wid))
        return self.get_ticket(ticket_id)

    async def add_input(self, ticket_id, text, request_id=None, expected_revision=None):
        text = _text(text, "Task input", 100000)
        request_id = _text(request_id, "Request ID", 200) if request_id is not None else identifier("input")
        ticket = self.get_ticket(ticket_id)
        with self.store._lock:
            if self.store.connection.execute("SELECT 1 FROM local_task_inputs WHERE ticket_id=? AND request_id=?", (ticket_id, request_id)).fetchone():
                return ticket
        self._check_revision(ticket, expected_revision)
        if not ticket["can_add_input"]:
            raise StateError("This task cannot accept more input")
        workers = self._workers(ticket_id)
        for worker in workers:
            self.worker_guard(worker["session_id"])
            if self.store.has_unresolved(worker["session_id"]):
                raise StateError("Wait for worker effects to settle before adding input")
            self._remaining_snapshot(worker)
        identity = identifier("input")
        with self.store.transaction() as db:
            db.execute("INSERT INTO local_task_inputs VALUES(?,?,?,?,?)", (identity, ticket_id, request_id, text, time.time()))
            db.execute("UPDATE local_task_tickets SET state='queued',attention=NULL,revision=revision+1,updated_at=? WHERE id=?", (time.time(), ticket_id))
            for worker in workers:
                db.execute("INSERT INTO local_task_input_deliveries(input_id,worker_id) VALUES(?,?)", (identity, worker["id"]))
        return self.get_ticket(ticket_id)

    def _deliver_inputs(self, worker):
        with self.store._lock:
            rows = self.store.connection.execute("SELECT i.id,i.text FROM local_task_inputs i JOIN local_task_input_deliveries d "
                    "ON d.input_id=i.id WHERE d.worker_id=? AND d.delivered=0 ORDER BY i.created_at", (worker["id"],)).fetchall()
        if not rows:
            return
        if self.store.has_unresolved(worker["session_id"]):
            return
        self.worker_guard(worker["session_id"])
        run = self.store.get_run(worker["run_id"]) if worker["run_id"] else None
        if run and run["status"] not in TERMINAL:
            for row in rows:
                self.coordinator.inject(worker["session_id"], row["text"], request_id=f"task-input:{row['id']}:{worker['id']}", _task_input=True)
                with self.store.transaction() as db:
                    db.execute("UPDATE local_task_input_deliveries SET delivered=1 WHERE input_id=? AND worker_id=?", (row["id"], worker["id"]))
            return
        if worker["attempt"] >= self._limits()["max_attempts_per_worker"]:
            raise StateError("Configured worker attempt limit reached")
        self._remaining_snapshot(worker)
        with self.store.transaction() as db:
            db.execute("UPDATE local_task_workers SET state='queued',run_id=NULL,prompt=? WHERE id=?",
                       ("Follow-up inputs:\n" + "\n\n".join(row["text"] for row in rows), worker["id"]))
            for row in rows:
                db.execute("UPDATE local_task_input_deliveries SET delivered=1 WHERE input_id=? AND worker_id=?", (row["id"], worker["id"]))

    def _save_projection(self, ticket_id):
        view = self.get_ticket(ticket_id)
        with self.store.transaction() as db:
            for worker in view["workers"]:
                db.execute("UPDATE local_task_workers SET answer=?,error=? WHERE id=?", (worker["answer"], worker["error"], worker["id"]))
            prior = db.execute("SELECT state,summary,attention FROM local_task_tickets WHERE id=?", (ticket_id,)).fetchone()
            if (prior["state"], prior["summary"], prior["attention"]) != (view["state"], view["summary"], view["attention"]):
                db.execute("UPDATE local_task_tickets SET state=?,summary=?,attention=?,revision=revision+1,updated_at=? WHERE id=?",
                           (view["state"], view["summary"], view["attention"], time.time(), ticket_id))
                self.store._event(db, view["source_session_id"], view["parent_run_id"], "task.updated",
                                  {"ticket_id": ticket_id, "state": view["state"]})

    def _attach_existing(self, worker):
        """Recover delivery acknowledgement before doing any new admission work."""
        if worker["run_id"]:
            return worker
        request = f"local-task:{worker['id']}:{worker['attempt']}"
        run = next((old for old in self.store.list_runs(worker["session_id"]) if old["request_id"] == request), None)
        if run is None:
            return worker
        state = ("needs_reconciliation" if run["status"] in {"running", "cancelling", "waiting_approval", "waiting_input"}
                 and run["id"] not in self.coordinator._engines else "working")
        with self.store.transaction() as db:
            db.execute("UPDATE local_task_workers SET run_id=?,state=?,attempt=attempt+1,updated_at=? WHERE id=? AND run_id IS NULL",
                       (run["id"], state, time.time(), worker["id"]))
        return {**worker, "run_id": run["id"], "state": state, "attempt": worker["attempt"] + 1}

    async def tick(self):
        async with self._tick_lock:
            with self.store._lock:
                tickets = [dict(row) for row in self.store.connection.execute("SELECT * FROM local_task_tickets t "
                    "WHERE state NOT IN ('completed','failed','cancelled') OR EXISTS(SELECT 1 FROM local_task_inputs i "
                    "JOIN local_task_input_deliveries d ON d.input_id=i.id WHERE i.ticket_id=t.id AND d.delivered=0)").fetchall()]
            for ticket in tickets:
                if ticket["parent_run_id"] and self.store.get_run(ticket["parent_run_id"])["status"] == "cancelled" and not ticket["cancel_requested"]:
                    await self.cancel_ticket(ticket["id"])
                for worker in self._workers(ticket["id"]):
                    worker = self._attach_existing(worker)
                    if not ticket["cancel_requested"]:
                        try:
                            self._deliver_inputs(worker)
                        except (ValueError, StateError, OSError, KeyError):
                            continue
                    worker = next(row for row in self._workers(ticket["id"]) if row["id"] == worker["id"])
                    if worker["run_id"] or worker["state"] != "queued" or ticket["cancel_requested"] or ticket["state"] == "needs_reconciliation":
                        continue
                    try:
                        # During a tool-mediated delegation, its dispatch intent
                        # exists until the tool result commits. Wait, don't fail.
                        if self.store.has_unresolved(ticket["source_session_id"]):
                            if self._source_unknown(ticket["source_session_id"]):
                                with self.store.transaction() as db:
                                    db.execute("UPDATE local_task_tickets SET state='needs_reconciliation' WHERE id=?", (ticket["id"],))
                            continue
                        self.worker_guard(worker["session_id"])
                        if ticket["origin"] == "inferred":
                            policy = self.settings()
                            enabled = {rule["id"] for rule in policy["rules"] if rule["enabled"]}
                            if policy["mode"] != "auto" or policy["revision"] != ticket["policy_revision"] or not set(json.loads(ticket["matched_rules_json"])) <= enabled:
                                raise StateError("Proactive task authorization changed before launch")
                        request = f"local-task:{worker['id']}:{worker['attempt']}"
                        run = next((old for old in self.store.list_runs(worker["session_id"]) if old["request_id"] == request), None)
                        if run is None:
                            path = Path(worker["cwd"])
                            pin = self._remaining_snapshot(worker)
                            roots = [Path(root).resolve() for root in pin["snapshot"]["allowed_roots"]]
                            if not any(path.resolve().is_relative_to(root) for root in roots):
                                raise StateError("Worker directory escaped its captured workspace")
                            path.mkdir(parents=True, exist_ok=True)
                            if not any(path.resolve().is_relative_to(root) for root in roots):
                                raise StateError("Worker directory changed during preparation")
                            run = await self.coordinator.submit(worker["session_id"], worker["prompt"], request_id=request,
                                                                unattended=bool(ticket["unattended"]), execution_snapshot=pin)
                        with self.store.transaction() as db:
                            state = ("needs_reconciliation" if run["status"] in {"running", "cancelling", "waiting_approval", "waiting_input"}
                                     and run["id"] not in self.coordinator._engines else "working")
                            db.execute("UPDATE local_task_workers SET run_id=?,state=?,attempt=attempt+1,updated_at=? WHERE id=? AND run_id IS NULL",
                                       (run["id"], state, time.time(), worker["id"]))
                    except Exception as error:  # noqa: BLE001 - failed admission must settle visibly, not loop on a plugin error.
                        # Bounded categories avoid leaking provider/key material
                        # from arbitrary plugin exception text into task views.
                        with self.store.transaction() as db:
                            db.execute("UPDATE local_task_workers SET state='failed',error=?,updated_at=? WHERE id=?",
                                       (f"Task admission failed ({type(error).__name__}); inspect workspace, policy and provider settings.", time.time(), worker["id"]))
                self._save_projection(ticket["id"])

    async def _loop(self):
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as error:  # noqa: BLE001 - one task cannot stop the daemon's scheduler.
                with self.store.transaction() as db:
                    self.store._event(db, "", None, "task.scheduler_failed", {"error_type": type(error).__name__})
            try:
                await asyncio.wait_for(self._stop.wait(), self.poll_seconds)
            except TimeoutError:
                pass

    async def start(self):
        if self._runner and not self._runner.done():
            return
        self._stop.clear()
        # Active runs that survived a previous daemon have no live owner. Keep
        # their IDs and evidence; existing Runtime recovery is the only resume.
        with self.store.transaction() as db:
            db.execute("UPDATE local_task_workers SET state='needs_reconciliation' WHERE run_id IN "
                       "(SELECT id FROM runs WHERE status IN ('running','waiting_approval','waiting_input','cancelling'))")
        self._runner = asyncio.create_task(self._loop(), name="aster-local-tasks")

    async def close(self):
        self._stop.set()
        if self._runner and self._runner is not asyncio.current_task():
            await self._runner
        self._runner = None

    def check_lifecycle(self, session_ids, action):
        if action == "unarchive":
            return
        ids = set(session_ids)
        for ticket in self.list_tickets():
            related = {ticket["source_session_id"], *(worker["session_id"] for worker in ticket["workers"])}
            if ids & related:
                if ticket["state"] in {"queued", "working", "waiting", "needs_reconciliation"}:
                    raise SessionBusy("Finish, cancel or reconcile bound local tasks before changing conversation visibility")
                existing = {row[0] for row in self.store.connection.execute("SELECT id FROM sessions WHERE id IN (" +
                            ",".join("?" for _ in related) + ")", tuple(related)).fetchall()}
                if any(self.store.has_unresolved(sid) for sid in existing):
                    raise SessionBusy("Reconcile bound local task effects before deleting conversations")

    def cleanup_session(self, session_id, db=None):
        if db is None:
            with self.store.transaction() as connection:
                self.cleanup_session(session_id, connection)
            return
        ids = [row[0] for row in db.execute("SELECT id FROM local_task_tickets WHERE source_session_id=?", (session_id,)).fetchall()]
        for ticket_id in set(ids):
            # Surviving worker conversations retain a permanent binding tombstone.
            # Losing a task row must never turn a restricted reader into an
            # ordinary conversation with fresh global grants.
            db.execute("UPDATE local_task_bindings SET retired=1 WHERE session_id IN "
                       "(SELECT session_id FROM local_task_workers WHERE ticket_id=?)", (ticket_id,))
            db.execute("DELETE FROM local_task_input_deliveries WHERE worker_id IN (SELECT id FROM local_task_workers WHERE ticket_id=?)", (ticket_id,))
            db.execute("DELETE FROM local_task_workers WHERE ticket_id=?", (ticket_id,))
            db.execute("DELETE FROM local_task_inputs WHERE ticket_id=?", (ticket_id,))
            db.execute("DELETE FROM local_task_candidates WHERE ticket_id=?", (ticket_id,))
            db.execute("DELETE FROM local_task_tickets WHERE id=?", (ticket_id,))
        db.execute("UPDATE local_task_workers SET state='cancelled',run_id=NULL,error='工作对话已删除。' WHERE session_id=?", (session_id,))
        db.execute("DELETE FROM local_task_bindings WHERE session_id=?", (session_id,))
        db.execute("DELETE FROM local_task_candidates WHERE source_session_id=?", (session_id,))
