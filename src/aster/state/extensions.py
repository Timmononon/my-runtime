"""Additive session lineage and mailbox lifecycle; original transcripts stay intact."""
from __future__ import annotations

import copy
import json
import time

from aster.domain import message_from_dict


def initialize_extensions(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS session_lineage(
      child_id TEXT PRIMARY KEY REFERENCES sessions(id), parent_id TEXT REFERENCES sessions(id),
      boundary_seq INTEGER NOT NULL, kind TEXT NOT NULL, created_at REAL NOT NULL, closed_at REAL);
    CREATE TABLE IF NOT EXISTS mailbox_lifecycle(
      message_id TEXT PRIMARY KEY REFERENCES mailbox(id), state TEXT NOT NULL DEFAULT 'pending',
      message_json TEXT NOT NULL, steering INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS automation_routes(
      route_key TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), created_at REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS automation_trigger_routes(
      trigger_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
      session_mode TEXT NOT NULL, created_at REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS file_observations(
      session_id TEXT NOT NULL, path TEXT NOT NULL, start_line INTEGER NOT NULL, max_lines INTEGER NOT NULL,
      sha256 TEXT NOT NULL, content TEXT NOT NULL, tool_call_id TEXT NOT NULL, observed_at REAL NOT NULL,
      PRIMARY KEY(session_id,path,start_line,max_lines));
    """)
    # A surviving branch keeps its kind/closed state when its parent is permanently
    # deleted. NULL records that detached root without inventing another session.
    if next(row for row in db.execute("PRAGMA table_info(session_lineage)") if row[1] == "parent_id")[3]:
        db.executescript("""BEGIN IMMEDIATE;
        CREATE TABLE session_lineage_detached(
          child_id TEXT PRIMARY KEY REFERENCES sessions(id), parent_id TEXT REFERENCES sessions(id),
          boundary_seq INTEGER NOT NULL, kind TEXT NOT NULL, created_at REAL NOT NULL, closed_at REAL);
        INSERT INTO session_lineage_detached SELECT * FROM session_lineage;
        DROP TABLE session_lineage;
        ALTER TABLE session_lineage_detached RENAME TO session_lineage;
        COMMIT;""")


class SessionExtensions:
    def session_ancestors(self, session_id):
        self.get_session(session_id)
        found, current = [], session_id
        with self._lock:
            while True:
                row = self.connection.execute("SELECT parent_id FROM session_lineage WHERE child_id=?", (current,)).fetchone()
                if row is None or row[0] is None:
                    return found
                current = row[0]
                if current in found or current == session_id:
                    raise ValueError("Session lineage cycle")
                found.append(current)

    def lineage(self, session_id):
        with self._lock:
            row = self.connection.execute("SELECT * FROM session_lineage WHERE child_id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def inherited_attachment_ids(self, session_id):
        return {item.get("attachment_id", item.get("artifact_id")) for message in self.history(session_id)
                if message.metadata.get("inherited_from_session") for item in message.metadata.get("artifacts", [])
                if item.get("attachment_id") or item.get("artifact_id")}

    def fork_session(self, session_id, *, message_index=None, agent_id=None, kind="fork", title=None):
        from aster.state.store import ReconciliationRequired, identifier
        if kind not in {"fork", "side_chat"}:
            raise ValueError("Invalid branch kind")
        source = self.get_session(session_id)
        if any(self.has_unresolved(sid) for sid in [session_id, *self.session_ancestors(session_id)]):
            raise ReconciliationRequired("Resolve uncertain source actions before branching")
        child_id, now = identifier("ses"), time.time()
        with self.transaction() as db:
            self._require_session_writable(db, session_id)
            self._require_project_available(db, source["project_id"])
            rows = db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY seq", (session_id,)).fetchall()
            boundary = len(rows) if message_index is None else message_index
            if not isinstance(boundary, int) or isinstance(boundary, bool) or not 0 <= boundary <= len(rows):
                raise ValueError("message_index is an exclusive raw-history message count")
            selected = rows[:boundary]
            pending = set()
            for row in selected:
                message = message_from_dict(json.loads(row["message_json"]))
                pending.update(call.id for call in message.tool_calls)
                if message.role == "tool":
                    pending.discard(message.tool_call_id)
            if selected:
                last = message_from_dict(json.loads(selected[-1]["message_json"]))
                if pending or last.role not in {"assistant", "tool"} or last.tool_calls:
                    raise ValueError("Branch boundary must end at a complete assistant turn")
            db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?)", (child_id, source["cwd"], agent_id or source["agent_id"],
                       source["project_id"], title if title is not None else source["title"], now, now))
            through = selected[-1]["seq"] if selected else 0
            db.execute("INSERT INTO session_lineage VALUES(?,?,?,?,?,NULL)", (child_id, session_id, through, kind, now))
            mapping = {}
            for row in selected:
                message = message_from_dict(json.loads(row["message_json"]))
                message.metadata = {**copy.deepcopy(message.metadata), "inherited_from_session": session_id,
                                    "source_message_id": row["id"], "source_seq": row["seq"]}
                mapping[row["seq"]] = self._append(db, child_id, None, message)
            checkpoint = db.execute("SELECT * FROM checkpoints WHERE session_id=?", (session_id,)).fetchone()
            # Reuse a checkpoint only when its archive marker was copied; a
            # later source checkpoint must not cross this fork boundary.
            if checkpoint and checkpoint["archive_through"] in mapping:
                db.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?,?)", (child_id, checkpoint["run_id"],
                           mapping[checkpoint["archive_through"]], checkpoint["messages_json"], checkpoint["metadata_json"], now))
            self._event(db, child_id, None, "session.branched", {"parent_id": session_id, "message_index": boundary,
                                                               "boundary_seq": through, "kind": kind})
        return {**self.get_session(child_id), "lineage": self.lineage(child_id)}

    def close_side_chat(self, session_id):
        from aster.state.store import ACTIVE, StateError
        with self.transaction() as db:
            self._require_session_writable(db, session_id)
            row = db.execute("SELECT * FROM session_lineage WHERE child_id=? AND kind='side_chat'", (session_id,)).fetchone()
            if not row:
                raise ValueError("Session is not a side chat")
            if db.execute("SELECT 1 FROM runs WHERE session_id=? AND status IN (" + ",".join("?" for _ in ACTIVE) + ")",
                          (session_id, *ACTIVE)).fetchone():
                raise StateError("Cancel active side-chat work before closing")
            if row["closed_at"] is None:
                for queued in db.execute("SELECT id FROM runs WHERE session_id=? AND status='queued'", (session_id,)).fetchall():
                    self._finish_run(db, queued["id"], "cancelled", error="Side chat closed before execution")
                pending = db.execute("SELECT m.id FROM mailbox m JOIN mailbox_lifecycle l ON l.message_id=m.id "
                                     "WHERE m.session_id=? AND l.state='pending'", (session_id,)).fetchall()
                for item in pending:
                    db.execute("UPDATE mailbox_lifecycle SET state='retracted',updated_at=? WHERE message_id=?", (time.time(), item["id"]))
                    self._event(db, session_id, None, "message.retracted", {"message_id": item["id"], "reason": "side_chat_closed"})
                db.execute("UPDATE session_lineage SET closed_at=? WHERE child_id=?", (time.time(), session_id))
                self._event(db, session_id, None, "session.closed", {"kind": "side_chat", "audit_retained": True})

    def list_mailbox(self, session_id, *, include_terminal=False):
        self.get_session(session_id)
        with self._lock:
            rows = self.connection.execute("SELECT m.*,l.state,l.steering,l.updated_at,l.message_json FROM mailbox m LEFT JOIN "
                "mailbox_lifecycle l ON l.message_id=m.id WHERE m.session_id=? ORDER BY m.seq", (session_id,)).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            message = json.loads(item.pop("message_json") or "{}")
            item["attachments"] = message.get("metadata", {}).get("attachment_ids", [])
            item["state"] = item["state"] or ("consumed" if item["consumed_run_id"] else "pending")
            if include_terminal or item["state"] == "pending":
                output.append(item)
        return output

    def retract_message(self, session_id, message_id):
        from aster.domain import Message
        from aster.state.store import dumps
        with self.transaction() as db:
            self._require_session_writable(db, session_id)
            row = db.execute("SELECT m.*,l.state FROM mailbox m LEFT JOIN mailbox_lifecycle l ON l.message_id=m.id "
                             "WHERE m.id=? AND m.session_id=?", (message_id, session_id)).fetchone()
            if row is None:
                raise KeyError(message_id)
            if row["consumed_run_id"] or row["state"] in {"consumed", "retracted"}:
                return False
            db.execute("INSERT INTO mailbox_lifecycle VALUES(?,?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET "
                       "state='retracted',updated_at=excluded.updated_at", (message_id, "retracted", dumps(Message("user", row["text"])), 0, time.time()))
            self._event(db, session_id, None, "message.retracted", {"message_id": message_id})
        return True

    def automation_session(self, source_id, route_key):
        """Stable route is persisted before submission, including per-trigger fresh routes."""
        from aster.state.store import identifier
        source = self.get_session(source_id)
        with self.transaction() as db:
            self._require_session_writable(db, source_id)
            existing = db.execute("SELECT session_id FROM automation_routes WHERE route_key=?", (route_key,)).fetchone()
            if existing:
                self._require_session_writable(db, existing[0])
                return existing[0]
            self._require_project_available(db, source["project_id"])
            sid, now = identifier("ses"), time.time()
            db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?)", (sid, source["cwd"], source["agent_id"], source["project_id"],
                       "Automation: " + source["title"], now, now))
            db.execute("INSERT INTO automation_routes VALUES(?,?,?)", (route_key, sid, now))
            self._event(db, sid, None, "automation.session_created", {"source_session_id": source_id, "route_key": route_key})
            return sid

    def automation_execution_session(self, source_id, job_id, trigger_id, mode):
        from aster.state.store import StateError
        self.require_session_writable(source_id)
        with self._lock:
            pinned = self.connection.execute("SELECT * FROM automation_trigger_routes WHERE trigger_id=?", (trigger_id,)).fetchone()
        if pinned:
            self.require_session_writable(pinned["session_id"])
            return dict(pinned)
        source = self.get_session(source_id)
        # Upgrade recovery: an old receipt might have committed a run before its
        # acknowledgement. Its globally random trigger identity is authoritative.
        with self._lock:
            accepted = self.connection.execute("SELECT r.session_id FROM runs r JOIN sessions s ON s.id=r.session_id "
                "WHERE r.request_id=? AND s.agent_id=? AND s.project_id IS ?",
                (trigger_id, source["agent_id"], source["project_id"])).fetchall()
        if len(accepted) > 1:
            raise StateError("Automation receipt has ambiguous prior executions; reconcile before continuing")
        route = "automation:" + job_id + (":trigger:" + trigger_id if mode == "fresh" else "")
        sid = accepted[0]["session_id"] if accepted else source_id if mode == "source" else self.automation_session(source_id, route)
        with self.transaction() as db:
            self._require_session_writable(db, source_id)
            self._require_session_writable(db, sid)
            db.execute("INSERT INTO automation_trigger_routes VALUES(?,?,?,?) ON CONFLICT(trigger_id) DO NOTHING",
                       (trigger_id, sid, mode, time.time()))
            pinned = db.execute("SELECT * FROM automation_trigger_routes WHERE trigger_id=?", (trigger_id,)).fetchone()
            return dict(pinned)
