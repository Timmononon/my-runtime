"""Hooks use an injected governed process executor, never an alternate loop."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from aster.domain import ToolCall, ToolContext, ToolResult

_inside_hook = contextvars.ContextVar("aster_inside_hook", default=False)
EVENTS = frozenset({"PreToolUse", "PostToolUse", "SessionStart", "Stop"})


@dataclass(frozen=True)
class HookCommand:
    event: str
    executable: str
    args: tuple[str, ...] = ()
    timeout_seconds: float = 15
    output_limit: int = 16384
    tool_names: frozenset[str] | None = None


@dataclass
class HookDecision:
    allow: bool = True
    reason: str = ""
    outputs: list[dict] = field(default_factory=list)


class HookManager:
    def __init__(
        self, hooks: list[HookCommand], executor: Callable[[HookCommand, dict, ToolContext], Awaitable[dict]]
    ):
        self.hooks = tuple(hooks)
        self.executor = executor
        for hook in hooks:
            if hook.event not in EVENTS or hook.timeout_seconds <= 0 or hook.output_limit < 1:
                raise ValueError("Invalid hook configuration")

    async def run(self, event: str, payload: dict, context: ToolContext) -> HookDecision:
        if event not in EVENTS:
            raise ValueError("Unknown hook event")
        if _inside_hook.get():
            return HookDecision(allow=event != "PreToolUse", reason="Recursive hook invocation blocked")
        token = _inside_hook.set(True)
        decision = HookDecision()
        try:
            for hook in self.hooks:
                if hook.event != event or (
                    hook.tool_names is not None and payload.get("tool_name") not in hook.tool_names
                ):
                    continue
                if context.cancel.is_set() and event != "Stop":
                    return HookDecision(False, "Hook cancelled", decision.outputs)
                try:
                    result = await asyncio.wait_for(
                        self.executor(hook, payload, context), hook.timeout_seconds
                    )
                    stdout = str(result.get("stdout", ""))
                    if len(stdout.encode("utf-8")) > hook.output_limit:
                        raise ValueError("Hook output budget exceeded")
                    record = {"event": event, "returncode": result.get("returncode"), "stdout": stdout}
                    decision.outputs.append(record)
                    if result.get("returncode") != 0:
                        raise ValueError("Hook process failed")
                    answer = json.loads(stdout) if stdout.strip() else {}
                    if not isinstance(answer, dict):
                        raise TypeError("Hook output must be a JSON object")
                    if event == "PreToolUse" and answer.get("allow") is False:
                        return HookDecision(
                            False, str(answer.get("reason", "Denied by pre-tool hook")), decision.outputs
                        )
                    # allow=true is only a hook opinion; it cannot override policy.
                except (TimeoutError, ValueError, TypeError, OSError) as exc:
                    decision.outputs.append({"event": event, "error": type(exc).__name__})
                    if event == "PreToolUse":
                        return HookDecision(False, str(exc) or "Pre-tool hook timed out", decision.outputs)
            return decision
        finally:
            _inside_hook.reset(token)


def supervised_hook_executor(supervisor):
    """Adapter for ProcessSupervisor. Hooks read JSON via ASTER_HOOK_INPUT.

    The caller registers trusted HookCommand instances. Each hook process has
    its own durable execution intent; audit events alone are not a journal.
    """

    async def execute(command: HookCommand, payload: dict, context: ToolContext) -> dict:
        store = context.services.get("store")
        if store is None:
            raise ValueError("Supervised hooks require the durable Store service")
        config = {
            "event": command.event,
            "executable": command.executable,
            "args": list(command.args),
            "timeout_seconds": command.timeout_seconds,
            "output_limit": command.output_limit,
            "tool_names": sorted(command.tool_names) if command.tool_names is not None else None,
        }
        fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        parent = payload.get("tool_call_id") or context.run_id
        call_id = "hook_" + hashlib.sha256(f"{command.event}:{parent}:{fingerprint}".encode()).hexdigest()
        existing = next(
            (row for row in store.tool_executions(context.run_id) if row["tool_call_id"] == call_id), None
        )
        if existing and existing["state"] in {"committed", "checkpointed", "failed_no_effect"}:
            saved = existing["result"]
            if not saved:
                raise ValueError("Committed hook result is unavailable; reconciliation required")
            return saved.get("data", {})
        if existing and existing["state"] in {"dispatching", "outcome_unknown"}:
            raise ValueError("Hook has an unknown outcome; automatic replay is forbidden")
        intent = existing or store.prepare_tool(
            context.session_id,
            context.run_id,
            ToolCall(call_id, "hook:" + command.event, config),
            risk="execute",
            replay_safe=False,
        )
        directory = context.artifact_dir / "hooks" / call_id
        directory.mkdir(parents=True, exist_ok=True)
        request = directory / "input.json"
        request.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        env = dict(os.environ)
        env["ASTER_HOOK_INPUT"] = str(request)
        if context.cancel.is_set():
            data = {"returncode": None, "stdout": "", "stderr": "Hook cancelled before dispatch"}
            store.commit_tool(
                intent["id"], ToolResult("Hook cancelled", is_error=True, outcome="not_executed", data=data)
            )
            return data
        store.mark_dispatching(intent["id"])
        if context.emit:
            await context.emit("hook.started", {"event": command.event, "execution_id": directory.name})
        try:
            result = await supervisor.run(
                [command.executable, *command.args],
                cwd=context.cwd,
                artifact_dir=directory,
                cancel=context.cancel,
                env=env,
                timeout=command.timeout_seconds,
            )
        except BaseException:
            store.commit_tool(
                intent["id"],
                ToolResult(
                    "Hook interrupted after dispatch",
                    is_error=True,
                    outcome="unknown",
                    error_category="hook_interrupted",
                ),
            )
            raise
        data = {"returncode": result.exit_code, "stdout": result.stdout, "stderr": result.stderr}
        unknown = bool(result.cancelled or result.timed_out or (result.error and result.pid))
        store.commit_tool(
            intent["id"],
            ToolResult(
                result.stdout,
                is_error=result.exit_code != 0,
                outcome="unknown" if unknown else "known",
                artifacts=result.artifacts,
                data=data,
            ),
        )
        if context.emit:
            await context.emit(
                "hook.finished",
                {
                    "event": command.event,
                    "execution_id": directory.name,
                    "exit_code": result.exit_code,
                    "cancelled": result.cancelled,
                    "timed_out": result.timed_out,
                },
            )
        return data

    return execute
