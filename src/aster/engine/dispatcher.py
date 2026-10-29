"""Validate, authorize, journal and execute complete calls in safe batches."""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import hashlib
import json
import time

from jsonschema import Draft202012Validator

from aster.domain import ToolCall, ToolContext, ToolResult, ToolSpec, to_dict
from aster.policy import ApprovalBroker, PermissionPolicy
from aster.state import Store
from aster.state.store import dumps
from aster.tools.registry import Registry


def rejected(message: str, category: str = "validation") -> ToolResult:
    return ToolResult(message, is_error=True, error_category=category, outcome="not_executed")


@dataclasses.dataclass
class EffectiveTool:
    original: object
    spec: ToolSpec

    async def execute(self, arguments, context):
        return await self.original.execute(arguments, context)


def steering(context):
    event = context.services.get("steering")
    return event is not None and event.is_set()


class Dispatcher:
    def __init__(self, registry: Registry, store: Store, policy: PermissionPolicy,
                 approvals: ApprovalBroker | None = None, *, max_concurrency: int = 4,
                 cancellation_grace: float = 5):
        self.registry, self.store, self.policy, self.approvals = registry, store, policy, approvals
        self.max_concurrency = max(1, max_concurrency)
        self.cancellation_grace = cancellation_grace
        self._resources: dict[str, asyncio.Lock] = {}
        self.background_tasks: set[asyncio.Task] = set()

    async def _admit(self, call: ToolCall, context: ToolContext):
        if context.cancel.is_set():
            return None, rejected("Cancelled before execution", "cancelled")
        if steering(context):
            return None, rejected("New user instructions are waiting; this call was not executed", "steered")
        if self.store.has_unresolved(context.session_id):
            return None, rejected("An earlier operation needs reconciliation before further dispatch", "permission")
        tool = self.registry.get(call.name)
        if tool is None:
            return None, rejected(f"Unknown tool: {call.name}", "permission")
        if not isinstance(call.arguments, dict):
            return None, rejected("Tool arguments must be a JSON object")
        errors = sorted(Draft202012Validator(tool.spec.parameters).iter_errors(call.arguments), key=lambda e: str(e.path))
        if errors:
            return None, rejected("Invalid arguments: " + "; ".join(error.message for error in errors[:4]))
        original_tool = tool
        if hasattr(original_tool, "classify"):
            spec = tool.classify(copy.deepcopy(call.arguments))
            if not isinstance(spec, ToolSpec) or spec.name != tool.spec.name or spec.parameters != tool.spec.parameters:
                return None, rejected("Tool classifier returned an invalid execution contract", "permission")
            tool = EffectiveTool(tool, spec)
        decision = self.policy.evaluate(tool.spec, call, context)
        if decision.action == "deny":
            return None, rejected(decision.reason, "permission")
        hooks = context.services.get("hooks")
        if hooks:
            try:
                hook = await hooks.run("PreToolUse", {"tool_name": call.name, "arguments": call.arguments,
                                                     "tool_call_id": call.id}, context)
            except Exception as error:  # noqa: BLE001 - custom hooks are an untrusted extension boundary.
                return None, rejected(f"PreToolUse hook failed: {type(error).__name__}", "permission")
            if not hook.allow:
                return None, rejected(hook.reason or "PreToolUse denied the call", "permission")
        # Hook mutation of the argument dict cannot inherit the original approval.
        errors = list(Draft202012Validator(tool.spec.parameters).iter_errors(call.arguments))
        if errors:
            return None, rejected("Hook left invalid tool arguments")
        if hasattr(original_tool, "classify"):
            spec = original_tool.classify(copy.deepcopy(call.arguments))
            if not isinstance(spec, ToolSpec) or spec.name != original_tool.spec.name or spec.parameters != original_tool.spec.parameters:
                return None, rejected("Tool classifier returned an invalid post-hook execution contract", "permission")
            tool = EffectiveTool(original_tool, spec)
        decision = self.policy.evaluate(tool.spec, call, context)
        if decision.action == "deny":
            return None, rejected(decision.reason, "permission")
        approved_digest = hashlib.sha256(dumps(call.arguments).encode()).hexdigest()
        if decision.action == "ask":
            if self.approvals is None or context.services.get("unattended", False):
                return None, rejected("Approval is required but no attended approval channel is available", "permission")
            approved = await self.approvals.approve({"tool_name": call.name, "tool_call_id": call.id,
                                                   "arguments": call.arguments, "arguments_hash": approved_digest,
                                                   "reason": decision.reason}, context)
            if not approved:
                return None, rejected("Approval was denied, cancelled or expired", "permission")
        if context.cancel.is_set():
            return None, rejected("Cancelled before dispatch", "cancelled")
        if steering(context):
            return None, rejected("New user instructions arrived before dispatch", "steered")
        if hashlib.sha256(dumps(call.arguments).encode()).hexdigest() != approved_digest:
            return None, rejected("Arguments changed after approval", "permission")
        return tool, None

    async def execute(self, calls: list[ToolCall], context: ToolContext) -> list[ToolResult]:
        results = [rejected("Not executed because an earlier call stopped this batch", "cancelled") for _ in calls]
        # Keep admission in order. A side-effecting call is a barrier between read batches.
        pending: list[tuple[int, ToolCall, object]] = []
        stopped = False

        async def flush():
            nonlocal stopped
            if not pending:
                return
            if context.emit:
                await context.emit("tools.batch", {"count": len(pending), "parallel": len(pending) > 1,
                                                    "max_concurrency": self.max_concurrency,
                                                    "tool_call_ids": [call.id for _, call, _ in pending]})
            semaphore = asyncio.Semaphore(self.max_concurrency)

            async def one(item):
                nonlocal stopped
                index, call, tool = item
                async with semaphore:
                    if stopped or context.cancel.is_set() or steering(context):
                        return
                    result = await self._execute_one(tool, call, context)
                    results[index] = result
                    if result.outcome == "unknown":
                        stopped = True
            group = asyncio.gather(*(one(item) for item in pending), return_exceptions=True)
            try:
                completed = await asyncio.shield(group)
            except asyncio.CancelledError:
                context.cancel.set()
                # Collect every sibling before leaving the batch; no orphan
                # tasks may continue mutating state behind a reported failure.
                completed = await asyncio.shield(group)
            pending.clear()
            errors = [item for item in completed if isinstance(item, BaseException)]
            if errors:
                raise errors[0]

        for index, call in enumerate(calls):
            if stopped or context.cancel.is_set() or steering(context):
                break
            tool = self.registry.get(call.name)
            if tool is None or not tool.spec.concurrency_safe or context.services.get("hooks"):
                await flush()
                if stopped or context.cancel.is_set():
                    break
            admitted, denial = await self._admit(call, context)
            if denial is not None:
                results[index] = denial
                continue
            if not admitted.spec.concurrency_safe:
                await flush()
                if stopped or context.cancel.is_set() or steering(context):
                    break
            # Freeze the approved value before waiting for a resource. UI/hooks
            # may still hold references to the original argument dictionary.
            sealed_call = ToolCall(call.id, call.name, copy.deepcopy(call.arguments))
            pending.append((index, sealed_call, admitted))
            if not admitted.spec.concurrency_safe:
                await flush()
        await flush()
        return results

    async def _execute_one(self, tool, call: ToolCall, context: ToolContext) -> ToolResult:
        resource = tool.spec.resource
        if resource:
            try:
                resource = resource.format_map(call.arguments)
            except (KeyError, ValueError):
                pass
            lock = self._resources.setdefault(resource, asyncio.Lock())
            async with lock:
                return await self._run(tool, call, context)
        return await self._run(tool, call, context)

    async def _run(self, tool, call: ToolCall, context: ToolContext) -> ToolResult:
        if context.cancel.is_set():
            return rejected("Cancelled while waiting for a resource", "cancelled")
        if steering(context):
            return rejected("New user instructions arrived before dispatch", "steered")
        # Other concurrently running calls may legitimately be dispatching. Only
        # terminal unknown entries are a barrier here; admission checks pre-batch state.
        if any(item["state"] == "outcome_unknown" for item in self.store.tool_executions(context.run_id)):
            return rejected("A sibling operation has an unknown effect", "permission")
        # Re-evaluate restrictions at dispatch; no new authorization can be implied.
        if self.policy.evaluate(tool.spec, call, context).action == "deny":
            return rejected("Policy changed before dispatch", "permission")
        # Record intent before dispatch so recovery can distinguish a prepared
        # call from an action that may already have affected external state.
        entry = self.store.prepare_tool(context.session_id, context.run_id, call,
                                        risk=tool.spec.risk, replay_safe=tool.spec.replay_safe)
        if context.cancel.is_set():
            result = rejected("Cancelled before dispatch", "cancelled")
            self.store.commit_tool(entry["id"], result)
            return result
        self.store.mark_dispatching(entry["id"])
        started = time.monotonic()
        if context.emit:
            await context.emit("tool.started", {"tool_call_id": call.id, "name": call.name})
        child_cancel = asyncio.Event()
        child = dataclasses.replace(context, cancel=child_cancel)
        async def invoke():
            observer = context.services.get("read_observer")
            cached = await observer.cached(call) if observer and tool.spec.risk == "read" else None
            return cached if cached is not None else await tool.execute(call.arguments, child)

        task = asyncio.create_task(invoke(), name=f"aster-tool-{call.id}")
        parent_cancel = asyncio.create_task(context.cancel.wait())
        steer_event = context.services.get("steering")
        steer_task = (asyncio.create_task(steer_event.wait()) if steer_event is not None
                      and tool.spec.risk == "read" and tool.spec.replay_safe else None)
        timed_out = False
        try:
            done, _ = await asyncio.wait({task, parent_cancel} | ({steer_task} if steer_task else set()), timeout=tool.spec.timeout_seconds,
                                         return_when=asyncio.FIRST_COMPLETED)
            if task not in done:
                timed_out = not context.cancel.is_set() and not (steer_task and steer_task.done())
                child_cancel.set()
                try:
                    await asyncio.wait_for(asyncio.shield(task), self.cancellation_grace)
                except TimeoutError:
                    self.background_tasks.add(task)
                    task.add_done_callback(self._background_done)
                    result = ToolResult("Tool did not stop within its cancellation grace period; verify effects before continuing",
                                        True, "timeout" if timed_out else "cancelled", "unknown",
                                        data={"cleanup_pending": True})
                except asyncio.CancelledError:
                    result = self._interrupted_result(tool.spec.risk, timed_out)
                except Exception as error:  # noqa: BLE001 - all post-dispatch failures need effect classification.
                    result = self._exception_result(tool.spec.risk, error)
                else:
                    result = task.result()
            else:
                result = task.result()
            if not isinstance(result, ToolResult):
                raise TypeError("Tool returned a non-ToolResult value")
            # Validate JSON persistence before claiming a determined result.
            json.dumps(to_dict(result), allow_nan=False)
        except asyncio.CancelledError:
            child_cancel.set()
            try:
                result = await asyncio.wait_for(asyncio.shield(task), self.cancellation_grace)
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - cancellation must retain uncertain effects.
                if not task.done():
                    self.background_tasks.add(task)
                    task.add_done_callback(self._background_done)
                result = self._interrupted_result(tool.spec.risk, False)
        except Exception as error:  # noqa: BLE001 - tool implementations have arbitrary exception types.
            result = self._exception_result(tool.spec.risk, error)
        finally:
            parent_cancel.cancel()
            if steer_task:
                steer_task.cancel()
            await asyncio.gather(parent_cancel, *([steer_task] if steer_task else []), return_exceptions=True)
        self.store.commit_tool(entry["id"], result)
        if context.emit:
            await context.emit("tool.finished", {"tool_call_id": call.id, "name": call.name,
                                                 "is_error": result.is_error, "outcome": result.outcome,
                                                 "risk": tool.spec.risk, "duration_ms": round((time.monotonic() - started) * 1000, 3)})
        hooks = context.services.get("hooks")
        if hooks:
            try:
                await hooks.run("PostToolUse", {"tool_name": call.name, "tool_call_id": call.id,
                                                "result": to_dict(result)}, context)
            except Exception:  # noqa: BLE001 - optional post hooks cannot erase an already committed result.
                if context.emit:
                    await context.emit("hook.failed", {"event": "PostToolUse", "tool_call_id": call.id})
        return result

    def _background_done(self, task):
        self.background_tasks.discard(task)
        if not task.cancelled():
            task.exception()  # Consume diagnostics; never silently overwrite the unknown ledger.

    @staticmethod
    def _interrupted_result(risk: str, timed_out: bool) -> ToolResult:
        return ToolResult("Tool timed out" if timed_out else "Tool was cancelled", True,
                          "timeout" if timed_out else "cancelled", "known" if risk == "read" else "unknown")

    @staticmethod
    def _exception_result(risk: str, error: Exception) -> ToolResult:
        # Exceptions after dispatch are not proof that an external action did not occur.
        return ToolResult(f"Tool infrastructure failure: {type(error).__name__}", True, "infrastructure",
                          "known" if risk == "read" else "unknown")
