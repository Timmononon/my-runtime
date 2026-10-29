"""Explicit asynchronous model/tool loop with durable safety boundaries."""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import hashlib
import time
from pathlib import Path

from aster.domain import Message, ModelTurn, ToolContext, ToolResult, to_dict, tool_result_message
from aster.engine.compaction import CompactionController, calibration_fingerprint
from aster.engine.context import CompactionInterrupted, ContextBudgetExceeded, estimate_tokens
from aster.engine.context_usage import context_usage_basis
from aster.engine.dispatcher import Dispatcher, rejected
from aster.engine.observations import ObservationManager
from aster.policy import ApprovalBroker, PermissionPolicy, QuestionBroker
from aster.state import SessionBusy, StateError, Store
from aster.state.store import TERMINAL, BudgetExceeded, DeadlineExceeded, dumps, identifier
from aster.tools.registry import Registry


@dataclasses.dataclass
class RunConfig:
    max_iterations: int = 40
    max_tool_calls: int = 100
    max_concurrency: int = 4
    max_total_tokens: int = 300000
    max_model_requests: int = 200
    model_timeout_seconds: float = 180
    deadline_seconds: float = 1800
    model_retries: int = 2
    retry_base_seconds: float = 0.5
    duplicate_limit: int = 3
    context_tokens: int = 96000
    reserve_tokens: int = 8192
    retained_context_rounds: int = 20
    tool_output_chars: int = 50000
    cancellation_grace_seconds: float = 5
    recovery_max_attempts: int = 3
    recovery_max_age_seconds: float = 86400
    semantic_compaction: bool = True
    summary_max_chunks: int = 8
    summary_failure_limit: int = 3
    summary_backoff_iterations: int = 5
    file_restore_max_files: int = 5
    file_restore_chars: int = 100000
    file_restore_tokens: int = 50000
    file_restore_per_file_tokens: int = 5000
    browser_observation_keep: int = 3
    browser_observation_chars: int = 48000
    image_keep: int = 50
    browser_image_keep: int = 1
    tool_aggregate_chars: int = 200000
    compaction_absolute_buffer_tokens: int = 60000
    summary_input_cap_tokens: int = 150000
    compaction_snapshot_retention: int = 1
    compaction_snapshot_max_age_days: float = 14
    compaction_persist_learnings: bool = True
    compaction_reanchor: bool = True
    time_compaction_enabled: bool = False
    time_compaction_gap_seconds: float = 3600
    time_compaction_keep_recent: int = 5

    def __post_init__(self):
        for name in ("max_iterations", "max_tool_calls", "max_concurrency", "max_total_tokens", "max_model_requests", "duplicate_limit"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")
        if self.model_timeout_seconds <= 0 or self.deadline_seconds <= 0 or self.model_retries < 0:
            raise ValueError("Invalid timeout or retry configuration")
        if self.file_restore_max_files < 0 or self.file_restore_chars < 0 or self.browser_observation_keep < 1 or self.browser_observation_chars < 100:
            raise ValueError("Invalid observation budget")
        import math
        for name in ("context_tokens", "reserve_tokens", "retained_context_rounds", "summary_max_chunks",
                     "summary_failure_limit", "summary_backoff_iterations",
                     "file_restore_tokens", "file_restore_per_file_tokens", "tool_aggregate_chars",
                     "compaction_absolute_buffer_tokens", "summary_input_cap_tokens", "time_compaction_keep_recent"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.context_tokens <= self.reserve_tokens:
            raise ValueError("Context must exceed the output reserve")
        for name in ("image_keep", "browser_image_keep", "compaction_snapshot_retention"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        for name in ("compaction_snapshot_max_age_days", "time_compaction_gap_seconds"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) < 0:
                raise ValueError(f"Invalid {name}")
        for name in ("compaction_persist_learnings", "compaction_reanchor", "time_compaction_enabled"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be boolean")


@dataclasses.dataclass
class RunOutcome:
    session_id: str
    run_id: str
    status: str
    text: str = ""
    error: str | None = None
    iterations: int = 0
    tool_calls: int = 0


class SteeringInterrupt(CompactionInterrupted):
    """Replace the pending model decision without cancelling the whole run."""


class Engine:
    def __init__(self, model, registry: Registry, store: Store, *, policy: PermissionPolicy | None = None,
                 approvals: ApprovalBroker | None = None, config: RunConfig | None = None,
                 services: dict | None = None, artifact_root: str | Path | None = None):
        self.model, self.registry, self.store = model, registry, store
        self.policy = policy or PermissionPolicy()
        self.approvals = approvals
        self.config = config or RunConfig()
        self.services = dict(services or {})
        self.artifact_root = Path(artifact_root or store.path.parent / "artifacts").resolve()
        self.dispatcher = Dispatcher(registry, store, self.policy, approvals,
                                     max_concurrency=self.config.max_concurrency,
                                     cancellation_grace=self.config.cancellation_grace_seconds)
        self._active: dict[str, asyncio.Event] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._steering: dict[str, asyncio.Event] = {}

    def steer(self, run_id):
        event = self._steering.get(run_id)
        if event is None:
            return False
        event.set()
        if self.approvals:
            self.approvals.cancel_run(run_id)
        for pending in self.store.pending_requests(run_id):
            if pending["kind"] == "question":
                self.store.resolve_pending(pending["id"], None, state="cancelled")
        return True

    def cancel(self, run_id: str) -> bool:
        event = self._active.get(run_id)
        if event is None:
            return False
        event.set()
        if self.approvals:
            self.approvals.cancel_run(run_id)
        return True

    def inject(self, session_id: str, text: str, *, request_id: str | None = None) -> str:
        return self.store.queue_message(session_id, text, request_id=request_id)

    async def run(self, session_id: str, text: str, *, system: str = "", emit=None,
                  request_id: str | None = None, services: dict | None = None) -> RunOutcome:
        if not text.strip():
            raise ValueError("A user request is required")
        if self.dispatcher.background_tasks:
            raise SessionBusy("A previous tool has not finished cleanup; new execution is temporarily blocked")
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        # Coordinator may explicitly queue; core rejects overlap rather than hiding it.
        if lock.locked():
            raise SessionBusy("This session is already running")
        async with lock:
            config_snapshot = {**to_dict(self.config), "_runtime_identity": self._runtime_identity()}
            run, created = self.store.create_run(session_id, system=system, config=config_snapshot, request_id=request_id)
            if not created:
                return RunOutcome(session_id, run["id"], run["status"], run["answer"], run["error"])
            self.store.append_message(session_id, run["id"], Message("user", text))
            return await self._drive(run, emit=emit, services=services)

    async def recover(self, run_id: str, *, emit=None, services: dict | None = None,
                      allow_configuration_change: bool = False) -> RunOutcome:
        run = self.store.get_run(run_id)
        sid = run["session_id"]
        lock = self._locks.setdefault(sid, asyncio.Lock())
        if lock.locked() or run_id in self._active:
            raise SessionBusy("Cannot recover a run while its worker is alive")
        if self.dispatcher.background_tasks:
            raise StateError("Tool cleanup is still pending; recovery is unsafe")
        saved_identity = run["config"].get("_runtime_identity")
        if saved_identity and saved_identity != self._runtime_identity() and not allow_configuration_change:
            raise StateError("Provider, model or tool contracts changed; explicit configuration-change consent is required")
        async with lock:
            run = self.store.recover_run(run_id, max_attempts=self.config.recovery_max_attempts,
                                         max_age_seconds=self.config.recovery_max_age_seconds)
            if run["status"] == "needs_reconciliation":
                return RunOutcome(sid, run_id, "needs_reconciliation", error="Verify uncertain tool effects before resuming")
            # Never silently change the saved execution budget while recovering.
            saved = RunConfig(**{key: value for key, value in run["config"].items() if not key.startswith("_")})
            saved = dataclasses.replace(saved, context_tokens=min(saved.context_tokens, self.config.context_tokens),
                                        reserve_tokens=max(saved.reserve_tokens, self.config.reserve_tokens))
            return await self._drive(run, emit=emit, services=services, config=saved)

    async def execute_queued(self, run_id: str, text: str, *, emit=None, services: dict | None = None,
                             user_message: Message | None = None) -> RunOutcome:
        run = self.store.get_run(run_id)
        if run["status"] != "queued":
            raise StateError("Only a queued run can be started by its coordinator")
        sid = run["session_id"]
        lock = self._locks.setdefault(sid, asyncio.Lock())
        if lock.locked():
            raise SessionBusy("Session already has an active engine")
        if self.store.has_unresolved(sid):
            raise StateError("Session has unresolved external effects")
        async with lock:
            self.store.set_run_status(run_id, "running")
            self.store.append_message(sid, run_id, user_message or Message("user", text))
            config = RunConfig(**{key: value for key, value in run["config"].items() if not key.startswith("_")})
            return await self._drive(self.store.get_run(run_id), emit=emit, services=services, config=config)

    def _runtime_identity(self):
        identity = {"provider": type(self.model).__name__, "model": str(getattr(self.model, "model", "")),
                    "tools_fingerprint": hashlib.sha256(dumps(self.registry.specs()).encode()).hexdigest()}
        if getattr(self.model, "identity", None) is not None:
            identity["provider_identity"] = self.model.identity
        return identity

    async def _drive(self, run: dict, *, emit=None, services=None, config=None) -> RunOutcome:
        config = config or self.config
        sid, rid = run["session_id"], run["id"]
        session = self.store.get_session(sid)
        cancel = self._active[rid] = asyncio.Event()
        artifact_dir = self.artifact_root / sid / rid
        artifact_dir.mkdir(parents=True, exist_ok=True)
        tool_services = {**self.services, **(services or {})}
        tool_services.setdefault("store", self.store)
        tool_services.setdefault("registry", self.registry)
        tool_services.setdefault("agent_id", session["agent_id"])
        tool_services.setdefault("project_id", session["project_id"])
        tool_services.setdefault("questions", QuestionBroker(self.store))
        from functools import partial

        from aster.service.artifact_access import read_registered_artifact
        tool_services.setdefault("artifact_read_resolver", partial(read_registered_artifact, self.store, self.store.path.parent))
        steer = self._steering[rid] = asyncio.Event()
        tool_services["steering"] = steer
        current_phase = None

        async def event(kind: str, payload: dict):
            nonlocal current_phase
            phase = {"run.started": "setup", "model.request": "awaiting_model", "model.retry": "retrying_model",
                     "tool.started": "executing_tools", "approval.waiting": "awaiting_approval",
                     "question.waiting": "awaiting_input", "context.compacting": "compacting",
                     "run.finishing": "done"}.get(kind)
            if phase and phase != current_phase:
                current_phase = phase
                self.store.emit_event(sid, rid, "run.phase", {"phase": phase})
            stored = self.store.emit_event(sid, rid, kind, payload)
            if emit:
                try:
                    await emit(kind, {**payload, "event_id": stored["event_id"], "session_id": sid, "run_id": rid})
                except Exception as error:  # noqa: BLE001 - UI callbacks must not cancel tool side effects.
                    # A disconnected UI must not cancel an already-dispatched action.
                    self.store.emit_event(sid, rid, "sink.failed", {"error_type": type(error).__name__})

        context = ToolContext(sid, rid, Path(session["cwd"]), artifact_dir, cancel, event, tool_services)
        observer = ObservationManager(self.store, context, restore_files=config.file_restore_max_files,
                                      restore_chars=config.file_restore_chars, restore_tokens=config.file_restore_tokens,
                                      restore_per_file_tokens=config.file_restore_per_file_tokens,
                                      browser_keep=config.browser_observation_keep, image_keep=config.image_keep,
                                      browser_image_keep=config.browser_image_keep, browser_chars=config.browser_observation_chars)
        tool_services["read_observer"] = observer
        fingerprint = calibration_fingerprint(self._runtime_identity(), self.registry.specs(), run["system"])
        prior_checkpoint = self.store.get_checkpoint(sid)

        def checkpoint(messages, metadata=None):
            return self.store.checkpoint(sid, rid, messages, metadata={**(metadata or {}),
                "overhead_tokens": compaction.manager.overhead_tokens, "calibration_fingerprint": fingerprint,
                "context_usage": context_usage_basis(run["system"], self._offered_specs(context), config,
                    session_id=sid, model=str(getattr(self.model, "model", "")),
                    overhead_tokens=compaction.manager.overhead_tokens),
                **compaction.state()})
        outcome = RunOutcome(sid, rid, "running")
        budget = self.store.start_budget(rid, config.deadline_seconds)
        outcome.iterations, outcome.tool_calls = budget["iterations"], budget["tool_calls"]
        remaining_seconds = max(0, budget["deadline_at"] - time.time())
        deadline = time.monotonic() + remaining_seconds
        deadline_expired = asyncio.Event()

        async def deadline_watch():
            await asyncio.sleep(remaining_seconds)
            deadline_expired.set()
            cancel.set()

        watchdog = asyncio.create_task(deadline_watch())

        async def helper(source, summary_system, purpose):
            nonlocal token_total
            token_total = sum((item.get("input_tokens") or 0) + (item.get("output_tokens") or 0)
                              for item in self.store.usage(rid))
            if token_total + estimate_tokens(source, [], summary_system) >= config.max_total_tokens:
                raise ContextBudgetExceeded("No token budget remains for compaction helpers")

            async def summary_event(kind, payload):
                # Helper analysis/source text is not a user-facing event or diagnostic.
                await event("context.summary.progress", {"provider_event": kind, "purpose": purpose})

            turn = await self._generate(source, summary_system, context, config, deadline,
                                        tools_override=[], emit_override=summary_event, purpose=purpose)
            turn.usage.raw = {**turn.usage.raw, "purpose": purpose}
            token_total += (turn.usage.input_tokens or 0) + (turn.usage.output_tokens or 0)
            await event("context.summary.usage", to_dict(turn.usage))
            return turn

        prior_meta = prior_checkpoint["metadata"] if prior_checkpoint else {}
        compaction = CompactionController(self.store, context, config, observer, system=run["system"],
            tools=lambda: self._offered_specs(context), helper=helper, emit=event, checkpoint=prior_meta)
        if prior_meta.get("calibration_fingerprint") == fingerprint:
            overhead = prior_meta.get("overhead_tokens", 0)
            if type(overhead) is int and 0 <= overhead <= config.context_tokens:
                compaction.manager.overhead_tokens = overhead
            previous_usage = prior_meta.get("last_prompt_total")
            if type(previous_usage) is int and 0 <= previous_usage <= config.context_tokens * 2:
                compaction.real_tokens = previous_usage
        try:
            await event("run.started", {"recovered": run["resume_attempts"] > 0})
            hooks = tool_services.get("hooks")
            if hooks:
                await hooks.run("SessionStart", {"session_id": sid, "run_id": rid}, context)
            messages = [self._budget_tool_message(message, context, config.tool_output_chars)
                        if message.role == "tool" else message for message in self.store.active_history(sid)]
            tool_count = budget["tool_calls"]
            token_total = sum((item.get("input_tokens") or 0) + (item.get("output_tokens") or 0)
                              for item in self.store.usage(rid))

            repeats: dict[str, tuple[str, int]] = {}
            reactive_used = compaction.reactive_used
            defer_compaction_helpers = False
            for _ in range(config.max_iterations - budget["iterations"]):
                # A cancelled/steered response can still have measured usage
                # committed by _generate. The durable lower bound, rather than
                # only successfully returned ModelTurns, owns run admission.
                token_total = sum((item.get("input_tokens") or 0) + (item.get("output_tokens") or 0)
                                  for item in self.store.usage(rid))
                if self.store.has_unresolved(sid):
                    outcome.status, outcome.error = "needs_reconciliation", "A prior tool or hook action needs verification"
                    break
                if cancel.is_set():
                    outcome.status = "timed_out" if deadline_expired.is_set() else "cancelled"
                    break
                outcome.iterations = self.store.reserve_iteration(rid, config.max_iterations)
                if token_total >= config.max_total_tokens:
                    outcome.status, outcome.error = "budget_exhausted", "Configured token budget exhausted"
                    break
                injected = self.store.drain_messages(sid, rid)
                compaction.protect(injected)
                messages.extend(injected)
                steer.clear()
                try:
                    shaped = await compaction.prepare(messages, allow_helpers=not defer_compaction_helpers)
                    defer_compaction_helpers = False
                except SteeringInterrupt:
                    defer_compaction_helpers = True
                    await event("run.steered", {"phase": "compaction"})
                    continue
                if shaped is not messages:
                    messages = shaped
                    checkpoint(messages, {"compacted": True})
                if token_total >= config.max_total_tokens:
                    outcome.status, outcome.error = "budget_exhausted", "Configured token budget exhausted during compaction"
                    break
                try:
                    observer.observe_visible(messages)
                    turn = await self._generate(messages, run["system"], context, config, deadline)
                except SteeringInterrupt:
                    await event("run.steered", {"phase": "model"})
                    continue
                except Exception as error:
                    category = getattr(error, "category", "")
                    if category in ("context_length", "context_length_exceeded", "context_overflow") and not reactive_used:
                        reactive_used = True
                        try:
                            shaped = await compaction.prepare(messages, force=True, phase="reactive")
                        except SteeringInterrupt:
                            defer_compaction_helpers = True
                            continue
                        if shaped is messages:
                            raise ContextBudgetExceeded("Provider rejected context and no complete old round can be removed") from error
                        messages = shaped
                        checkpoint(messages, {"reactive_compaction": True})
                        try:
                            turn = await self._generate(messages, run["system"], context, config, deadline)
                        except SteeringInterrupt:
                            continue
                    else:
                        raise
                compaction.observe_usage(messages, turn.usage)
                token_total += (turn.usage.input_tokens or 0) + (turn.usage.output_tokens or 0)
                await event("model.usage", to_dict(turn.usage))
                if cancel.is_set():
                    outcome.status = "timed_out" if deadline_expired.is_set() else "cancelled"
                    break
                if turn.finish_reason not in ("stop", "completed", "tool_calls"):
                    outcome.status, outcome.error = "failed", f"Incomplete model response: {turn.finish_reason}"
                    await event("model.incomplete", {"finish_reason": turn.finish_reason})
                    break
                ids = [call.id for call in turn.tool_calls]
                old_ids = self.store.tool_call_ids(rid) | {call.id for message in messages for call in message.tool_calls}
                old_ids.update(item["call_id"] for message in messages for item in message.provider_items
                               if item.get("type") == "function_call" and item.get("call_id"))
                if any(not cid for cid in ids) or len(set(ids)) != len(ids) or old_ids.intersection(ids):
                    raise StateError("Model returned missing, repeated or reused tool call IDs")
                assistant = Message("assistant", turn.text, tool_calls=copy.deepcopy(turn.tool_calls),
                                    provider_items=copy.deepcopy(turn.provider_items))
                self.store.append_message(sid, rid, assistant)
                messages.append(assistant)
                if not turn.tool_calls:
                    # A follow-up queued during generation keeps this run alive.
                    injected = self.store.drain_messages(sid, rid)
                    if injected:
                        compaction.protect(injected)
                        messages.extend(injected)
                        checkpoint(messages)
                        continue
                    outcome.status, outcome.text = "completed", turn.text
                    checkpoint(messages)
                    break
                if tool_count + len(turn.tool_calls) > config.max_tool_calls:
                    results = [rejected("Tool-call budget exhausted", "budget") for _ in turn.tool_calls]
                    outcome.status, outcome.error = "budget_exhausted", "Configured tool-call budget exhausted"
                elif any(repeats.get(self._signature(call), ("", 0))[1] >= config.duplicate_limit for call in turn.tool_calls):
                    results = [rejected("Repeated identical tool requests made no progress", "loop_detected") for _ in turn.tool_calls]
                    outcome.status, outcome.error = "budget_exhausted", "Repeated no-progress tool calls"
                else:
                    try:
                        tool_count = self.store.reserve_tool_calls(rid, len(turn.tool_calls), config.max_tool_calls)
                        results = await self.dispatcher.execute(turn.tool_calls, context)
                    except BudgetExceeded as error:
                        results = [rejected(str(error), "budget") for _ in turn.tool_calls]
                        outcome.status = "timed_out" if isinstance(error, DeadlineExceeded) else "budget_exhausted"
                        outcome.error = str(error)
                    except Exception:  # noqa: BLE001 - reconcile every sibling result after dispatch failure.
                        # Preserve every known result even if a later dispatch/storage boundary fails.
                        results = self._results_after_failure(rid, turn.tool_calls)
                        outcome.status, outcome.error = "failed", "Tool dispatch failed; inspect the execution ledger"
                    outcome.tool_calls = tool_count
                for call, result in zip(turn.tool_calls, results, strict=True):
                    signature = self._signature(call)
                    digest = hashlib.sha256(dumps(result).encode()).hexdigest()
                    previous = repeats.get(signature)
                    repeats[signature] = (digest, previous[1] + 1 if previous and previous[0] == digest else 1)
                    raw_message = self._tool_message(call.id, result, rid, tool_name=call.name)
                    from aster.tools.files import FileTool
                    if isinstance(self.registry.get(call.name), FileTool) and call.name in {"file_read", "text_search"}:
                        raw_message.metadata["tool_budget_policy"] = call.name
                    if observation := observer.record(call, result):
                        raw_message.metadata["file_observation"] = observation
                    projected = self._budget_tool_message(raw_message, context, config.tool_output_chars)
                    if "output_artifact" in projected.metadata:
                        raw_message.metadata["output_artifact"] = projected.metadata["output_artifact"]
                    self.store.append_message(sid, rid, raw_message)
                    messages.append(projected)
                # Auxiliary native searches reserve their own model receipts.
                # Recount the known lower bound once; missing usage stays NULL in Store.
                token_total = sum((item.get("input_tokens") or 0) + (item.get("output_tokens") or 0)
                                  for item in self.store.usage(rid))
                checkpoint(messages)
                if any(result.outcome == "unknown" for result in results):
                    outcome.status, outcome.error = "needs_reconciliation", "A tool action has an uncertain outcome; verify effects before continuing"
                    break
                if outcome.status in TERMINAL:
                    break
            else:
                outcome.status, outcome.error = "budget_exhausted", "Configured model-iteration limit reached"
            if outcome.status == "running":
                outcome.status = "timed_out" if deadline_expired.is_set() else "cancelled"
        except asyncio.CancelledError:
            cancel.set()
            outcome.status = "timed_out" if deadline_expired.is_set() else "cancelled"
        except DeadlineExceeded as error:
            outcome.status, outcome.error = "timed_out", str(error)
        except (ContextBudgetExceeded, BudgetExceeded) as error:
            outcome.status, outcome.error = "budget_exhausted", str(error)
        except TimeoutError:
            outcome.status, outcome.error = "timed_out", "Model or overall task deadline expired"
        except Exception as error:  # noqa: BLE001 - persist a terminal state for any provider/tool boundary failure.
            outcome.status, outcome.error = "failed", f"{type(error).__name__}: {error}"
        finally:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
            if self.approvals:
                self.approvals.cancel_run(rid)
            questions = tool_services.get("questions")
            if questions:
                questions.cancel_run(rid)
        if outcome.status == "budget_exhausted" and not cancel.is_set() and "messages" in locals():
            # Preserve a compact resumable state on force-stop without spending
            # another model request after a run budget has been exhausted.
            try:
                if compaction.manager.should_preflight(messages, self._offered_specs(context), run["system"]):
                    shaped = await compaction.prepare(messages, force=True, phase="force_stop", semantic=False)
                    if shaped is not messages:
                        messages = shaped
                        checkpoint(messages, {"force_stop_compaction": True})
            except asyncio.CancelledError:
                cancel.set()
                outcome.status = "timed_out" if deadline_expired.is_set() else "cancelled"
            except SteeringInterrupt:
                # Preserve queued user input for the next run; the exhausted
                # run must still finish its durable lifecycle and cleanup.
                pass
            except Exception as error:  # noqa: BLE001 - terminal budget result remains authoritative.
                await event("context.compaction_failed", {"phase": "force_stop", "category": type(error).__name__})
        if tool_services.get("hooks"):
            try:
                await tool_services["hooks"].run("Stop", to_dict(outcome), context)
            except Exception:  # noqa: BLE001 - extension failures are diagnostics, but their ledger remains authoritative.
                await event("hook.failed", {"event": "Stop"})
        # If cancellation raced with an external write, unknown wins over cancelled.
        if any(item["state"] in ("dispatching", "outcome_unknown") and item["risk"] != "read"
               for item in self.store.tool_executions(rid)):
            outcome.status, outcome.error = "needs_reconciliation", "A dispatched action needs verification"
        await event("run.finishing", {"status": outcome.status})
        finished = self.store.finish_run(rid, outcome.status, answer=outcome.text, error=outcome.error,
                                         payload=to_dict(outcome))
        if emit:
            try:
                await emit("run.finished", {**finished["payload"], "event_id": finished["event_id"]})
            except Exception as error:  # noqa: BLE001 - the durable completion survives a disconnected sink.
                self.store.emit_event(sid, rid, "sink.failed", {"error_type": type(error).__name__})
        self._active.pop(rid, None)
        self._steering.pop(rid, None)
        return outcome

    async def _generate(self, messages, system, context, config, deadline, *, tools_override=None, emit_override=None,
                        purpose=None) -> ModelTurn:
        purpose = purpose or ("compaction" if tools_override == [] else "main")
        for attempt in range(config.model_retries + 1):
            if context.cancel.is_set():
                raise asyncio.CancelledError
            steer = context.services.get("steering")
            if steer and steer.is_set():
                raise SteeringInterrupt
            receipt = self.store.begin_model_request(context.run_id, limit=config.max_model_requests,
                                                      purpose=purpose)
            offered_tools = self._offered_specs(context) if tools_override is None else tools_override
            began, first_progress, output_received = time.monotonic(), False, False
            await context.emit("model.request", {"request_id": receipt, "model": getattr(self.model, "model", type(self.model).__name__),
                                                 "purpose": purpose})

            async def model_emit(kind, payload, receipt=receipt, began=began):
                nonlocal first_progress, output_received
                if kind in {"model.text.delta", "model.reasoning.delta", "model.tool.delta"}:
                    output_received = True
                if not first_progress:
                    first_progress = True
                    await context.emit("model.first_progress", {"request_id": receipt, "latency_ms": round((time.monotonic() - began) * 1000, 3)})
                await (emit_override or context.emit)(kind, payload)

            task = asyncio.create_task(self.model.generate(copy.deepcopy(messages), offered_tools, system=system,
                                                           emit=model_emit, cancel=context.cancel))
            cancelled = asyncio.create_task(context.cancel.wait())
            steered = asyncio.create_task(steer.wait()) if steer else None
            request_outcome, category, measured = "unknown", None, None
            try:
                done, _ = await asyncio.wait({task, cancelled} | ({steered} if steered else set()), timeout=max(0, min(config.model_timeout_seconds,
                                                                                deadline - time.monotonic())),
                                             return_when=asyncio.FIRST_COMPLETED)
                if task not in done:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    if context.cancel.is_set():
                        raise asyncio.CancelledError
                    if steer and steer.is_set():
                        raise SteeringInterrupt
                    raise TimeoutError("Model request timed out")
                turn = task.result()
                if not isinstance(turn, ModelTurn):
                    raise TypeError("Provider returned a non-ModelTurn value")
                # Usage belongs to the completed provider request even when a
                # newer user direction wins before its answer can be applied.
                self.store.finish_model_request(receipt, turn.usage)
                measured = turn.usage
                if steer and steer.is_set():
                    raise SteeringInterrupt
                request_outcome = "completed"
                return turn
            except asyncio.CancelledError:
                request_outcome, category = "cancelled", "cancelled"
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            except Exception as error:
                request_outcome = "interrupted" if isinstance(error, SteeringInterrupt) else "failed"
                category = "steering" if isinstance(error, SteeringInterrupt) else "timeout" if isinstance(error, TimeoutError) else getattr(error, "category", type(error).__name__)
                if output_received:
                    # Do not turn a failed streamed answer into an automatic new
                    # answer, including the reactive-compaction path in _drive.
                    if category in {"context_length", "context_length_exceeded", "context_overflow"}:
                        error.category = "incomplete"
                    raise
                if not getattr(error, "retryable", False) or attempt >= config.model_retries:
                    raise
                await context.emit("model.retry", {"attempt": attempt + 1, "category": getattr(error, "category", "unknown")})
                try:
                    delay = getattr(error, "retry_after", None)
                    if delay is None:
                        delay = min(30, config.retry_base_seconds * 2 ** attempt)
                    await asyncio.wait_for(context.cancel.wait(), min(60, max(0, delay)))
                    raise asyncio.CancelledError
                except TimeoutError:
                    pass
            finally:
                cancelled.cancel()
                if steered:
                    steered.cancel()
                await asyncio.gather(cancelled, *([steered] if steered else []), return_exceptions=True)
                await context.emit("model.finished", {"request_id": receipt, "duration_ms": round((time.monotonic() - began) * 1000, 3),
                    "outcome": request_outcome, "category": category, "input_tokens": measured.input_tokens if measured else None,
                    "output_tokens": measured.output_tokens if measured else None})
        raise AssertionError("Retry loop fell through")

    def _offered_specs(self, context):
        active = context.services.get("active_tools")
        return [spec for spec in self.registry.specs() if active is None or spec.name in active]

    def _results_after_failure(self, run_id, calls):
        entries = {entry["tool_call_id"]: entry for entry in self.store.tool_executions(run_id)}
        results = []
        for call in calls:
            entry = entries.get(call.id)
            if entry and entry["result"]:
                result = ToolResult(**entry["result"])
            elif entry and entry["state"] == "dispatching":
                result = ToolResult("Dispatch interrupted before result was durably saved", True, "infrastructure",
                                    "known" if entry["risk"] == "read" else "unknown")
                self.store.commit_tool(entry["id"], result)
            else:
                result = rejected("Not executed because dispatch failed", "infrastructure")
                if entry and entry["state"] == "prepared":
                    self.store.commit_tool(entry["id"], result)
            results.append(result)
        return results

    @staticmethod
    def _signature(call) -> str:
        return call.name + "\0" + dumps(call.arguments)

    @staticmethod
    def _tool_message(call_id, result, run_id, *, tool_name=None):
        message = tool_result_message(call_id, result, run_id)
        if tool_name:
            message.metadata["tool_name"] = tool_name
        return message

    @staticmethod
    def _budget_tool_message(message: Message, context, limit):
        projected = copy.deepcopy(message)
        if not projected.metadata.get("tool_content_version"):
            # Read old archives without rewriting them in place.
            metadata = projected.metadata
            projected = tool_result_message(projected.tool_call_id, ToolResult(projected.content,
                is_error=metadata.get("is_error", False), outcome=metadata.get("outcome", "known"),
                error_category=metadata.get("error_category"),
                artifacts=metadata.get("artifacts", []), data={**metadata.get("data", {}),
                "content_parts": metadata.get("content_parts", [])}), metadata.get("run_id", context.run_id), project=True)
            projected.metadata = {**metadata, **projected.metadata}
        # This policy is assigned by the dispatcher from its registered local
        # FileTool, never from ToolResult.data or a remote tool's claimed name.
        policy = projected.metadata.get("tool_budget_policy")
        file_read = policy == "file_read" or (policy is None and "tool_name" not in projected.metadata
                                               and projected.metadata.get("file_observation"))
        if policy == "text_search":
            limit = min(limit, 20_000)
        original = projected.content
        if not file_read and len(original) > limit:
            artifact = projected.metadata.get("output_artifact")
            local_search_spill = (policy == "text_search" and
                                  projected.metadata.get("data", {}).get("truncated") and
                                  projected.metadata.get("artifacts"))
            if local_search_spill:
                source_artifact = projected.metadata["artifacts"][0]
                if not artifact or artifact.get("artifact_id") == source_artifact.get("artifact_id"):
                    artifact = source_artifact
                else:
                    local_search_spill = False
            path = None
            if isinstance(artifact, dict) and artifact.get("artifact_id") and artifact.get("path"):
                try:
                    resolver = context.services.get("artifact_read_resolver")
                    candidate = Path(artifact["path"]).resolve()
                    owned_new_spill = local_search_spill and candidate.is_relative_to(context.artifact_dir.resolve())
                    path = Path(resolver(context.session_id, artifact["path"])
                                if resolver and not owned_new_spill else candidate)
                    if not path.is_file():
                        path = None
                    elif local_search_spill:
                        # Keep the structured metadata prefix alongside the
                        # original search JSON; the source file itself remains
                        # the one produced by the trusted local tool.
                        body = projected.metadata.get("source_content", "")
                        prefix = original[:-len(body)] if body and original.endswith(body) else ""
                        original = prefix + path.read_text(encoding="utf-8")
                    elif projected.metadata.get("tool_budget_version"):
                        # A tighter policy on a saved projection must re-slice
                        # its full registered source, not spill the old excerpt.
                        original = path.read_text(encoding="utf-8")
                except (OSError, ValueError, PermissionError):
                    path = None
            if path is None:
                if projected.metadata.get("tool_budget_version") and artifact:
                    # The archive still owns the old reference. Missing evidence
                    # must not be replaced with a newly mislabeled partial file.
                    path = Path(artifact["path"])
                else:
                    from aster.platform.filesystem import atomic_write
                    aid = identifier("output")
                    path = context.artifact_dir / f"{aid}.txt"
                    atomic_write(path, original.encode("utf-8"))
                    artifact = {"artifact_id": aid, "path": str(path), "mime_type": "text/plain",
                                "size": path.stat().st_size, "sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
                                "session_id": context.session_id, "run_id": projected.metadata.get("run_id", context.run_id)}
                    if store := context.services.get("store"):
                        store.emit_event(context.session_id, context.run_id, "artifact.registered", {"artifacts": [artifact]})
            notice = f"\n[Output truncated. Full UTF-8 output: {path}]\n"
            if len(notice) >= limit:
                notice = f"\n[Output truncated; full source artifact={artifact['artifact_id']}]\n"
            available = max(0, limit - len(notice))
            head, tail = (available + 1) // 2, available // 2
            projected.content = original[:head] + notice + (original[-tail:] if tail else "")
            projected.metadata["output_artifact"] = artifact
            projected.metadata["tool_budget_version"] = 1
            projected.metadata["tool_budget_chars"] = limit
        nontext = [part for part in projected.metadata.get("content_parts", []) if part.get("type") != "input_text"]
        projected.metadata["content_parts"] = ([{"type": "input_text", "text": projected.content}, *nontext]
                                               if nontext else [])
        # Full original data remains in the transcript/ledger, not in the model
        # context or token estimator a second time.
        projected.metadata.pop("data", None)
        projected.metadata.pop("source_content", None)
        return projected
