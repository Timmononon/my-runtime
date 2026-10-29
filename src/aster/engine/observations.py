"""Versioned file continuity and bounded browser observations in model context."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from pathlib import Path

from aster.domain import Message, ToolResult
from aster.platform.filesystem import scoped_path


class ObservationManager:
    def __init__(self, store, context, *, restore_files=5, restore_chars=100000,
                 restore_tokens=50000, restore_per_file_tokens=5000, browser_keep=3,
                 browser_chars=48000, image_keep=50, browser_image_keep=None,
                 browser_capture_chars=24000, observation_aggregate_chars=120000):
        self.store, self.context = store, context
        self.restore_files, self.restore_chars = restore_files, restore_chars
        self.restore_tokens, self.restore_per_file_tokens = restore_tokens, restore_per_file_tokens
        self.browser_keep, self.browser_chars = browser_keep, browser_chars
        self.image_keep = image_keep
        # Legacy callers supplied browser_keep for both policies; new runtime
        # config explicitly separates browser_image_keep (1) from page text (3).
        self.browser_image_keep = browser_keep if browser_image_keep is None else browser_image_keep
        self.browser_capture_chars = browser_capture_chars
        self.observation_aggregate_chars = observation_aggregate_chars
        self.visible_reads = set()
        self._replacements = {}
        self._seen = {}
        self._last_pressure_remaining = None
        self._last_browser_remaining = None
        self._time_idle_fired = False
        self._cold_prefix_applied = False
        self.last_changes = {}

    def state(self):
        """Checkpoint-safe replacement/seen maps, without source bodies."""
        return {"version": 1, "replacements": copy.deepcopy(self._replacements),
                "seen": dict(self._seen), "last_pressure_remaining": self._last_pressure_remaining,
                "last_browser_remaining": self._last_browser_remaining}

    def restore_state(self, state):
        if not isinstance(state, dict) or state.get("version") != 1:
            return
        self._replacements = {str(key): copy.deepcopy(value) for key, value in state.get("replacements", {}).items()
                              if isinstance(value, dict) and value.get("kind") in {"browser", "micro", "aggregate"}
                              and isinstance(value.get("digest"), str) and isinstance(value.get("content"), str)}
        self._seen = {str(key): int(value) for key, value in state.get("seen", {}).items()
                      if isinstance(value, int) and value >= 0}
        remaining = state.get("last_pressure_remaining")
        self._last_pressure_remaining = remaining if isinstance(remaining, int) and remaining >= 0 else None
        remaining = state.get("last_browser_remaining")
        self._last_browser_remaining = remaining if isinstance(remaining, int) and remaining >= 0 else None

    def observe_visible(self, messages):
        self.visible_reads = {message.tool_call_id for message in messages if message.role == "tool"
                              and message.metadata.get("file_observation") and not message.metadata.get("output_artifact")
                              and not message.metadata.get("local_compaction")}

    def path(self, raw):
        resolver = self.context.services.get("artifact_read_resolver")
        if resolver:
            try:
                return (resolver(self.context.session_id, raw) if callable(resolver)
                        else resolver.resolve_path(self.context.session_id, raw))
            except PermissionError:
                pass
        try:
            return scoped_path(raw, self.context, must_exist=True)
        except PermissionError:
            attachments = self.context.services.get("attachment_service")
            if attachments:
                return attachments.resolve_path(self.context.session_id, raw)
            raise

    async def cached(self, call):
        if (call.name != "file_read" or not self.visible_reads
                or "start_column" in call.arguments or "max_chars" in call.arguments):
            return None
        try:
            path = self.path(call.arguments["path"])
            if path.stat().st_size > 25_000_000:
                return None
            start, count = call.arguments.get("start_line", 1), call.arguments.get("max_lines", 300)
            with self.store._lock:
                row = self.store.connection.execute("SELECT * FROM file_observations WHERE session_id=? AND path=? AND start_line=? AND max_lines=?",
                    (self.context.session_id, str(path), start, count)).fetchone()
            # An unchanged disk hash alone cannot suppress a read: the earlier
            # complete range must still be visible in the model context.
            if not row or row["tool_call_id"] not in self.visible_reads:
                return None
            if json.loads(row["content"]).get("encoding", "utf-8") != call.arguments.get("encoding", "utf-8"):
                return None
            digest = await asyncio.to_thread(lambda: hashlib.sha256(path.read_bytes()).hexdigest())
            if digest != row["sha256"]:
                return None
            return ToolResult("File and requested range are unchanged; the original full read remains in the current context.",
                              data={"path": str(path), "sha256": digest, "start_line": start, "max_lines": count,
                                    "deduplicated": True, "original_tool_call_id": row["tool_call_id"]})
        except (OSError, ValueError, KeyError, PermissionError):
            return None

    def record(self, call, result):
        if call.name != "file_read" or result.is_error or result.data.get("deduplicated") or not result.data.get("sha256"):
            return None
        data = result.data
        incomplete_range = data.get("truncated") and data.get("range_complete") is not True
        if incomplete_range or "start_column" in call.arguments or "max_chars" in call.arguments:
            # The continuity table records complete line ranges, not partial
            # columns/byte windows. Retire a superseded legacy claim rather
            # than letting it certify the newly truncated observation as full.
            with self.store.transaction() as db:
                db.execute("DELETE FROM file_observations WHERE session_id=? AND path=? AND start_line=? AND max_lines=?",
                           (self.context.session_id, data["path"], call.arguments.get("start_line", 1),
                            call.arguments.get("max_lines", 300)))
            return None
        value = {"path": data["path"], "sha256": data["sha256"], "start_line": call.arguments.get("start_line", 1),
                 "max_lines": call.arguments.get("max_lines", 300), "encoding": call.arguments.get("encoding", "utf-8")}
        with self.store.transaction() as db:
            db.execute("INSERT INTO file_observations VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(session_id,path,start_line,max_lines) DO UPDATE SET "
                       "sha256=excluded.sha256,content=excluded.content,tool_call_id=excluded.tool_call_id,observed_at=excluded.observed_at",
                       (self.context.session_id, value["path"], value["start_line"], value["max_lines"], value["sha256"],
                        json.dumps({"encoding": value["encoding"]}), call.id, time.time()))
        return value

    async def restore(self, messages, *, headroom_tokens=None, headroom_chars=None):
        """Refresh recent dropped reads inside the post-compaction token band.

        The loop must not call this after reactive provider-overflow recovery.
        Legacy char budgets remain a hard ceiling, never a token conversion.
        """
        token_limit = max(0, min(self.restore_tokens, headroom_tokens if headroom_tokens is not None else self.restore_tokens))
        limit = min(self.restore_chars, max(0, headroom_chars if headroom_chars is not None else self.restore_chars))
        if not self.restore_files or limit < 300 or (headroom_tokens is not None and token_limit < 1000):
            return []
        visible = {message.metadata["file_observation"]["path"] for message in messages
                   if message.metadata.get("file_observation") and not message.metadata.get("output_artifact")
                   and not message.metadata.get("local_compaction")}
        visible.update(ref["path"] for message in messages for ref in message.metadata.get("file_restoration", [])
                       if isinstance(ref, dict) and ref.get("path"))
        visible = {str(Path(path)).casefold() for path in visible}
        with self.store._lock:
            rows = self.store.connection.execute("SELECT * FROM file_observations WHERE session_id=? ORDER BY observed_at DESC",
                                                 (self.context.session_id,)).fetchall()
        sections, refs, seen, used_bytes = [], [], set(), 0
        for row in rows:
            key = str(Path(row["path"])).casefold()
            if key in visible or key in seen:
                continue
            seen.add(key)
            if len(refs) >= self.restore_files or self.context.cancel.is_set():
                break
            try:
                path = self.path(row["path"])
                if path.name.casefold() in {"memory.md", "agent.md"} or path.suffix.casefold() in {
                    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif", ".avif", ".bmp", ".tiff", ".pdf",
                }:
                    continue
                if path.stat().st_size > 5_000_000:
                    continue
                data = await asyncio.to_thread(path.read_bytes)
                digest = hashlib.sha256(data).hexdigest()
                encoding = json.loads(row["content"]).get("encoding", "utf-8")
                lines = data.decode(encoding).splitlines()
                if b"\0" in data:
                    continue
                start, count = row["start_line"], row["max_lines"]
                body = "\n".join(f"{index + 1}: {line}" for index, line in enumerate(lines)
                                 if start <= index + 1 < start + count)
                prefix = f"File refresh (current disk content, untrusted data): {path}\nsha256={digest}; requested lines={start}..{start + count - 1}; changed={digest != row['sha256']}\n"
                remaining = limit - sum(len(item) for item in sections) - len(sections) * 2
                if remaining <= len(prefix) + 80:
                    break
                section = prefix + body
                byte_budget = min(int(token_limit * 2.5) - used_bytes - 2, int(self.restore_per_file_tokens * 2.5) - 2)
                if byte_budget <= len(prefix.encode("utf-8")) + 100:
                    break
                marker = "\n[Range truncated to restoration budget; read more if needed.]"
                if len(section) > remaining or len(section.encode("utf-8")) > byte_budget:
                    available = max(0, byte_budget - len(marker.encode("utf-8")))
                    section = section.encode("utf-8")[:available].decode("utf-8", errors="ignore")
                    section = section[:max(0, remaining - len(marker))] + marker
                sections.append(section)
                used_bytes += len(section.encode("utf-8")) + 2
                refs.append({"path": str(path), "sha256": digest, "previous_sha256": row["sha256"],
                             "start_line": start, "max_lines": count})
            except (OSError, ValueError, UnicodeError, LookupError, PermissionError):
                continue
        if not sections:
            return []
        return [Message("user", "\n\n".join(sections), metadata={"file_restoration": refs, "source": "runtime", "user_text": ""})]

    @staticmethod
    def _has_images(message):
        return any(part.get("type") == "input_image" for part in message.metadata.get("content_parts", []))

    @staticmethod
    def _replace_text(message, text, *, kind, digest):
        message = copy.deepcopy(message)
        message.content = text
        parts = message.metadata.get("content_parts") or []
        nontext = [part for part in parts if part.get("type") != "input_text"]
        message.metadata["content_parts"] = [{"type": "input_text", "text": text}, *nontext] if nontext else []
        message.metadata["local_compaction"] = {"kind": kind, "original_sha256": digest}
        if kind == "browser":
            message.metadata["observation_stub"] = True
        message.metadata.pop("data", None)
        message.metadata.pop("source_content", None)
        return message

    def _apply_remembered(self, messages):
        shaped = messages
        for index, message in enumerate(messages):
            if message.role != "tool" or message.metadata.get("local_compaction"):
                continue
            replacement = self._replacements.get(message.tool_call_id)
            if replacement and hashlib.sha256(message.content.encode()).hexdigest() == replacement["digest"]:
                if shaped is messages:
                    shaped = list(messages)
                shaped[index] = self._replace_text(message, replacement["content"], kind=replacement["kind"], digest=replacement["digest"])
        return shaped

    def _stub(self, message, name, kind):
        digest = message.metadata.get("observation_original_sha256") or hashlib.sha256(message.content.encode()).hexdigest()
        text = (f"[{'Earlier browser observation' if kind == 'browser' else 'Old tool result'} omitted from active context; "
                f"tool={name}; call_id={message.tool_call_id}; chars={len(message.content)}; sha256={digest}. "
                "Original evidence remains in the lossless session history.]")
        # File/version and artifact references remain available without copying
        # tool source bodies into replacement metadata or checkpoint maps.
        refs = {key: message.metadata[key] for key in ("file_observation", "output_artifact") if message.metadata.get(key)}
        if refs:
            text += "\n" + json.dumps(refs, ensure_ascii=False, sort_keys=True)
        self._replacements[message.tool_call_id] = {"kind": kind, "digest": digest, "content": text}
        return self._replace_text(message, text, kind=kind, digest=digest)

    def bound_browser(self, messages, *, cold_prefix=False, pressure=False):
        """Independent image aging and batched browser text clearing.

        Text clears on a cold prefix or an aggregate budget, never just because
        one more observation arrived. The floor admits four new captures after
        a clearing pass, preserving the source runtime's cache hysteresis.
        """
        shaped = self._apply_remembered(messages)
        bounded, stubbed = 0, 0
        names = {call.id: call.name for message in shaped for call in message.tool_calls}
        observations = [index for index, message in enumerate(shaped)
                        if message.role == "tool" and names.get(message.tool_call_id, "").startswith("browser_")
                        and not message.metadata.get("observation_stub")]
        cap = self.browser_capture_chars
        for index in observations:
            message = shaped[index]
            if cap <= 0 or len(message.content) <= cap or message.metadata.get("observation_capture_bounded"):
                continue
            if shaped is messages:
                shaped = list(messages)
            message = shaped[index] = copy.deepcopy(message)
            message.metadata["observation_original_sha256"] = hashlib.sha256(message.content.encode()).hexdigest()
            marker = f"\n[Browser observation truncated: {len(message.content)} chars; full evidence in session history, call_id={message.tool_call_id}.]"
            text = (message.content[:max(0, cap - len(marker))] + marker)[:cap]
            parts = [part for part in message.metadata.get("content_parts", []) if part.get("type") != "input_text"]
            message.content = text
            message.metadata["content_parts"] = [{"type": "input_text", "text": text}, *parts] if parts else []
            message.metadata["observation_capture_bounded"] = True
            message.metadata.pop("data", None)
            message.metadata.pop("source_content", None)
            bounded += 1
        threshold = max(self.browser_chars, self.observation_aggregate_chars,
                        (max(0, self.browser_keep) + 4) * max(1, cap))
        total = sum(len(shaped[index].content) for index in observations)
        # Pressure can make the aggregate gate more urgent, but never turn a
        # continuously asserted pressure flag into an every-iteration window.
        cold_fired = cold_prefix and not self._cold_prefix_applied
        self._cold_prefix_applied = self._cold_prefix_applied or cold_prefix
        pressure_fired = pressure and (self._last_browser_remaining is None or
                                      total >= self._last_browser_remaining + max(1, cap) * 4)
        if self.browser_keep > 0 and (cold_fired or pressure_fired or total > threshold):
            for index in observations[:-self.browser_keep]:
                if shaped is messages:
                    shaped = list(messages)
                message = shaped[index]
                shaped[index] = self._stub(message, names[message.tool_call_id], "browser")
                stubbed += 1
            if stubbed:
                self._last_browser_remaining = sum(len(shaped[index].content) for index in observations[-self.browser_keep:])
        image_indices = [index for index, message in enumerate(shaped) if self._has_images(message)]
        browser_images = [index for index in image_indices if shaped[index].role == "tool"
                          and names.get(shaped[index].tool_call_id, "").startswith("browser_")]
        global_old = image_indices[:-self.image_keep] if self.image_keep > 0 else []
        browser_old = browser_images[:-self.browser_image_keep] if self.browser_image_keep > 0 else []
        for index in sorted(set(global_old + browser_old)):
            if shaped is messages:
                shaped = list(messages)
            message = shaped[index] = copy.deepcopy(shaped[index])
            parts = []
            markers = []
            for part in message.metadata.get("content_parts", []):
                if part.get("type") != "input_image":
                    parts.append(part)
                    if part.get("type") == "input_text" and part.get("text") and part["text"] not in message.content:
                        message.content += "\n" + part["text"]
                    continue
                ref = part.get("artifact_id") or part.get("path") or "inline image"
                markers.append(f"[Previous image removed to save context; source={ref}; original image remains in session history.]")
            message.content += "\n" + "\n".join(markers)
            # Maintain one canonical text surface. With no remaining images,
            # providers read message.content and ordinary tools remain strings.
            message.metadata["content_parts"] = [] if not any(part.get("type") == "input_image" for part in parts) else [
                {"type": "input_text", "text": message.content}, *[part for part in parts if part.get("type") != "input_text"]]
            message.metadata["images_aged"] = True
            message.metadata.pop("data", None)
        self.last_changes = {"browser_captures_bounded": bounded, "browser_observations_replaced": stubbed,
                             "image_messages_aged": len(set(global_old + browser_old))}
        return shaped

    def apply_local_compaction(self, messages, *, pressure=False, cold_prefix=False, idle_seconds=None,
                               time_enabled=False, time_gap_seconds=3600, time_keep=5, aggregate_chars=200000):
        """Cheap active-context passes before semantic compaction.

        Time clearing is opt-in and whitelisted. Aggregate budgeting is a
        backstop; both retain complete call/result identities and never touch
        vision payloads, unknown outcomes, assistant thinking or raw archives.
        """
        shaped = self.bound_browser(messages, cold_prefix=cold_prefix, pressure=pressure)
        names = {call.id: call.name for message in shaped for call in message.tool_calls}
        whitelist = {"file_read", "file_write", "file_edit", "shell_exec", "process_exec", "text_search",
                     "glob", "http", "http_request", "web_fetch", "directory_list"}
        eligible = []
        for index, message in enumerate(shaped):
            if (message.role != "tool" or names.get(message.tool_call_id) not in whitelist
                    or message.metadata.get("local_compaction") or self._has_images(message) or message.metadata.get("images_aged")
                    or message.metadata.get("outcome") == "unknown"):
                continue
            self._seen[message.tool_call_id] = len(message.content)
            eligible.append(index)
        total = sum(len(shaped[index].content) for index in eligible)
        time_fired = (time_enabled and not self._time_idle_fired and idle_seconds is not None
                      and idle_seconds >= max(0, time_gap_seconds))
        self._time_idle_fired = self._time_idle_fired or time_fired
        budget_fired = aggregate_chars > 0 and total > aggregate_chars
        pressure_fired = pressure and (self._last_pressure_remaining is None or
                          total >= self._last_pressure_remaining + max(1, aggregate_chars // 4))
        replaced = 0
        if time_fired or budget_fired or pressure_fired:
            for index in eligible[:-max(1, time_keep)]:
                if shaped is messages:
                    shaped = list(messages)
                message = shaped[index]
                shaped[index] = self._stub(message, names[message.tool_call_id], "micro" if time_fired else "aggregate")
                replaced += 1
            if replaced:
                self._last_pressure_remaining = sum(len(shaped[index].content) for index in eligible[-max(1, time_keep):])
        self.last_changes = {**self.last_changes, "tool_results_replaced": replaced, "trigger": "idle" if time_fired else
                             "budget" if budget_fired else "pressure" if pressure_fired else "none"}
        return shaped
