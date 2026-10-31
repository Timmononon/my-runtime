"""Human browser controls alongside a run, without adding a session writer."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

from jsonschema import Draft202012Validator

from aster.config import load_config, read_yaml
from aster.domain import ToolCall, ToolContext, ToolResult, to_dict
from aster.policy import PermissionPolicy
from aster.state import StateError
from aster.state.store import dumps


class BrowserController:
    def __init__(self, coordinator):
        self.coordinator, self.store = coordinator, coordinator.store
        self._locks = {}
        self._closed = False
        with self.store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS browser_control_receipts(
                session_id TEXT NOT NULL,request_id TEXT NOT NULL,action TEXT NOT NULL,
                arguments_sha TEXT NOT NULL,state TEXT NOT NULL,result_json TEXT,
                created_at REAL NOT NULL,updated_at REAL NOT NULL,PRIMARY KEY(session_id,request_id))""")
            db.execute("CREATE TABLE IF NOT EXISTS browser_handoffs(session_id TEXT PRIMARY KEY,paused INTEGER NOT NULL,updated_at REAL NOT NULL)")
            # This coordinator is a fresh owner. Interrupted control receipts
            # remain inspectable but never become replayable or block deletion.
            db.execute("UPDATE browser_control_receipts SET state='outcome_unknown' WHERE state IN ('prepared','dispatching')")

    def busy(self, session_ids):
        return any(self._locks.get(session_id) and self._locks[session_id].locked() for session_id in session_ids)

    def receipts(self, session_id):
        with self.store._lock:
            rows = self.store.connection.execute("SELECT request_id,action,state,updated_at FROM browser_control_receipts WHERE session_id=? ORDER BY updated_at DESC LIMIT 50", (session_id,)).fetchall()
        return [dict(row) for row in rows]

    async def cleanup(self, session_id, *, deleted=False):
        factory = self.coordinator.factory
        if factory and factory.browser:
            context = ToolContext(session_id, "browser_cleanup", self.coordinator.data_dir,
                                  self.coordinator.data_dir / "artifacts" / session_id, asyncio.Event())
            await factory.browser.call("close", {}, context)
        if deleted:
            with self.store.transaction() as db:
                db.execute("DELETE FROM browser_control_receipts WHERE session_id=?", (session_id,))
                db.execute("DELETE FROM browser_handoffs WHERE session_id=?", (session_id,))
            self._locks.pop(session_id, None)
            if factory and factory.browser:
                factory.browser._paused.discard(session_id)

    def paused(self, session_id):
        with self.store._lock:
            row = self.store.connection.execute("SELECT paused FROM browser_handoffs WHERE session_id=?", (session_id,)).fetchone()
        return bool(row and row[0])

    async def execute(self, session_id, action, arguments, request_id, *, approved):
        if self._closed or self.coordinator._closed:
            return ToolResult("Browser operator control is shutting down", True, "cancelled", "not_executed")
        if not approved:
            return ToolResult("Explicit authenticated operator approval is required", True, "permission", "not_executed")
        names = {"attach": "browser_attach", "pause": "browser_handoff", "resume": "browser_handoff", "detach": "browser_close"}
        if action not in names or not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError("Browser control requires a fixed action and bounded request_id")
        self.store.require_session_writable(session_id)
        session = self.store.get_session(session_id)
        factory = self.coordinator.factory
        if not factory or not factory.browser:
            return ToolResult("Browser service is unavailable", True, "unavailable", "not_executed")
        tool = next((tool for tool in factory.tools if tool.spec.name == names[action]), None)
        if tool is None:
            return ToolResult("Browser control tool is unavailable", True, "unavailable", "not_executed")
        args = {"operation": action} if action in {"pause", "resume"} else dict(arguments)
        if action in {"pause", "resume", "detach"} and arguments:
            raise ValueError("This browser control does not accept extra arguments")
        if list(Draft202012Validator(tool.spec.parameters).iter_errors(args)):
            raise ValueError("Browser control arguments are invalid")
        digest = hashlib.sha256(dumps({"action": action, "arguments": args}).encode()).hexdigest()
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            if self._closed or self.coordinator._closed:
                return ToolResult("Browser operator control is shutting down", True, "cancelled", "not_executed")
            self.store.require_session_writable(session_id)
            with self.store.transaction() as db:
                old = db.execute("SELECT * FROM browser_control_receipts WHERE session_id=? AND request_id=?", (session_id, request_id)).fetchone()
                if old:
                    if old["arguments_sha"] != digest:
                        raise StateError("Browser request_id was reused with different approved arguments")
                    if old["result_json"]:
                        return ToolResult(**json.loads(old["result_json"]))
                    # A restarted or disconnected control is never automatically
                    # replayed, even when the browser's actual state is uncertain.
                    return ToolResult("Earlier browser control has an uncertain receipt; inspect state before a new explicit action",
                                      True, "browser_control", "unknown")
            settings = load_config(self.coordinator.data_dir, agent_id=session["agent_id"], project_id=session["project_id"], cwd=Path(session["cwd"])).data
            permissions = settings.get("permissions", {})
            denied = set(permissions.get("denied_tools", []))
            if permissions.get("allowed_tools") is not None and names[action] not in permissions["allowed_tools"]:
                denied.add(names[action])
            policy = PermissionPolicy(denied_tools=denied,
                                      allowed_tools=set(permissions.get("auto_approve_tools", [])),
                                      always_ask=set(permissions.get("ask_tools", [])))
            context = ToolContext(session_id, "browser_operator", Path(session["cwd"]),
                                  self.coordinator.data_dir / "artifacts" / session_id / "browser_controls", asyncio.Event(),
                                  services={"browser_operator": True,
                                            "browser_config_loader": lambda: read_yaml(self.coordinator.data_dir / "config.yaml").get("browser", {})})
            if policy.evaluate(tool.spec, ToolCall(request_id, names[action], args), context).action == "deny":
                return ToolResult("Browser control is denied by configured permissions", True, "permission", "not_executed")
            now = time.time()
            with self.store.transaction() as db:
                db.execute("INSERT INTO browser_control_receipts VALUES(?,?,?,?,?,?,?,?)",
                           (session_id, request_id, action, digest, "prepared", None, now, now))
                # Persist handoff before dispatch, so restart recovery cannot
                # infer permission to resume from a missing control result.
                if action == "pause":
                    db.execute("INSERT OR REPLACE INTO browser_handoffs VALUES(?,1,?)", (session_id, now))
            if action == "pause":
                # Admission and durable intent precede this flag. Stop queued/new
                # mutations immediately; the in-flight action still settles normally.
                factory.browser._paused.add(session_id)
            self.store.emit_event(session_id, None, "browser.control", {"request_id": request_id, "action": action, "state": "prepared"})
            with self.store.transaction() as db:
                db.execute("UPDATE browser_control_receipts SET state='dispatching',updated_at=? WHERE session_id=? AND request_id=?", (time.time(), session_id, request_id))
            try:
                result = await tool.execute(args, context)
            except BaseException:
                with self.store.transaction() as db:
                    db.execute("UPDATE browser_control_receipts SET state='outcome_unknown',updated_at=? WHERE session_id=? AND request_id=?", (time.time(), session_id, request_id))
                self.store.emit_event(session_id, None, "browser.control", {"request_id": request_id, "action": action, "state": "outcome_unknown"})
                raise
            with self.store.transaction() as db:
                if action == "resume" and not result.is_error:
                    db.execute("INSERT OR REPLACE INTO browser_handoffs VALUES(?,0,?)", (session_id, time.time()))
                state = "outcome_unknown" if result.outcome == "unknown" else "completed"
                db.execute("UPDATE browser_control_receipts SET state=?,result_json=?,updated_at=? WHERE session_id=? AND request_id=?",
                           (state, dumps(to_dict(result)), time.time(), session_id, request_id))
            self.store.emit_event(session_id, None, "browser.control", {"request_id": request_id, "action": action,
                                                                        "state": state, "is_error": result.is_error})
            return result

    async def close(self):
        self._closed = True
        for lock in list(self._locks.values()):
            async with lock:
                pass  # Drain receipts before the coordinator closes its database.
