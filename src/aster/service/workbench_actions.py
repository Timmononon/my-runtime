"""Local project records and explicit workbench context actions."""
from __future__ import annotations

import asyncio
import base64
import os
import re
import time
import uuid
from pathlib import Path, PurePosixPath

import yaml

from aster.config import load_config, read_yaml, safe_identifier
from aster.customization.skills import Skill, SkillRegistry, SkillScope
from aster.engine.context import ContextManager, estimate_tokens
from aster.engine.context_usage import context_usage_basis
from aster.packages.manager import tree_digest
from aster.platform.filesystem import FileLock, atomic_write
from aster.state.lifecycle import managed_project_id
from aster.state.store import ACTIVE, SessionBusy, StateError


def local_directory(value: str | Path) -> Path:
    raw = str(value)
    path = Path(raw).expanduser()
    if not raw.strip() or "\0" in raw or not path.is_absolute() or raw.startswith(("\\\\", "//")):
        raise ValueError("Choose an absolute local directory path")
    path = path.resolve(strict=True)
    if not path.is_dir():
        raise ValueError("Choose an existing directory, not a file")
    if os.name == "nt":
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(path.anchor) == 4:
            raise ValueError("Network drives are not supported by the local folder chooser")
    return path


class ProjectService:
    def __init__(self, coordinator):
        self.coordinator, self.store = coordinator, coordinator.store
        self.root = coordinator.data_dir / "projects"
        with self.store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS workbench_projects(
              id TEXT PRIMARY KEY,name TEXT NOT NULL,cwd TEXT NOT NULL,
              created_at REAL NOT NULL,updated_at REAL NOT NULL,removed_at REAL)""")
            # Legacy project recycle-bin entries are only associations. Preserve
            # session scopes and overlays while retiring their recoverable records.
            db.execute("DELETE FROM workbench_projects WHERE removed_at IS NOT NULL")

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120 or any(ord(ch) < 32 for ch in name):
            raise ValueError("Project name must contain 1-120 visible characters")
        return name.strip()

    def _path(self, project_id):
        safe_identifier(project_id)
        path = self.root / project_id / "config.yaml"
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise PermissionError("Project configuration escapes its Runtime directory")
        return path

    def get(self, project_id, *, include_removed=False):
        safe_identifier(project_id)
        with self.store._lock:
            row = self.store.connection.execute("SELECT * FROM workbench_projects WHERE id=?", (project_id,)).fetchone()
        if row is None or row["removed_at"] is not None and not include_removed:
            raise KeyError(project_id)
        return dict(row)

    def list(self, *, include_removed=False):
        with self.store._lock:
            rows = self.store.connection.execute("""SELECT p.*,COUNT(s.id) AS session_count
              FROM workbench_projects p LEFT JOIN sessions s ON s.project_id=p.id
              WHERE (? OR p.removed_at IS NULL) GROUP BY p.id ORDER BY p.updated_at DESC,p.id""", (include_removed,)).fetchall()
        return [dict(row) for row in rows]

    def _session_count(self, project_id):
        with self.store._lock:
            return self.store.connection.execute("SELECT COUNT(*) FROM sessions WHERE project_id=?", (project_id,)).fetchone()[0]

    def session_cwd(self, project_id, proposed=None):
        """Managed projects own new-session CWD; legacy config-only IDs still work."""
        try:
            record = self.get(project_id, include_removed=True)
        except KeyError:
            if managed_project_id(project_id):
                raise ValueError("This project was permanently removed; choose an existing project") from None
            return proposed
        if record["removed_at"] is not None:
            raise ValueError("This project was removed; choose another project or create a new conversation")
        return local_directory(record["cwd"])

    def _save(self, name, cwd, project_id=None):
        now = time.time()
        updating = project_id is not None
        project_id = project_id or "project_" + uuid.uuid4().hex
        target = self._path(project_id)
        with FileLock(self.root / ".workbench.lock"):
            before = target.read_bytes() if target.exists() else None
            configuration = read_yaml(target)
            configuration["cwd"] = str(cwd)
            try:
                with self.store.transaction() as db:
                    row = db.execute("SELECT * FROM workbench_projects WHERE id=?", (project_id,)).fetchone()
                    if updating and row is None:
                        raise KeyError(project_id)
                    if row and row["removed_at"] is not None:
                        raise ValueError("Removed projects cannot be edited")
                    db.execute("""INSERT INTO workbench_projects VALUES(?,?,?,?,?,NULL)
                      ON CONFLICT(id) DO UPDATE SET name=excluded.name,cwd=excluded.cwd,updated_at=excluded.updated_at""",
                               (project_id, name, str(cwd), row["created_at"] if row else now, now))
                    atomic_write(target, yaml.safe_dump(configuration, allow_unicode=True, sort_keys=False).encode("utf-8"))
            except Exception:
                # SQLite metadata is authoritative. Preserve pre-existing overlay
                # settings if a synchronous filesystem/DB write fails.
                if before is not None:
                    atomic_write(target, before)
                elif target.exists():
                    target.unlink()
                raise
        return {**self.get(project_id), "session_count": self._session_count(project_id)}

    def create(self, name, cwd):
        return self._save(self._name(name), local_directory(cwd))

    def update(self, project_id, *, name=None, cwd=None):
        previous = self.get(project_id)
        return self._save(self._name(name) if name is not None else previous["name"],
                          local_directory(cwd) if cwd is not None else Path(previous["cwd"]), project_id)

    def remove(self, project_id):
        self.get(project_id)
        with self.store.transaction() as db:
            sessions = db.execute("SELECT id FROM sessions WHERE project_id=?", (project_id,)).fetchall()
            changed = db.execute("DELETE FROM workbench_projects WHERE id=?", (project_id,))
            if changed.rowcount != 1:
                raise KeyError(project_id)
            for row in sessions:
                self.store._event(db, row["id"], None, "project.removed", {"project_id": project_id, "permanent": True})
        # Project config remains a scope overlay for surviving conversations. It
        # contains no recoverable project name/record and never deletes user files.
        return {"removed": True, "permanent": True, "detached_sessions": 0, "archived_sessions": 0, "preserved_sessions": len(sessions),
                "history_preserved": True, "files_preserved": True}

    def restore(self, project_id):
        raise StateError("Project deletion is permanent; restoration is unavailable")

    def folders(self, path=None):
        roots = []
        if os.name == "nt":
            import ctypes
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                candidate = f"{letter}:\\"
                if ctypes.windll.kernel32.GetDriveTypeW(candidate) in {2, 3, 5, 6}:
                    roots.append({"name": candidate, "path": candidate})
        else:
            roots = [{"name": "/", "path": "/"}]
        default = load_config(self.coordinator.data_dir).cwd
        chosen = local_directory(path) if path else local_directory(default if default.is_dir() else Path.home())
        entries, scanned, truncated = [], 0, False
        with os.scandir(chosen) as iterator:
            for entry in iterator:
                scanned += 1
                # Directory browsing is an explicit UI action, never a recursive
                # index. Return bounded partial results in very large folders.
                if scanned > 2000 or len(entries) >= 200:
                    truncated = True
                    break
                try:
                    if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                        continue
                    candidate = local_directory(entry.path)
                    if not candidate.is_relative_to(chosen):
                        continue
                    entries.append({"name": entry.name, "path": str(candidate)})
                except (OSError, ValueError):
                    continue
        return {"path": str(chosen), "parent": str(chosen.parent) if chosen.parent != chosen else None,
                "entries": sorted(entries, key=lambda item: item["name"].casefold()), "truncated": truncated, "roots": roots}


class WorkbenchActions:
    def __init__(self, coordinator):
        self.coordinator, self.store, self.data_dir = coordinator, coordinator.store, coordinator.data_dir
        self.projects = ProjectService(coordinator)

    def installed_skills(self, *, agent_id="default", project_id=None, cwd=None):
        settings = load_config(self.data_dir, agent_id=agent_id, project_id=project_id, cwd=cwd).data
        registry = SkillRegistry(self.data_dir / "skills")
        registry.reload()
        scope = SkillScope(registry, bound_skills=settings.get("skills"))
        return [{"name": skill.name, "description": skill.description, "version": skill.version,
                 "allowed_tools": sorted(skill.allowed_tools) if skill.allowed_tools is not None else None}
                for skill in registry._skills.values() if scope.bound_skills is None or skill.name in scope.bound_skills]

    def snapshot_selected_skill(self, name, settings):
        if name is None:
            return None
        registry = SkillRegistry(self.data_dir / "skills")
        registry.reload()
        skill = SkillScope(registry, bound_skills=settings.get("skills")).activate(safe_identifier(name))
        return {"name": skill.name, "description": skill.description, "content": skill.content, "path": str(skill.path),
                "version": skill.version, "allowed_tools": sorted(skill.allowed_tools) if skill.allowed_tools is not None else None,
                "secrets": dict(skill.secrets), "files": {name: base64.b64encode(content).decode("ascii") for name, content in skill.files}}

    def restore_selected_skill(self, snapshot, registry, scope):
        name = safe_identifier(snapshot["name"])
        if scope.bound_skills is not None and name not in scope.bound_skills:
            raise ValueError("Selected skill is not bound to this Agent")
        path = Path(snapshot["path"]).resolve()
        if path.name != "SKILL.md" or not path.is_relative_to((self.data_dir / "skills").resolve()):
            raise ValueError("Selected skill path is outside the installed skill directory")
        files = {}
        for filename, encoded in snapshot["files"].items():
            parts = PurePosixPath(filename)
            if parts.is_absolute() or ".." in parts.parts or "\\" in filename or ":" in filename:
                raise ValueError("Selected skill snapshot contains an invalid reference")
            if not isinstance(encoded, str) or len(encoded) > 1_400_000:
                raise ValueError("Selected skill snapshot exceeds file budget")
            files[filename] = base64.b64decode(encoded, validate=True)
            if len(files) > 250 or len(files[filename]) > 1024 * 1024 or sum(map(len, files.values())) > 10 * 1024 * 1024:
                raise ValueError("Selected skill snapshot exceeds bundle budget")
        if tree_digest(files) != snapshot["version"] or "SKILL.md" not in files:
            raise ValueError("Selected skill snapshot no longer matches its recorded version")
        # Match SkillRegistry.read_text's universal-newline behavior on Windows;
        # the digest still covers the original, unmodified source bytes.
        source = files["SKILL.md"].decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        match = re.match(r"\A---\s*\r?\n(.*?)\r?\n---(?:\r?\n|$)(.*)\Z", source, re.DOTALL)
        metadata, body = (yaml.safe_load(match[1]) or {}, match[2]) if match else ({}, source)
        allowed = metadata.get("allowed-tools")
        if isinstance(allowed, str):
            allowed = re.split(r"[\s,]+", allowed.strip()) if allowed.strip() else []
        if (str(metadata.get("name", path.parent.name)) != name or body != snapshot["content"]
                or (sorted(set(allowed)) if allowed is not None else None) != snapshot["allowed_tools"]
                or metadata.get("secrets", {}) != snapshot["secrets"]):
            raise ValueError("Selected skill metadata does not match the pinned source")
        skill = Skill(name, snapshot["description"], path, body, snapshot["version"],
                      None if allowed is None else frozenset(allowed), tuple(snapshot["secrets"].items()), tuple(files.items()))
        registry._skills[name] = skill
        scope._active[name] = skill
        return skill

    async def compact(self, session_id, *, method="deterministic"):
        if self.coordinator._closed:
            raise StateError("Runtime is shutting down")
        task = asyncio.current_task()
        manual_tasks = self.coordinator._manual_tasks
        manual_tasks.add(task)
        try:
            return await self._compact(session_id, method=method)
        finally:
            manual_tasks.discard(task)

    async def _compact(self, session_id, *, method="deterministic"):
        if method not in {"deterministic", "semantic"}:
            raise ValueError("Compaction method must be deterministic or semantic")
        session = self.store.get_session(session_id)
        gate = self.coordinator._session_gates.setdefault(session_id, asyncio.Lock())

        def require_idle():
            if self.coordinator._closed:
                raise StateError("Runtime is shutting down")
            self.store.require_session_writable(session_id)
            lineage = self.store.lineage(session_id)
            if lineage and lineage.get("closed_at") is not None:
                raise StateError("Closed side chats cannot be compacted")
            if self.store.list_runs(session_id, statuses=(*ACTIVE, "queued")) or self.store.has_unresolved(session_id):
                raise SessionBusy("Manual compaction requires an idle session with no queued or uncertain actions")

        if gate.locked():
            raise SessionBusy("The session writer is active; wait before compacting")
        require_idle()
        async with gate:
            require_idle()
            if method == "semantic":
                if not self.store.active_history(session_id):
                    return await self._semantic_compact(session_id)
                async with self.coordinator._capacity:
                    require_idle()
                    return await self._semantic_compact(session_id)
            # No await between the second admission check and checkpoint commit.
            # Submit/recovery on this coordinator cannot interleave this writer.
            messages = self.store.active_history(session_id)
            settings = load_config(self.data_dir, agent_id=session["agent_id"], project_id=session["project_id"],
                                   cwd=Path(session["cwd"])).data
            options = self.coordinator._run_config(settings)
            manager = ContextManager(context_tokens=options.context_tokens, reserve_tokens=options.reserve_tokens,
                                     retain_turns=options.retained_context_rounds, tool_output_chars=options.tool_output_chars,
                                     absolute_buffer_tokens=options.compaction_absolute_buffer_tokens,
                                     summary_input_cap_tokens=options.summary_input_cap_tokens)
            manager.protect_messages(self.store.history(session_id), from_archive=True)
            before = estimate_tokens(messages)
            shaped = manager.compact(messages, [], "", force=True) if messages else messages
            after = estimate_tokens(shaped)
            changed = shaped is not messages and after < before
            if not changed:
                shaped, after = messages, before
            with self.store._lock:
                count = self.store.connection.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (session_id,)).fetchone()[0]
            result = {"changed": changed, "before_tokens": before, "after_tokens": after,
                      "archive_messages": count, "preserved_archive": True, "method": "deterministic",
                      "semantic_applied": False,
                      "active_messages_before": len(messages), "active_messages_after": len(shaped)}
            if changed:
                previous = self.store.get_checkpoint(session_id)
                runs = self.store.list_runs(session_id)
                run_id = runs[0]["id"] if runs else "manual_compaction"
                try:
                    self.store.save_compaction_snapshot(session_id, runs[0]["id"] if runs else None, messages,
                        phase="manual", retention=options.compaction_snapshot_retention,
                        max_age_days=options.compaction_snapshot_max_age_days)
                except Exception as error:  # noqa: BLE001 - snapshot failures cannot block compaction.
                    self.store.emit_event(session_id, None, "context.snapshot_failed", {"phase": "manual", "category": type(error).__name__})
                metadata = {**(previous["metadata"] if previous else {}), "manual_compaction": result,
                            "last_prompt_total": None}
                self.store.checkpoint(session_id, run_id, shaped, metadata=metadata)
            self.store.emit_event(session_id, None, "context.manually_compacted", result)
            return result

    async def _semantic_compact(self, session_id):
        """Explicit paid-capable action; caller holds the session's one-writer gate.

        The helper has a durable budget/run receipt, but adds no conversation
        messages, tool executions or Hooks/MCP discovery to the raw transcript.
        """
        import dataclasses

        from aster.domain import ToolContext, to_dict
        from aster.engine.compaction import CompactionController, calibration_fingerprint
        from aster.engine.context import ContextBudgetExceeded
        from aster.engine.observations import ObservationManager
        from aster.state.store import dumps

        messages = self.store.active_history(session_id)
        if not messages:
            return {"changed": False, "before_tokens": 0, "after_tokens": 0, "archive_messages": 0,
                    "preserved_archive": True, "method": "semantic", "semantic_applied": False,
                    "summary_status": "not_needed", "active_messages_before": 0, "active_messages_after": 0}
        captured = self.coordinator.capture_automation_execution(session_id, self.coordinator.session_selection(session_id))
        payload = {**captured, "unattended": False, "kind": "compaction", "text": "", "user_message": None}
        config = self.coordinator._run_config(captured["snapshot"])
        run, _ = self.store.create_run(session_id, system=captured["system"], config=to_dict(config), request_payload=payload)
        rid, resources = run["id"], []
        cancel = self.coordinator._controls[rid] = asyncio.Event()
        status, error = "failed", "Compaction did not complete"
        try:
            self.store.emit_event(session_id, rid, "run.phase", {"phase": "setup"})
            engine, services, resources = await self.coordinator._build_engine(run, payload, resources=resources, context_only=True)
            self.coordinator._engines[rid] = engine
            engine._active[rid] = cancel
            config = dataclasses.replace(engine.config, compaction_reanchor=False)
            with self.store.transaction() as db:
                db.execute("UPDATE runs SET config_json=? WHERE id=?", (dumps({**to_dict(config), "_runtime_identity": engine._runtime_identity()}), rid))
            self.store.start_budget(rid, config.deadline_seconds)
            deadline = time.monotonic() + config.deadline_seconds

            current_phase = "setup"

            async def emit(kind, data):
                nonlocal current_phase
                phase = {"context.compacting": "compacting", "model.request": "awaiting_model",
                         "model.retry": "retrying_model", "model.finished": "compacting"}.get(kind)
                if phase and phase != current_phase:
                    current_phase = phase
                    self.store.emit_event(session_id, rid, "run.phase", {"phase": phase})
                self.store.emit_event(session_id, rid, kind, data)

            context = ToolContext(session_id, rid, Path(captured["snapshot"]["cwd"]), engine.artifact_root / session_id / rid,
                                  cancel, emit, services)
            async def helper(source, system, purpose):
                consumed = sum((row.get("input_tokens") or 0) + (row.get("output_tokens") or 0) for row in self.store.usage(rid))
                if consumed + estimate_tokens(source, [], system) >= config.max_total_tokens:
                    raise ContextBudgetExceeded("Compaction helper input exceeds the remaining token budget")
                async def progress(kind, data):
                    await emit("context.summary.progress", {"provider_event": kind, "purpose": purpose})
                return await engine._generate(source, system, context, config, deadline, tools_override=[],
                                              emit_override=progress, purpose=purpose)

            observer = ObservationManager(self.store, context, restore_files=config.file_restore_max_files,
                restore_chars=config.file_restore_chars, restore_tokens=config.file_restore_tokens,
                restore_per_file_tokens=config.file_restore_per_file_tokens, image_keep=config.image_keep,
                browser_image_keep=config.browser_image_keep)
            previous = self.store.get_checkpoint(session_id)
            prior_metadata = previous["metadata"] if previous else {}
            fingerprint = calibration_fingerprint(engine._runtime_identity(), engine.registry.specs(), run["system"])
            controller = CompactionController(self.store, context, config, observer, system=run["system"],
                                              tools=lambda: engine._offered_specs(context), helper=helper, emit=emit,
                                              checkpoint=prior_metadata)
            overhead = prior_metadata.get("overhead_tokens", 0)
            if prior_metadata.get("calibration_fingerprint") == fingerprint and type(overhead) is int and overhead >= 0:
                controller.manager.overhead_tokens = overhead
            before = estimate_tokens(messages)
            shaped = await controller.prepare(messages, force=True, phase="manual", semantic=True)
            # The helper can finish while cancellation is pending; check again
            # before publishing its result as the model-live checkpoint.
            if cancel.is_set():
                raise asyncio.CancelledError
            after = estimate_tokens(shaped)
            changed = shaped is not messages and after < before
            result = {"changed": changed, "before_tokens": before, "after_tokens": after if changed else before,
                "archive_messages": len(self.store.history(session_id)), "preserved_archive": True,
                "method": "semantic", "run_id": rid, "active_messages_before": len(messages),
                "semantic_applied": changed and controller.manager.last_summary_status in {"succeeded", "cached"}
                    and any(message.metadata.get("compaction_summary") and message.metadata.get("semantic") for message in shaped),
                "summary_status": controller.manager.last_summary_status,
                "active_messages_after": len(shaped) if changed else len(messages)}
            if changed:
                self.store.checkpoint(session_id, rid, shaped, metadata={**prior_metadata, "manual_compaction": result,
                    **controller.state(), "overhead_tokens": controller.manager.overhead_tokens,
                    "context_usage": context_usage_basis(run["system"], engine._offered_specs(context), config,
                        session_id=session_id, model=str(getattr(engine.model, "model", "")),
                        overhead_tokens=controller.manager.overhead_tokens),
                    "calibration_fingerprint": fingerprint})
            await emit("context.manually_compacted", result)
            status, error = "completed", None
            return result
        except asyncio.CancelledError:
            status, error = "cancelled", "Manual compaction cancelled; original history remains intact"
            raise
        except Exception as exc:
            error = "Manual compaction failed: " + type(exc).__name__
            raise
        finally:
            try:
                self.store.emit_event(session_id, rid, "run.phase", {"phase": "done"})
                self.store.finish_run(rid, status, error=error, payload={"status": status, "kind": "compaction"})
            finally:
                self.coordinator._controls.pop(rid, None)
                self.coordinator._engines.pop(rid, None)
                if "engine" in locals():
                    engine._active.pop(rid, None)
                await self.coordinator._close_resources(resources, run_id=rid)
