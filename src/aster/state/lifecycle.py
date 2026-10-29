"""Recoverable archives and permanent conversation deletion."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path


def initialize_lifecycle(db):
    # Keep the v2 sessions layout intact (legacy writers use positional INSERTs).
    db.execute("""CREATE TABLE IF NOT EXISTS session_lifecycle(
      session_id TEXT PRIMARY KEY REFERENCES sessions(id), archived_at REAL, deleted_at REAL)""")


def managed_project_id(value):
    return isinstance(value, str) and re.fullmatch(r"project_[0-9a-f]{32}", value) is not None


class SessionLifecycle:
    def purge_legacy_deleted_sessions(self):
        """Retire old recycle-bin records without erasing unresolved action evidence."""
        from aster.state.store import SessionBusy
        rows = self.connection.execute("SELECT session_id FROM session_lifecycle WHERE deleted_at IS NOT NULL").fetchall()
        for row in rows:
            try:
                self.delete_session(row[0])
            except SessionBusy:
                # Hidden from every listing and never restorable. An operator can
                # reconcile the retained run, then explicitly finish deletion.
                continue

    def _require_project_available(self, db, project_id):
        if project_id and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workbench_projects'").fetchone():
            row = db.execute("SELECT removed_at FROM workbench_projects WHERE id=?", (project_id,)).fetchone()
            if row and row[0] is not None or row is None and managed_project_id(project_id):
                raise ValueError("This project was permanently removed; choose an existing project")

    def _require_session_writable(self, db, session_id):
        from aster.state.store import StateError
        row = db.execute("SELECT s.id,l.archived_at,l.deleted_at FROM sessions s LEFT JOIN session_lifecycle l "
                         "ON l.session_id=s.id WHERE s.id=?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(session_id)
        if row["deleted_at"] is not None or row["archived_at"] is not None:
            raise StateError("Unarchive this conversation before changing it")

    def require_session_writable(self, session_id):
        with self._lock:
            self._require_session_writable(self.connection, session_id)

    def lifecycle_related_sessions(self, session_id):
        """Branches depend on ancestor evidence; a parent must not hide live descendants."""
        self.get_session(session_id)
        with self._lock:
            children = self.connection.execute("""WITH RECURSIVE children(id) AS (
              SELECT ? UNION SELECT l.child_id FROM session_lineage l JOIN children c ON l.parent_id=c.id)
              SELECT id FROM children""", (session_id,)).fetchall()
        return {row[0] for row in children} | set(self.session_ancestors(session_id))

    def _require_lifecycle_idle(self, db, session_ids, *, automations=True):
        from aster.state.store import SessionBusy
        ids = tuple(session_ids)
        placeholders = ",".join("?" for _ in ids)
        if not ids:
            return
        if db.execute(f"SELECT 1 FROM runs WHERE session_id IN ({placeholders}) AND status IN "
                      "('queued','running','waiting_approval','waiting_input','cancelling') LIMIT 1", ids).fetchone():
            raise SessionBusy("Finish or cancel active and queued work in this conversation or its related branches first")
        if db.execute(f"SELECT 1 FROM tool_executions WHERE session_id IN ({placeholders}) "
                      "AND state IN ('prepared','dispatching','outcome_unknown') LIMIT 1", ids).fetchone():
            raise SessionBusy("Reconcile unfinished or uncertain tool actions before changing conversation visibility")
        if db.execute(f"SELECT 1 FROM pending_requests WHERE session_id IN ({placeholders}) "
                      "AND state='pending' LIMIT 1", ids).fetchone():
            raise SessionBusy("Resolve pending approval or input requests first")
        if db.execute(f"SELECT 1 FROM mailbox m LEFT JOIN mailbox_lifecycle l ON l.message_id=m.id "
                      f"WHERE m.session_id IN ({placeholders}) AND m.consumed_run_id IS NULL "
                      "AND COALESCE(l.state,'pending')='pending' LIMIT 1", ids).fetchone():
            raise SessionBusy("Send or retract pending messages before changing conversation visibility")
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='automation_jobs'").fetchone():
            if automations and db.execute(f"SELECT 1 FROM automation_jobs WHERE session_id IN ({placeholders}) "
                                          "AND enabled=1 LIMIT 1", ids).fetchone():
                raise SessionBusy("Disable enabled automations for this conversation or its related branches first")
            if automations and db.execute(f"SELECT 1 FROM automation_jobs j JOIN automation_routes r "
                                          "ON r.route_key='automation:' || j.id "
                                          f"WHERE r.session_id IN ({placeholders}) AND j.enabled=1 LIMIT 1", ids).fetchone():
                raise SessionBusy("Disable the enabled automation that reuses this conversation first")
            if db.execute(f"SELECT 1 FROM automation_triggers t JOIN automation_jobs j ON j.id=t.job_id "
                          f"WHERE j.session_id IN ({placeholders}) AND t.state IN ('prepared','submitted') LIMIT 1", ids).fetchone():
                raise SessionBusy("Finish pending automation deliveries before changing conversation visibility")
            if db.execute(f"SELECT 1 FROM automation_triggers t JOIN automation_trigger_routes r ON r.trigger_id=t.id "
                          f"WHERE r.session_id IN ({placeholders}) AND t.state IN ('prepared','submitted') LIMIT 1", ids).fetchone():
                raise SessionBusy("Finish pending automation deliveries into this conversation first")

    @staticmethod
    def lifecycle_noop(session, action):
        from aster.state.store import StateError
        if action not in {"archive", "unarchive", "delete"}:
            raise ValueError("Unknown conversation lifecycle action")
        if action in {"archive", "unarchive"} and session["deleted_at"] is not None:
            raise StateError("This conversation was deleted permanently")
        return {"archive": session["archived_at"] is not None,
                "unarchive": session["archived_at"] is None, "delete": False}[action]

    @staticmethod
    def _artifact_records(db, session_id=None):
        """Read explicit authority slots, matching maintenance.registered_artifacts."""
        records = {}
        clause, args = (" WHERE session_id=?", (session_id,)) if session_id else ("", ())

        def slots(value):
            for record in [*value.get("artifacts", []), value.get("output_artifact")]:
                if isinstance(record, dict) and isinstance(record.get("artifact_id"), str) and isinstance(record.get("path"), str):
                    records[record["artifact_id"]] = record

        for row in db.execute("SELECT message_json FROM messages" + clause, args):
            slots(json.loads(row[0]).get("metadata", {}))
        for row in db.execute("SELECT result_json FROM tool_executions" + clause +
                              (" AND" if clause else " WHERE") + " result_json IS NOT NULL", args):
            slots(json.loads(row[0]))
        for row in db.execute("SELECT payload_json FROM events WHERE type='artifact.registered'" +
                              (" AND session_id=?" if session_id else ""), args):
            slots(json.loads(row[0]))
        for row in db.execute("SELECT messages_json FROM checkpoints" + clause, args):
            for message in json.loads(row[0]):
                slots(message.get("metadata", {}))
        return records

    def _preserve_branch_evidence(self, db, session_id, records):
        children = db.execute("SELECT * FROM session_lineage WHERE parent_id=?", (session_id,)).fetchall()
        parent = db.execute("SELECT * FROM session_lineage WHERE child_id=?", (session_id,)).fetchone()
        executions = [dict(row) for row in db.execute("SELECT * FROM tool_executions WHERE session_id=?", (session_id,))]
        audits = [dict(row) for row in db.execute("SELECT type,payload_json,created_at FROM events WHERE session_id=? "
                  "AND type IN ('tool.reconciled','session.ancestor_deleted')", (session_id,))]
        for child in children:
            # Audit snapshots have no runnable IDs in the dispatch registry. They
            # retain determined ancestor effects without making them replayable.
            self._event(db, child["child_id"], None, "session.ancestor_deleted",
                        {"lineage": dict(child), "executions": executions, "audit_events": audits,
                         "replayable": False})
            db.execute("UPDATE session_lineage SET parent_id=?,boundary_seq=? WHERE child_id=?",
                       (parent["parent_id"] if parent else None, parent["boundary_seq"] if parent else child["boundary_seq"],
                        child["child_id"]))
        # Inherited attachments must remain downloadable after their original
        # owner's events disappear. Register them only for existing reference holders.
        for row in db.execute("SELECT DISTINCT session_id FROM messages WHERE session_id<>?", (session_id,)).fetchall():
            surviving = self._artifact_records(db, row[0])
            for identity in records.keys() & surviving.keys():
                record = {**records[identity], "session_id": row[0]}
                self._event(db, row[0], None, "artifact.registered", {"artifacts": [record]})
                if record.get("attachment_id"):
                    self._event(db, row[0], None, "attachment.registered", {"attachment": record})

    def delete_session(self, session_id):
        """Forget a session and its owned state; live branches keep their evidence."""
        with self._lock:
            with self.transaction() as db:
                self.get_session(session_id)
                self._require_lifecycle_idle(db, self.lifecycle_related_sessions(session_id))
                records = self._artifact_records(db, session_id)
                # Copy branch receipts and surviving artifact grants before their
                # source rows disappear; the copies do not authorize replay.
                self._preserve_branch_evidence(db, session_id, records)
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "automation_jobs" in tables:
                    jobs = [row[0] for row in db.execute("SELECT id FROM automation_jobs WHERE session_id=?", (session_id,))]
                    for job in jobs:
                        db.execute("DELETE FROM automation_trigger_routes WHERE trigger_id IN "
                                   "(SELECT id FROM automation_triggers WHERE job_id=?)", (job,))
                        db.execute("DELETE FROM automation_triggers WHERE job_id=?", (job,))
                        db.execute("DELETE FROM automation_watch_state WHERE job_id=?", (job,))
                    db.execute("DELETE FROM automation_jobs WHERE session_id=?", (session_id,))
                db.execute("DELETE FROM mailbox_lifecycle WHERE message_id IN (SELECT id FROM mailbox WHERE session_id=?)", (session_id,))
                if self.fts_available:
                    db.execute("DELETE FROM messages_fts WHERE rowid IN (SELECT seq FROM messages WHERE session_id=?)", (session_id,))
                for table in ("usage", "run_requests", "run_budgets"):
                    db.execute(f"DELETE FROM {table} WHERE run_id IN (SELECT id FROM runs WHERE session_id=?)", (session_id,))
                for table in ("compaction_snapshots", "compaction_learnings", "session_model_selection", "checkpoints", "mailbox", "pending_requests", "plans",
                              "file_observations", "automation_routes", "automation_trigger_routes", "session_lifecycle",
                              "messages", "tool_executions", "events", "runs"):
                    if table in tables:
                        db.execute(f"DELETE FROM {table} WHERE session_id=?", (session_id,))
                db.execute("DELETE FROM session_lineage WHERE child_id=?", (session_id,))
                db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
                remaining = self._artifact_records(db)
            owned = {key: value for key, value in records.items() if key not in remaining}
            try:
                warnings = self._remove_unreferenced_artifact_files(owned, remaining)
            except (OSError, ValueError, TypeError, KeyError) as error:
                # The committed deletion cannot be undone by filesystem cleanup.
                # Report a bounded type/count, never paths or raw parser input.
                warnings = [{"code": "artifact_cleanup_failed", "error_type": type(error).__name__,
                             "unremoved_owned_artifacts": len(owned)}]
        result = {"deleted": True, "id": session_id}
        if warnings:
            result["cleanup_warnings"] = warnings
        return result

    def _remove_unreferenced_artifact_files(self, records, remaining):
        from aster.platform.filesystem import atomic_write
        if not records:
            return []
        data = self.path.parent.resolve()
        root = (data / "artifacts").resolve()
        if not root.is_relative_to(data):
            return [{"code": "artifact_cleanup_failed", "error_type": "PermissionError",
                     "unremoved_owned_artifacts": len(records)}] if records else []
        mapping_file = data / "restore-artifacts.json"
        document = json.loads(mapping_file.read_text(encoding="utf-8")) if mapping_file.exists() else {}
        if not isinstance(document, dict):
            raise TypeError("Malformed restored artifact document")
        mappings = document.get("artifact_map", {})
        if not isinstance(mappings, dict):
            raise TypeError("Malformed restored artifact mapping")
        for key, value in {**remaining, **records}.items():
            mapped = mappings.get(key)
            if mapped is not None and (not isinstance(mapped, dict) or
                    not isinstance(mapped.get("path"), str) or Path(mapped.get("original_path", "")) != Path(value["path"])):
                raise ValueError("Restored artifact mapping disagrees with its registration")
        protected = {Path(mappings.get(key, {}).get("path", value["path"])).resolve() for key, value in remaining.items()}
        warnings = []
        for key, value in records.items():
            target = Path(mappings.get(key, {}).get("path", value["path"])).resolve()
            try:
                if target.is_relative_to(root) and target not in protected and target.is_file():
                    target.unlink()
                    if target.parent != root and not any(target.parent.iterdir()):
                        target.parent.rmdir()
            except OSError as error:
                warnings.append({"code": "artifact_cleanup_failed", "error_type": type(error).__name__,
                                 "unremoved_owned_artifacts": int(target.exists())})
            mappings.pop(key, None)
        if mapping_file.exists():
            document["artifact_map"] = mappings
            atomic_write(mapping_file, json.dumps(document, ensure_ascii=False).encode("utf-8"))
        return warnings

    def change_session_lifecycle(self, session_id, action):
        if action == "delete":
            return self.delete_session(session_id)
        with self.transaction() as db:
            session = self.get_session(session_id)
            if self.lifecycle_noop(session, action):
                return session
            self._require_lifecycle_idle(db, self.lifecycle_related_sessions(session_id))
            archived, deleted = session["archived_at"], None
            if action == "archive":
                archived = time.time()
            elif action == "unarchive":
                archived = None
            db.execute("INSERT INTO session_lifecycle VALUES(?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
                       "archived_at=excluded.archived_at,deleted_at=excluded.deleted_at", (session_id, archived, deleted))
            self._event(db, session_id, None, "session." + {"archive": "archived", "unarchive": "unarchived"}[action],
                        {"archived_at": archived, "deleted_at": deleted, "history_preserved": True})
            return self.get_session(session_id)
