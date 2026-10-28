"""Transactional execution state and a durable event outbox.

Methods are deliberately synchronous and short: never hold a transaction across
an await, provider request, subprocess, or human decision.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from aster.domain import Message, ToolCall, ToolResult, message_from_dict, to_dict, tool_result_message
from aster.state.compaction import CompactionPersistence, initialize_compaction
from aster.state.extensions import SessionExtensions, initialize_extensions
from aster.state.lifecycle import SessionLifecycle, initialize_lifecycle

SCHEMA_VERSION = 2
ACTIVE = ("running", "waiting_approval", "waiting_input", "cancelling")
TERMINAL = ("completed", "cancelled", "failed", "timed_out", "budget_exhausted", "needs_reconciliation")


def identifier(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def dumps(value) -> str:
    return json.dumps(to_dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class StateError(RuntimeError):
    pass


class SessionBusy(StateError):
    pass


class ReconciliationRequired(StateError):
    pass


class BudgetExceeded(StateError):
    pass


class DeadlineExceeded(BudgetExceeded):
    pass


class Store(SessionExtensions, SessionLifecycle, CompactionPersistence):
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    @contextlib.contextmanager
    def transaction(self):
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except BaseException:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def _initialize(self):
        with self._lock:
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise StateError(f"Database schema {version} is newer than supported {SCHEMA_VERSION}")
            self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS sessions(
              id TEXT PRIMARY KEY, cwd TEXT NOT NULL, agent_id TEXT NOT NULL, project_id TEXT,
              title TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS runs(
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
              status TEXT NOT NULL, system TEXT NOT NULL, config_json TEXT NOT NULL,
              request_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
              answer TEXT NOT NULL DEFAULT '', error TEXT, resume_attempts INTEGER NOT NULL DEFAULT 0,
              UNIQUE(session_id,request_id));
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs(session_id)
              WHERE status IN ('running','waiting_approval','waiting_input','cancelling');
            CREATE TABLE IF NOT EXISTS messages(
              seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
              session_id TEXT NOT NULL REFERENCES sessions(id), run_id TEXT REFERENCES runs(id),
              role TEXT NOT NULL, content TEXT NOT NULL, tool_call_id TEXT,
              message_json TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS one_tool_result ON messages(run_id,tool_call_id)
              WHERE role='tool' AND tool_call_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS message_session_seq ON messages(session_id,seq);
            CREATE TABLE IF NOT EXISTS events(
              event_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
              run_id TEXT, type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS event_run_seq ON events(run_id,event_id);
            CREATE TABLE IF NOT EXISTS tool_executions(
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES runs(id),
              tool_call_id TEXT NOT NULL, tool_name TEXT NOT NULL, arguments_json TEXT NOT NULL,
              arguments_hash TEXT NOT NULL, risk TEXT NOT NULL, replay_safe INTEGER NOT NULL,
              state TEXT NOT NULL, result_json TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
              UNIQUE(run_id,tool_call_id));
            CREATE TABLE IF NOT EXISTS checkpoints(
              session_id TEXT PRIMARY KEY REFERENCES sessions(id), run_id TEXT NOT NULL,
              archive_through INTEGER NOT NULL, messages_json TEXT NOT NULL,
              metadata_json TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS pending_requests(
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL, run_id TEXT NOT NULL,
              kind TEXT NOT NULL, payload_json TEXT NOT NULL, state TEXT NOT NULL,
              decision_json TEXT, created_at REAL NOT NULL, expires_at REAL NOT NULL,
              updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS mailbox(
              seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
              session_id TEXT NOT NULL REFERENCES sessions(id), text TEXT NOT NULL,
              request_id TEXT, consumed_run_id TEXT, created_at REAL NOT NULL,
              UNIQUE(session_id,request_id));
            CREATE TABLE IF NOT EXISTS usage(
              id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
              usage_json TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS run_requests(
              run_id TEXT PRIMARY KEY REFERENCES runs(id), payload_json TEXT NOT NULL,
              created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS run_budgets(
              run_id TEXT PRIMARY KEY REFERENCES runs(id), iterations INTEGER NOT NULL DEFAULT 0,
              model_requests INTEGER NOT NULL DEFAULT 0, tool_calls INTEGER NOT NULL DEFAULT 0,
              started_at REAL NOT NULL, deadline_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS plans(
              session_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
              snapshot_json TEXT NOT NULL, updated_at REAL NOT NULL);
            """)
            try:
                self.connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(content, tokenize='trigram')")
                self.fts_available = True
            except sqlite3.OperationalError:
                self.fts_available = False
            initialize_extensions(self.connection)
            initialize_lifecycle(self.connection)
            initialize_compaction(self.connection)
            self.connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.purge_legacy_deleted_sessions()
            self.sweep_compaction_records()

    def _event(self, db, session_id: str, run_id: str | None, kind: str, payload: dict) -> dict:
        now = time.time()
        cursor = db.execute("INSERT INTO events(session_id,run_id,type,payload_json,created_at) VALUES(?,?,?,?,?)",
                            (session_id, run_id, kind, dumps(payload), now))
        return {"schema_version": 1, "event_id": cursor.lastrowid, "session_id": session_id,
                "run_id": run_id, "type": kind, "payload": to_dict(payload), "created_at": now}

    def emit_event(self, session_id: str, run_id: str | None, kind: str, payload: dict) -> dict:
        with self.transaction() as db:
            return self._event(db, session_id, run_id, kind, payload)

    def events(self, *, run_id: str | None = None, session_id: str | None = None,
               after: int = 0, limit: int = 1000) -> list[dict]:
        filters, args = ["event_id> ?"], [after]
        if run_id:
            filters.append("run_id=?")
            args.append(run_id)
        if session_id:
            filters.append("session_id=?")
            args.append(session_id)
        with self._lock:
            rows = self.connection.execute("SELECT * FROM events WHERE " + " AND ".join(filters) +
                                           " ORDER BY event_id LIMIT ?", (*args, min(max(limit, 1), 10000))).fetchall()
        return [{**dict(row), "schema_version": 1, "payload": json.loads(row["payload_json"])} for row in rows]

    def create_session(self, cwd: str | Path, *, agent_id: str = "default", project_id: str | None = None,
                       title: str = "", session_id: str | None = None) -> dict:
        cwd = Path(cwd).resolve()
        if not cwd.is_dir():
            raise ValueError(f"Session working directory does not exist: {cwd}")
        sid, now = session_id or identifier("ses"), time.time()
        with self.transaction() as db:
            self._require_project_available(db, project_id)
            db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?)", (sid, str(cwd), agent_id, project_id, title, now, now))
            self._event(db, sid, None, "session.created", {"cwd": str(cwd), "agent_id": agent_id})
        return self.get_session(sid)

    def get_session(self, session_id: str) -> dict:
        with self._lock:
            row = self.connection.execute("SELECT s.*,l.archived_at,l.deleted_at FROM sessions s LEFT JOIN "
                                          "session_lifecycle l ON l.session_id=s.id WHERE s.id=?", (session_id,)).fetchone()
        if not row:
            raise KeyError(session_id)
        return dict(row)

    def update_session_title(self, session_id: str, title: str) -> dict:
        if not isinstance(title, str):
            raise TypeError("Session title must be text")
        if len(title) > 200:
            raise ValueError("Session title cannot exceed 200 characters")
        with self.transaction() as db:
            self._require_session_writable(db, session_id)
            changed = db.execute("UPDATE sessions SET title=?,updated_at=? WHERE id=?",
                                 (title.strip(), time.time(), session_id))
            if changed.rowcount != 1:
                raise KeyError(session_id)
            self._event(db, session_id, None, "session.updated", {"title": title.strip()})
        return self.get_session(session_id)

    def list_sessions(self, *, limit: int = 100, offset: int = 0, include_side_chats: bool = False, state: str = "active") -> list[dict]:
        states = {"active": "l.archived_at IS NULL AND l.deleted_at IS NULL",
                  "archived": "l.archived_at IS NOT NULL AND l.deleted_at IS NULL",
                  "deleted": "0", "all": "l.deleted_at IS NULL"}
        if state not in states:
            raise ValueError("Session state must be active, archived, deleted or all")
        if offset < 0 or limit < 1:
            raise ValueError("Session page limit must be positive and offset must not be negative")
        with self._lock:
            return [dict(row) for row in self.connection.execute(
                "SELECT s.*,l.archived_at,l.deleted_at FROM sessions s LEFT JOIN session_lifecycle l ON l.session_id=s.id "
                "WHERE (? OR s.id NOT IN (SELECT child_id FROM session_lineage WHERE kind='side_chat')) AND " + states[state] +
                " ORDER BY s.updated_at DESC,s.id DESC LIMIT ? OFFSET ?", (include_side_chats, limit, offset)).fetchall()]

    def get_run(self, run_id: str) -> dict:
        with self._lock:
            row = self.connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        item = dict(row)
        item["config"] = json.loads(item.pop("config_json"))
        return item

    def list_runs(self, session_id: str | None = None, *, statuses: tuple[str, ...] | None = None) -> list[dict]:
        where, args = [], []
        if session_id:
            where.append("session_id=?")
            args.append(session_id)
        if statuses:
            where.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            args.extend(statuses)
        with self._lock:
            rows = self.connection.execute("SELECT id FROM runs" + (" WHERE " + " AND ".join(where) if where else "") +
                                           " ORDER BY created_at DESC", args).fetchall()
        return [self.get_run(row[0]) for row in rows]

    def create_run(self, session_id: str, *, system: str = "", config: dict | None = None,
                   request_id: str | None = None, initial_status: str = "running",
                   request_payload: dict | None = None) -> tuple[dict, bool]:
        if initial_status not in ("running", "queued"):
            raise ValueError("New run must be running or queued")
        self.get_session(session_id)
        lineage = self.lineage(session_id)
        if lineage and lineage["closed_at"] is not None:
            raise StateError("Side chat is closed")
        if any(self.has_unresolved(parent) for parent in self.session_ancestors(session_id)):
            raise ReconciliationRequired("A source session has unresolved tool effects")
        rid, now = identifier("run"), time.time()
        with self.transaction() as db:
            if request_id:
                existing = db.execute("SELECT id FROM runs WHERE session_id=? AND request_id=?", (session_id, request_id)).fetchone()
                if existing:
                    return self.get_run(existing[0]), False
            self._require_session_writable(db, session_id)
            if db.execute("SELECT 1 FROM tool_executions WHERE session_id=? AND "
                          "(state='outcome_unknown' OR (state='dispatching' AND risk<>'read' AND ?<>'queued')) LIMIT 1",
                          (session_id, initial_status)).fetchone():
                raise ReconciliationRequired("This session has unresolved tool outcomes")
            try:
                db.execute("INSERT INTO runs(id,session_id,status,system,config_json,request_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                           (rid, session_id, initial_status, system, dumps(config or {}), request_id, now, now))
            except sqlite3.IntegrityError as error:
                raise SessionBusy("This session already has an active run") from error
            if request_payload is not None:
                db.execute("INSERT INTO run_requests VALUES(?,?,?)", (rid, dumps(request_payload), now))
            self._event(db, session_id, rid, "run.created", {"status": initial_status})
        return self.get_run(rid), True

    def run_request(self, run_id: str) -> dict:
        with self._lock:
            row = self.connection.execute("SELECT payload_json FROM run_requests WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return json.loads(row[0])

    def set_run_status(self, run_id: str, status: str, *, answer: str = "", error: str | None = None) -> dict:
        if status not in ACTIVE + TERMINAL:
            raise ValueError(status)
        if status in TERMINAL:
            return self.finish_run(run_id, status, answer=answer, error=error)
        with self.transaction() as db:
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise KeyError(run_id)
            self._require_session_writable(db, run["session_id"])
            if run["status"] in TERMINAL and run["status"] != status:
                raise StateError("Terminal run transitions require explicit recovery")
            db.execute("UPDATE runs SET status=?,answer=?,error=?,updated_at=? WHERE id=?",
                       (status, answer, error, time.time(), run_id))
            return self._event(db, run["session_id"], run_id, "run.status", {"status": status, "error": error})

    def _finish_run(self, db, run_id, status, *, answer="", error=None, payload=None):
        run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            raise KeyError(run_id)
        if run["status"] in TERMINAL and run["status"] != status:
            raise StateError("Terminal run transitions require explicit recovery")
        sid = run["session_id"]
        db.execute("UPDATE runs SET status=?,answer=?,error=?,updated_at=? WHERE id=?",
                   (status, answer, error, time.time(), run_id))
        self._event(db, sid, run_id, "run.status", {"status": status, "error": error})
        plan = db.execute("SELECT * FROM plans WHERE session_id=?", (sid,)).fetchone()
        if plan:
            snapshot = json.loads(plan["snapshot_json"])
            # A queued cancellation must not close another run's live plan.
            if snapshot.get("run_id") in (None, run_id):
                snapshot.update(run_status=status, lifecycle="completed" if status == "completed" else "incomplete",
                                close_reason=status)
                revision = plan["revision"] + 1
                db.execute("UPDATE plans SET revision=?,snapshot_json=?,updated_at=? WHERE session_id=?",
                           (revision, dumps(snapshot), time.time(), sid))
                self._event(db, sid, run_id, "plan.updated", {"revision": revision, "snapshot": snapshot})
        final = payload or {"session_id": sid, "run_id": run_id, "status": status, "text": answer, "error": error}
        return self._event(db, sid, run_id, "run.finished", final)

    def finish_run(self, run_id: str, status: str, *, answer: str = "", error: str | None = None,
                   payload: dict | None = None) -> dict:
        """Commit the terminal state, plan closure, and stream completion fence together."""
        if status not in TERMINAL:
            raise ValueError(status)
        with self.transaction() as db:
            return self._finish_run(db, run_id, status, answer=answer, error=error, payload=payload)

    def completion_cursor(self, run_id: str) -> int | None:
        """A previous recovery attempt's completion cannot end the active stream."""
        with self._lock:
            row = self.connection.execute(
                "SELECT MAX(e.event_id) FROM events e JOIN runs r ON r.id=e.run_id "
                "WHERE e.run_id=? AND e.type='run.finished' AND r.status IN (" +
                ",".join("?" for _ in TERMINAL) + ") AND e.event_id>COALESCE((SELECT MAX(event_id) "
                "FROM events WHERE run_id=? AND type='run.recovered'),0)",
                (run_id, *TERMINAL, run_id)).fetchone()
        return row[0]

    def _append(self, db, session_id: str, run_id: str | None, message: Message) -> int:
        self._require_session_writable(db, session_id)
        if message.role == "tool" and message.tool_call_id and run_id:
            row = db.execute("SELECT seq FROM messages WHERE run_id=? AND role='tool' AND tool_call_id=?",
                             (run_id, message.tool_call_id)).fetchone()
            if row:
                return row[0]
        mid, now = identifier("msg"), time.time()
        cursor = db.execute("INSERT INTO messages(id,session_id,run_id,role,content,tool_call_id,message_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                            (mid, session_id, run_id, message.role, message.content, message.tool_call_id, dumps(message), now))
        seq = cursor.lastrowid
        if self.fts_available:
            db.execute("INSERT INTO messages_fts(rowid,content) VALUES(?,?)", (seq, message.content))
        db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
        self._event(db, session_id, run_id, "message.committed", {"message_id": mid, "seq": seq, "role": message.role,
                                                                 "tool_call_id": message.tool_call_id})
        return seq

    def append_message(self, session_id: str, run_id: str | None, message: Message) -> int:
        with self.transaction() as db:
            return self._append(db, session_id, run_id, message)

    def history(self, session_id: str, *, after: int = 0) -> list[Message]:
        with self._lock:
            rows = self.connection.execute("SELECT message_json FROM messages WHERE session_id=? AND seq>? ORDER BY seq",
                                           (session_id, after)).fetchall()
        return [message_from_dict(json.loads(row[0])) for row in rows]

    def history_records(self, session_id: str, *, limit: int = 1000) -> list[dict]:
        with self._lock:
            rows = self.connection.execute("SELECT * FROM messages WHERE session_id=? ORDER BY seq LIMIT ?",
                                           (session_id, limit)).fetchall()
        return [{**dict(row), "message": json.loads(row["message_json"])} for row in rows]

    def tool_call_ids(self, run_id: str) -> set[str]:
        with self._lock:
            rows = self.connection.execute("SELECT message_json FROM messages WHERE run_id=? AND role='assistant'", (run_id,)).fetchall()
        return {call["id"] for row in rows for call in json.loads(row[0]).get("tool_calls", [])}

    def active_history(self, session_id: str) -> list[Message]:
        with self._lock:
            checkpoint = self.connection.execute("SELECT * FROM checkpoints WHERE session_id=?", (session_id,)).fetchone()
        if not checkpoint:
            messages = self.history(session_id)
        else:
            saved = [message_from_dict(item) for item in json.loads(checkpoint["messages_json"])]
            messages = saved + self.history(session_id, after=checkpoint["archive_through"])
        with self._lock:
            rows = self.connection.execute("SELECT run_id,tool_call_id,result_json FROM tool_executions WHERE session_id=? "
                                           "AND state IN ('committed','checkpointed') AND result_json IS NOT NULL", (session_id,)).fetchall()
        known = {(row["run_id"], row["tool_call_id"]): json.loads(row["result_json"]) for row in rows}
        for message in messages:
            if message.role == "tool" and message.metadata.get("outcome") == "unknown":
                result = known.get((message.metadata.get("run_id"), message.tool_call_id))
                if result and result["outcome"] != "unknown":
                    corrected = tool_result_message(message.tool_call_id, ToolResult(**result), message.metadata["run_id"])
                    message.content = corrected.content
                    message.metadata = {**corrected.metadata, "reconciled": True}
        return messages

    def checkpoint(self, session_id: str, run_id: str, messages: list[Message], *, metadata: dict | None = None) -> dict:
        with self.transaction() as db:
            self._require_session_writable(db, session_id)
            through = db.execute("SELECT COALESCE(MAX(seq),0) FROM messages WHERE session_id=?", (session_id,)).fetchone()[0]
            db.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET run_id=excluded.run_id,"
                       "archive_through=excluded.archive_through,messages_json=excluded.messages_json,"
                       "metadata_json=excluded.metadata_json,created_at=excluded.created_at",
                       (session_id, run_id, through, dumps(messages), dumps(metadata or {}), time.time()))
            db.execute("UPDATE tool_executions SET state='checkpointed',updated_at=? WHERE run_id=? AND state='committed' "
                       "AND EXISTS(SELECT 1 FROM messages m WHERE m.run_id=tool_executions.run_id AND m.tool_call_id=tool_executions.tool_call_id AND m.role='tool')",
                       (time.time(), run_id))
            return self._event(db, session_id, run_id, "checkpoint.saved", {"archive_through": through})

    def get_checkpoint(self, session_id: str) -> dict | None:
        with self._lock:
            row = self.connection.execute("SELECT * FROM checkpoints WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "messages": json.loads(row["messages_json"]), "metadata": json.loads(row["metadata_json"])}

    def prepare_tool(self, session_id: str, run_id: str, call: ToolCall, *, risk: str, replay_safe: bool) -> dict:
        eid, now, arguments = identifier("exe"), time.time(), dumps(call.arguments)
        with self.transaction() as db:
            self._require_session_writable(db, session_id)
            existing = db.execute("SELECT * FROM tool_executions WHERE run_id=? AND tool_call_id=?", (run_id, call.id)).fetchone()
            if existing:
                raise StateError(f"Repeated tool call ID: {call.id}")
            db.execute("INSERT INTO tool_executions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (eid, session_id, run_id, call.id, call.name, arguments, hashlib.sha256(arguments.encode()).hexdigest(),
                        risk, int(replay_safe), "prepared", None, now, now))
        return self.tool_execution(eid)

    def tool_execution(self, execution_id: str) -> dict:
        with self._lock:
            row = self.connection.execute("SELECT * FROM tool_executions WHERE id=?", (execution_id,)).fetchone()
        if not row:
            raise KeyError(execution_id)
        item = dict(row)
        item["arguments"] = json.loads(item["arguments_json"])
        item["result"] = json.loads(item["result_json"]) if item["result_json"] else None
        return item

    def tool_executions(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self.connection.execute("SELECT id FROM tool_executions WHERE run_id=? ORDER BY created_at,id", (run_id,)).fetchall()
        return [self.tool_execution(row[0]) for row in rows]

    def has_unresolved(self, session_id: str) -> bool:
        sessions = [session_id, *self.session_ancestors(session_id)]
        with self._lock:
            return self.connection.execute("SELECT 1 FROM tool_executions WHERE session_id IN (" + ",".join("?" for _ in sessions) + ") AND "
                                           "(state='outcome_unknown' OR (state='dispatching' AND risk<>'read')) LIMIT 1",
                                           sessions).fetchone() is not None

    def mark_dispatching(self, execution_id: str):
        with self.transaction() as db:
            execution = db.execute("SELECT session_id FROM tool_executions WHERE id=?", (execution_id,)).fetchone()
            if execution:
                self._require_session_writable(db, execution[0])
            cursor = db.execute("UPDATE tool_executions SET state='dispatching',updated_at=? WHERE id=? AND state='prepared'",
                                (time.time(), execution_id))
            if cursor.rowcount != 1:
                raise StateError("Only prepared executions can dispatch")

    def commit_tool(self, execution_id: str, result: ToolResult):
        state = "outcome_unknown" if result.outcome == "unknown" else (
            "failed_no_effect" if result.outcome == "not_executed" else "committed")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM tool_executions WHERE id=?", (execution_id,)).fetchone()
            if not row or row["state"] not in ("prepared", "dispatching"):
                raise StateError("Execution is already terminal or missing")
            db.execute("UPDATE tool_executions SET state=?,result_json=?,updated_at=? WHERE id=?",
                       (state, dumps(result), time.time(), execution_id))
            self._event(db, row["session_id"], row["run_id"], "tool.committed",
                        {"tool_call_id": row["tool_call_id"], "state": state, "is_error": result.is_error})

    def reconcile_tool(self, execution_id: str, result: ToolResult, *, evidence: str):
        if not evidence.strip() or result.outcome == "unknown":
            raise ValueError("Reconciliation requires concrete evidence and a determined outcome")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM tool_executions WHERE id=?", (execution_id,)).fetchone()
            if not row or row["state"] != "outcome_unknown":
                raise StateError("Only unknown executions may be reconciled")
            db.execute("UPDATE tool_executions SET state='committed',result_json=?,updated_at=? WHERE id=?",
                       (dumps(result), time.time(), execution_id))
            # Keep the original unknown result in the archive. Recovery projects the
            # determined result into model context; the evidence remains auditable.
            db.execute("DELETE FROM checkpoints WHERE session_id=?", (row["session_id"],))
            self._event(db, row["session_id"], row["run_id"], "tool.reconciled",
                        {"execution_id": execution_id, "evidence": evidence, "result": to_dict(result)})

    def queue_message(self, session_id: str, text: str, *, request_id: str | None = None,
                      message: Message | None = None, steering: bool = False) -> str:
        self.get_session(session_id)
        lineage = self.lineage(session_id)
        if lineage and lineage["closed_at"] is not None:
            raise StateError("Side chat is closed")
        if not text.strip() and not (message and message.metadata.get("artifacts")):
            raise ValueError("Message is empty")
        mid = identifier("inbox")
        with self.transaction() as db:
            if request_id:
                row = db.execute("SELECT id FROM mailbox WHERE session_id=? AND request_id=?", (session_id, request_id)).fetchone()
                if row:
                    return row[0]
            self._require_session_writable(db, session_id)
            db.execute("INSERT INTO mailbox(id,session_id,text,request_id,created_at) VALUES(?,?,?,?,?)",
                       (mid, session_id, text, request_id, time.time()))
            db.execute("INSERT INTO mailbox_lifecycle VALUES(?,?,?,?,?)",
                       (mid, "pending", dumps(message or Message("user", text)), int(steering), time.time()))
            self._event(db, session_id, None, "message.queued", {"message_id": mid, "steering": steering})
        return mid

    def drain_messages(self, session_id: str, run_id: str) -> list[Message]:
        with self.transaction() as db:
            rows = db.execute("SELECT m.*,l.message_json FROM mailbox m LEFT JOIN mailbox_lifecycle l ON l.message_id=m.id "
                              "WHERE m.session_id=? AND m.consumed_run_id IS NULL AND COALESCE(l.state,'pending')='pending' ORDER BY m.seq", (session_id,)).fetchall()
            messages = []
            for row in rows:
                message = message_from_dict(json.loads(row["message_json"])) if row["message_json"] else Message("user", row["text"])
                message.metadata["mailbox_id"] = row["id"]
                self._append(db, session_id, run_id, message)
                db.execute("UPDATE mailbox SET consumed_run_id=? WHERE id=?", (run_id, row["id"]))
                db.execute("UPDATE mailbox_lifecycle SET state='consumed',updated_at=? WHERE message_id=?", (time.time(), row["id"]))
                self._event(db, session_id, run_id, "message.consumed", {"message_id": row["id"]})
                messages.append(message)
            return messages

    def create_pending(self, session_id: str, run_id: str, kind: str, payload: dict, timeout: float) -> dict:
        pid, now = identifier("req"), time.time()
        with self.transaction() as db:
            self._require_session_writable(db, session_id)
            db.execute("INSERT INTO pending_requests VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (pid, session_id, run_id, kind, dumps(payload), "pending", None, now, now + timeout, now))
            event = self._event(db, session_id, run_id, f"{kind}.requested", {"request_id": pid, **payload, "expires_at": now + timeout})
        return {"id": pid, "event": event, "expires_at": now + timeout}

    def resolve_pending(self, request_id: str, decision, *, state: str = "resolved") -> bool:
        if state not in ("resolved", "cancelled", "expired"):
            raise ValueError(state)
        with self.transaction() as db:
            row = db.execute("SELECT * FROM pending_requests WHERE id=?", (request_id,)).fetchone()
            if not row or row["state"] != "pending":
                return False
            if state == "resolved" and row["expires_at"] < time.time():
                state, decision = "expired", None
            db.execute("UPDATE pending_requests SET state=?,decision_json=?,updated_at=? WHERE id=?",
                       (state, dumps(decision), time.time(), request_id))
            self._event(db, row["session_id"], row["run_id"], f"{row['kind']}.resolved",
                        {"request_id": request_id, "state": state, "decision": decision})
            return state == "resolved"

    def get_pending(self, request_id: str) -> dict:
        with self._lock:
            row = self.connection.execute("SELECT * FROM pending_requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            raise KeyError(request_id)
        return {**dict(row), "payload": json.loads(row["payload_json"]),
                "decision": json.loads(row["decision_json"]) if row["decision_json"] else None}

    def pending_requests(self, run_id: str | None = None) -> list[dict]:
        with self._lock:
            rows = self.connection.execute("SELECT id FROM pending_requests WHERE state='pending'" +
                                           (" AND run_id=?" if run_id else ""), (run_id,) if run_id else ()).fetchall()
        return [self.get_pending(row[0]) for row in rows]

    def add_usage(self, run_id: str, usage):
        with self.transaction() as db:
            db.execute("INSERT INTO usage(run_id,usage_json,created_at) VALUES(?,?,?)", (run_id, dumps(usage), time.time()))

    def start_budget(self, run_id: str, deadline_seconds: float) -> dict:
        now = time.time()
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO run_budgets(run_id,started_at,deadline_at) VALUES(?,?,?)", (run_id, now, now + deadline_seconds))
        return self.run_budget(run_id)

    def run_budget(self, run_id: str) -> dict | None:
        with self._lock:
            row = self.connection.execute("SELECT * FROM run_budgets WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def _reserve_budget(self, db, run_id, field, amount, limit):
        if field not in {"iterations", "model_requests", "tool_calls"}:
            raise ValueError("Unknown budget counter")
        row = db.execute("SELECT * FROM run_budgets WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise StateError("Run budget has not been initialized")
        if row["deadline_at"] <= time.time():
            raise DeadlineExceeded("The original run deadline has expired")
        if row[field] + amount > limit:
            raise BudgetExceeded(f"Cumulative {field} budget exhausted")
        db.execute(f"UPDATE run_budgets SET {field}={field}+? WHERE run_id=?", (amount, run_id))
        return row[field] + amount

    def reserve_iteration(self, run_id: str, limit: int) -> int:
        with self.transaction() as db:
            return self._reserve_budget(db, run_id, "iterations", 1, limit)

    def reserve_tool_calls(self, run_id: str, count: int, limit: int) -> int:
        with self.transaction() as db:
            return self._reserve_budget(db, run_id, "tool_calls", count, limit)

    def begin_model_request(self, run_id: str, *, limit: int, purpose: str) -> int:
        from aster.domain import Usage
        with self.transaction() as db:
            number = self._reserve_budget(db, run_id, "model_requests", 1, limit)
            unknown = Usage(raw={"purpose": purpose, "request_number": number, "status": "outcome_unknown"})
            cursor = db.execute("INSERT INTO usage(run_id,usage_json,created_at) VALUES(?,?,?)", (run_id, dumps(unknown), time.time()))
            return cursor.lastrowid

    def finish_model_request(self, usage_id: int, usage):
        with self.transaction() as db:
            row = db.execute("SELECT usage_json FROM usage WHERE id=?", (usage_id,)).fetchone()
            if row is None:
                raise StateError("Model request receipt is missing")
            original = json.loads(row[0])["raw"]
            value = to_dict(usage)
            value["raw"] = {**value.get("raw", {}), **original, "status": "response_received"}
            db.execute("UPDATE usage SET usage_json=? WHERE id=?", (dumps(value), usage_id))

    def usage(self, run_id: str) -> list[dict]:
        with self._lock:
            return [json.loads(row[0]) for row in self.connection.execute("SELECT usage_json FROM usage WHERE run_id=? ORDER BY id", (run_id,))]

    def set_plan(self, session_id: str, run_id: str, snapshot: dict, *, expected_revision: int | None = None) -> dict:
        with self.transaction() as db:
            self._require_session_writable(db, session_id)
            row = db.execute("SELECT revision FROM plans WHERE session_id=?", (session_id,)).fetchone()
            revision = row[0] if row else 0
            if expected_revision is not None and revision != expected_revision:
                raise StateError("Plan revision conflict")
            revision += 1
            db.execute("INSERT INTO plans VALUES(?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET revision=excluded.revision,"
                       "snapshot_json=excluded.snapshot_json,updated_at=excluded.updated_at", (session_id, revision, dumps(snapshot), time.time()))
            self._event(db, session_id, run_id, "plan.updated", {"revision": revision, "snapshot": snapshot})
        return {"revision": revision, "snapshot": snapshot}

    def get_plan(self, session_id: str) -> dict | None:
        with self._lock:
            row = self.connection.execute("SELECT * FROM plans WHERE session_id=?", (session_id,)).fetchone()
        return {"revision": row["revision"], "snapshot": json.loads(row["snapshot_json"])} if row else None

    def search_messages(self, query: str, *, session_id: str | None = None,
                        agent_id: str | None = None, project_id: str | None = None,
                        limit: int = 20, all_projects: bool = False) -> list[dict]:
        query = query.strip()
        if not query:
            return []
        clauses, args = [], []
        if session_id is not None:
            clauses.append("m.session_id=?")
            args.append(session_id)
        else:
            clauses.append("m.session_id NOT IN (SELECT child_id FROM session_lineage WHERE kind='side_chat')")
            clauses.append("NOT EXISTS(SELECT 1 FROM session_lifecycle l WHERE l.session_id=m.session_id "
                           "AND (l.archived_at IS NOT NULL OR l.deleted_at IS NOT NULL))")
        if agent_id is not None:
            clauses.append("s.agent_id=?")
            args.append(agent_id)
        if project_id is not None:
            clauses.append("s.project_id=?")
            args.append(project_id)
        elif not all_projects:
            clauses.append("s.project_id IS NULL")
        if self.fts_available and len(query) >= 3:
            clauses.append("m.seq IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)")
            args.append('"' + query.replace('"', '""') + '"')
        else:
            clauses.append("m.content LIKE ? ESCAPE '\\'")
            args.append("%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        with self._lock:
            rows = self.connection.execute("SELECT m.id AS message_id,m.session_id,m.role,m.content,m.created_at "
                                           "FROM messages m JOIN sessions s ON s.id=m.session_id WHERE " +
                                           " AND ".join(clauses) + " ORDER BY m.seq DESC LIMIT ?",
                                           (*args, min(max(limit, 1), 100))).fetchall()
        return [dict(row) for row in rows]

    def backup(self, destination: str | Path):
        destination = Path(destination).resolve()
        if destination == self.path:
            raise ValueError("Backup destination is the active database")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, sqlite3.connect(destination) as target:
            self.connection.backup(target)

    def recover_run(self, run_id: str, *, max_attempts: int = 3, max_age_seconds: float = 86400) -> dict:
        """Explicit manual recovery. Caller must own the runtime's service lease."""
        with self.transaction() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(run_id)
            if row["status"] == "completed":
                raise StateError("A completed run cannot be recovered")
            if row["resume_attempts"] >= max_attempts:
                raise StateError("Recovery attempt limit reached")
            if time.time() - row["updated_at"] > max_age_seconds:
                raise StateError("Run is older than the configured recovery window")
            sid = row["session_id"]
            self._require_session_writable(db, sid)
            other = db.execute("SELECT id FROM runs WHERE session_id=? AND id<>? AND status IN ('running','waiting_approval','waiting_input','cancelling')",
                               (sid, run_id)).fetchone()
            if other:
                raise SessionBusy("Another run owns this session")
            executions = {item["tool_call_id"]: dict(item) for item in db.execute("SELECT * FROM tool_executions WHERE run_id=?", (run_id,))}
            records = db.execute("SELECT message_json FROM messages WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
            calls = []
            for record in records:
                calls.extend(message_from_dict(json.loads(record[0])).tool_calls)
            unresolved = False
            for call in calls:
                item = executions.get(call.id)
                if item is None:
                    result = ToolResult("Interrupted before tool dispatch; this call was not executed", True, "interrupted", "not_executed")
                elif item["state"] == "prepared":
                    result = ToolResult("Interrupted before tool dispatch; safe to request again if still needed", True, "interrupted", "not_executed")
                    db.execute("UPDATE tool_executions SET state='failed_no_effect',result_json=?,updated_at=? WHERE id=?",
                               (dumps(result), time.time(), item["id"]))
                elif item["state"] == "dispatching":
                    safe = item["risk"] == "read" or item["replay_safe"]
                    result = ToolResult("Previous execution was interrupted. " +
                                        ("This tool is explicitly safe to retry if still needed." if safe else "Its external effect is unknown; verify it before continuing."),
                                        True, "interrupted", "known" if safe else "unknown")
                    db.execute("UPDATE tool_executions SET state=?,result_json=?,updated_at=? WHERE id=?",
                               ("committed" if safe else "outcome_unknown", dumps(result), time.time(), item["id"]))
                elif item["result_json"]:
                    result = ToolResult(**json.loads(item["result_json"]))
                else:
                    result = ToolResult("Execution result is missing; verification is required", True, "infrastructure", "unknown")
                    db.execute("UPDATE tool_executions SET state='outcome_unknown',result_json=?,updated_at=? WHERE id=?",
                               (dumps(result), time.time(), item["id"]))
                if result.outcome == "unknown":
                    unresolved = True
                self._append(db, sid, run_id, tool_result_message(call.id, result, run_id))
            model_call_ids = {call.id for call in calls}
            for call_id, item in executions.items():
                if call_id in model_call_ids:
                    continue
                # Hooks have their own durable execution entries, without fabricated
                # assistant tool calls. Their uncertainty still blocks the whole run.
                if item["state"] in ("dispatching", "prepared"):
                    unknown = item["state"] == "dispatching" and item["risk"] != "read" and not item["replay_safe"]
                    result = ToolResult("Independent execution interrupted; inspect its recorded effects", True,
                                        "interrupted", "unknown" if unknown else "not_executed")
                    state = "outcome_unknown" if unknown else "failed_no_effect"
                    db.execute("UPDATE tool_executions SET state=?,result_json=?,updated_at=? WHERE id=?",
                               (state, dumps(result), time.time(), item["id"]))
                    unresolved |= unknown
                elif item["state"] == "outcome_unknown":
                    unresolved = True
            # Repaired results are appended after the saved archive boundary;
            # active_history already combines them with the model-live prefix.
            # Unconditionally deleting that prefix loses its semantic summary,
            # calibrated overhead and same-run reactive/backoff control state.
            checkpoint = db.execute("SELECT * FROM checkpoints WHERE session_id=?", (sid,)).fetchone()
            if checkpoint:
                def paired(messages):
                    pending, seen = set(), set()
                    for message in messages:
                        canonical = [call.id for call in message.tool_calls]
                        wire = [item.get("call_id") for item in message.provider_items if item.get("type") == "function_call"]
                        ids = set(canonical) | set(wire)
                        if len(set(canonical)) != len(canonical) or len(set(wire)) != len(wire):
                            return False
                        if message.role == "tool":
                            if ids or message.tool_call_id not in pending:
                                return False
                            pending.remove(message.tool_call_id)
                        elif pending:
                            return False
                        if ids:
                            if message.role != "assistant" or not all(isinstance(value, str) and value for value in ids) or seen & ids:
                                return False
                            pending.update(ids)
                            seen.update(ids)
                    return not pending

                through = checkpoint["archive_through"]
                boundary_exists = through == 0 or db.execute("SELECT 1 FROM messages WHERE session_id=? AND seq=?",
                                                              (sid, through)).fetchone() is not None
                try:
                    valid = boundary_exists and isinstance(json.loads(checkpoint["metadata_json"]), dict) and paired(self.active_history(sid))
                except (TypeError, ValueError, KeyError):
                    valid = False
                if not valid:
                    # Keep valid control metadata even if an old/corrupt live
                    # projection needs reconstruction. Raw evidence is unchanged.
                    try:
                        metadata = json.loads(checkpoint["metadata_json"])
                    except (TypeError, ValueError):
                        metadata = {}
                    if not isinstance(metadata, dict):
                        metadata = {}
                    restored = self.history(sid)
                    through = db.execute("SELECT COALESCE(MAX(seq),0) FROM messages WHERE session_id=?", (sid,)).fetchone()[0]
                    db.execute("UPDATE checkpoints SET run_id=?,archive_through=?,messages_json=?,metadata_json=?,created_at=? "
                               "WHERE session_id=?", (run_id, through, dumps(restored), dumps(metadata), time.time(), sid))
                    self._event(db, sid, run_id, "recovery.checkpoint_rebuilt", {"reason": "invalid_boundary_or_tool_pairs",
                                                                             "archive_through": through})
            for pending in db.execute("SELECT * FROM pending_requests WHERE run_id=? AND state='pending'", (run_id,)).fetchall():
                db.execute("UPDATE pending_requests SET state='cancelled',decision_json='null',updated_at=? WHERE id=?", (time.time(), pending["id"]))
                self._event(db, sid, run_id, f"{pending['kind']}.resolved", {"request_id": pending["id"], "state": "cancelled"})
            status = "needs_reconciliation" if unresolved else "running"
            db.execute("UPDATE runs SET status=?,resume_attempts=resume_attempts+1,updated_at=?,error=NULL WHERE id=?", (status, time.time(), run_id))
            self._event(db, sid, run_id, "run.recovered", {"status": status, "resume_attempts": row["resume_attempts"] + 1})
            if unresolved:
                self._finish_run(db, run_id, status, error="A dispatched action needs verification")
        return self.get_run(run_id)
