"""Offline BFCL V4 execution through Aster's real Coordinator and Dispatcher.

Only explicit local scripted predictions are accepted. Passing a provider or
model factory is deliberately impossible; protocol/scorer validation never
becomes a model capability result.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import re
import tempfile
import time
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import yaml

from aster.domain import ModelTurn, ToolCall, ToolResult, to_dict
from aster.evaluation.bfcl_v4_tools import BFCLExecutionUnsupported, create_environment, parse_call
from aster.platform.filesystem import atomic_write
from aster.service.coordinator import RunCoordinator


@dataclass(frozen=True)
class OfflineLimits:
    max_requests: int = 160
    max_tool_calls: int = 500
    max_steps_per_turn: int = 20
    deadline_seconds: float = 120

    def __post_init__(self):
        for name in ("max_requests", "max_tool_calls", "max_steps_per_turn"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError("Offline limits must be explicit positive integers")
        if self.max_steps_per_turn > 20:
            raise ValueError("BFCL permits at most 20 model steps per user turn")
        if (isinstance(self.deadline_seconds, bool) or not isinstance(self.deadline_seconds, (int, float))
                or not math.isfinite(self.deadline_seconds) or self.deadline_seconds <= 0):
            raise ValueError("A positive finite case deadline is required")


def _safe(value, depth=0):
    if depth > 40:
        return "<depth limit>"
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, dict):
        return {str(key): _safe(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item, depth + 1) for item in value]
    return repr(value)


def _write(path, value):
    atomic_write(Path(path), json.dumps(_safe(value), ensure_ascii=False, indent=2, allow_nan=False).encode())


class OfflineCaseBudget:
    """A single durable budget shared by all turns and memory entries of a case."""

    def __init__(self, limits, *, path=None, cancel=None):
        if not isinstance(limits, OfflineLimits):
            raise TypeError("OfflineLimits is required")
        self.limits, self.path, self.cancel = limits, path, cancel
        if path is not None and Path(path).exists():
            raise ValueError("An existing offline case budget cannot be rebound or replayed")
        self.started = time.monotonic()
        self.counts = {"requests": 0, "tool_calls": 0}
        self.receipts, self.stopped = [], None
        self._persist()

    def _persist(self):
        if self.path is not None:
            _write(self.path, self.snapshot())

    def check(self):
        if self.cancel is not None and self.cancel.is_set():
            self.stopped = "cancelled"
        if time.monotonic() - self.started >= self.limits.deadline_seconds:
            self.stopped = "case_deadline_exhausted"
        if self.stopped:
            self._persist()
        return self.stopped is None

    def reserve(self, kind):
        if not self.check():
            return False
        maximum = self.limits.max_requests if kind == "requests" else self.limits.max_tool_calls
        if self.counts[kind] >= maximum:
            self.stopped = "case_" + kind + "_exhausted"
            self._persist()
            return False
        self.counts[kind] += 1
        self.receipts.append({"kind": kind, "index": self.counts[kind], "reserved_before_dispatch": True})
        self._persist()
        return True

    def snapshot(self):
        return {"limits": to_dict(self.limits), "counts": dict(self.counts), "receipts": self.receipts,
                "stop_reason": self.stopped, "model_api_calls": 0}


class _NoCredentials:
    def get(self, *args, **kwargs):
        return None

    def resolve(self, _reference):
        raise RuntimeError("Offline BFCL execution cannot resolve credentials")


class _ScriptedModel:
    model = "bfcl-v4-offline-script"

    def __init__(self, steps_by_turn, environment, budget):
        self.identity = {"kind": "local-scripted-protocol-validation", "model_api_calls": 0}
        self.steps, self.environment, self.budget = steps_by_turn, environment, budget
        self.turn, self.cursor, self.observations = 0, 0, []

    def select_turn(self, index):
        self.turn, self.cursor = index, 0

    async def generate(self, messages, tools, *, system, emit=None, cancel=None):
        if cancel is not None and cancel.is_set():
            raise asyncio.CancelledError
        if not self.budget.reserve("requests"):
            raise RuntimeError("Offline case request budget exhausted")
        self.observations.append({"turn_index": self.turn, "step_index": self.cursor,
                                  "messages": to_dict(messages), "offered_tools": [spec.name for spec in tools],
                                  "system": system})
        sequence = self.steps[self.turn]
        step = sequence[self.cursor] if self.cursor < len(sequence) else {"text": "Offline scripted turn complete."}
        self.cursor += 1
        delay = step.get("delay_seconds", 0)
        if delay:
            await asyncio.sleep(delay)
        reverse = {name: alias for alias, name in self.environment.original_names.items()}
        calls = [ToolCall(f"offline-{self.turn}-{self.cursor}-{index}", reverse.get(call["name"], call["name"]),
                          copy.deepcopy(call["arguments"])) for index, call in enumerate(step.get("calls", []))]
        return ModelTurn(text=step.get("text", ""), tool_calls=calls)


class _SimulatorTool:
    def __init__(self, spec, environment, budget):
        self.spec, self.environment, self.budget = spec, environment, budget

    async def execute(self, arguments, context):
        if context.cancel.is_set():
            return ToolResult("Cancelled before simulator execution", is_error=True, outcome="not_executed",
                              error_category="cancelled")
        if not self.budget.reserve("tool_calls"):
            return ToolResult("Offline case tool/deadline budget exhausted", is_error=True,
                              outcome="not_executed", error_category="budget")
        content = self.environment.execute(self.spec.name, arguments)
        return ToolResult(content, data={"bfcl_simulator": True, "external_action": False})


class _OfflineCoordinator(RunCoordinator):
    def __init__(self, data, *, environment, driver, budget):
        self.environment, self.offered = environment, set()
        tools = [_SimulatorTool(spec, environment, budget) for spec in environment.specs]
        super().__init__(data, tools=tools, model_factory=lambda _settings: driver,
                         credentials=_NoCredentials(), browser_enabled=False, max_parallel_runs=1)

    async def _build_engine(self, run, payload, **kwargs):
        engine, services, resources = await super()._build_engine(run, payload, **kwargs)
        names = {spec.name for spec in self.environment.specs}
        services["active_tools"] = set(self.offered)
        engine.services["active_tools"] = set(self.offered)
        engine.policy.denied_tools.update(names - self.offered)
        return engine, services, resources


def _scripts(value, turn_count):
    if not isinstance(value, list) or len(value) != turn_count:
        raise ValueError("An explicit script list is required for every user turn")
    copied = copy.deepcopy(value)
    if sum(len(turn) for turn in copied if isinstance(turn, list)) > 4000:
        raise ValueError("Offline script exceeds its bounded step count")
    for turn in copied:
        if not isinstance(turn, list):
            raise TypeError("Offline user-turn predictions must be step lists")
        for step in turn:
            if (not isinstance(step, dict) or set(step) - {"calls", "text", "delay_seconds"}
                    or not isinstance(step.get("text", ""), str) or not isinstance(step.get("calls", []), list)):
                raise ValueError("Invalid offline prediction step")
            delay = step.get("delay_seconds", 0)
            if (isinstance(delay, bool) or not isinstance(delay, (float, int))
                    or not math.isfinite(delay) or not 0 <= delay <= .5):
                raise ValueError("Offline step delays must be bounded local waits")
            for call in step.get("calls", []):
                if (not isinstance(call, dict) or set(call) != {"name", "arguments"}
                        or not isinstance(call["name"], str) or not isinstance(call["arguments"], dict)):
                    raise ValueError("Invalid offline structured call")
                json.dumps(call["arguments"], allow_nan=False)
    return copied


def _expected(case):
    expected = case.get("expected")
    if (not isinstance(expected, list) or len(expected) != len(case["question"])
            or any(not isinstance(turn, list) or any(not isinstance(call, str) for call in turn) for turn in expected)):
        raise ValueError("Multi-turn references must provide a call list for every user turn")
    return expected


def _turn_check(model, reference, expected, decoded, index):
    from aster.evaluation.bfcl_v4_official import check_irrelevant_turn, check_multiturn_turn

    results = reference.call_expressions(expected)
    irrelevance = check_irrelevant_turn(decoded, expected)
    if not irrelevance["valid"]:
        return irrelevance
    if not expected:
        return irrelevance
    if not decoded or all(not step for step in decoded):
        return {"valid": False, "error_type": "multi_turn:empty_turn_model_response"}
    return check_multiturn_turn(model.instances, reference.instances, model.results, results, index)


def replay_multiturn(case, predictions_by_turn, *, root=None, limits=None, forced_terminated=False):
    """Grade saved call expressions with isolated state and the pinned checker.

    Predictions are [user turn][model step][call expression]. No model runs.
    """
    limits = limits or OfflineLimits()
    from aster.evaluation.bfcl_v4_data import public_turn
    from aster.evaluation.bfcl_v4_official import check_multiturn_completion

    expected = _expected(case)
    if not isinstance(forced_terminated, bool):
        raise TypeError("A saved forced-termination flag must be boolean")
    if not isinstance(predictions_by_turn, list) or any(not isinstance(turn, list) for turn in predictions_by_turn):
        raise ValueError("Predictions must be an explicit list of user-turn step lists")
    completion = check_multiturn_completion(predictions_by_turn, expected, forced_terminated=forced_terminated)
    if not completion["valid"]:
        return {**completion, "id": case["id"], "turns": [completion], "forced_termination": True,
                "model_quality": "unsupported", "model_api_calls": 0,
                "validation_kind": "saved_prediction_replay", "budget": OfflineCaseBudget(limits).snapshot()}
    turn_results = []
    budget = OfflineCaseBudget(limits)
    with tempfile.TemporaryDirectory(prefix="aster-bfcl-v4-replay-", dir=root) as temporary:
        model = create_environment(case, root=Path(temporary) / "model")
        reference = create_environment(case, root=Path(temporary) / "reference")
        try:
            for index, steps in enumerate(predictions_by_turn):
                if len(steps) > limits.max_steps_per_turn:
                    turn_results.append({"valid": False, "error_type": "multi_turn:force_terminated"})
                    break
                for calls in steps:
                    if not isinstance(calls, list) or not budget.reserve("requests"):
                        budget.stopped = budget.stopped or "invalid_steps"
                        break
                    if any(not budget.reserve("tool_calls") for _call in calls):
                        break
                    offered = {function["name"] for function in public_turn(case, index)["functions"]}
                    try:
                        if any(parse_call(expression)[1] not in offered for expression in calls):
                            budget.stopped = "multi_turn:unoffered_function"
                            break
                        model.call_expressions(calls)
                    except BFCLExecutionUnsupported:
                        budget.stopped = "multi_turn:invalid_call_expression"
                        break
                if budget.stopped:
                    turn_results.append({"valid": False, "error_type": budget.stopped})
                    break
                turn_results.append(_turn_check(model, reference, expected[index], steps, index))
            valid = len(turn_results) == len(expected) and all(item["valid"] for item in turn_results)
            return _safe({"id": case["id"], "valid": valid, "result": "pass" if valid else "fail",
                          "turns": turn_results, "scoring_rule": "pinned_bfcl_multiturn_state_and_response",
                          "model_quality": "unsupported", "model_api_calls": 0, "official_score": False,
                          "validation_kind": "saved_prediction_replay", "budget": budget.snapshot()})
        finally:
            model.close()
            reference.close()


async def run_case_offline(case, steps_by_turn, output_dir, *, limits=None, environment_factory=None, cancel=None,
                           shared_budget=None):
    """Run only fixed local scripts; this public entry has no provider injection."""
    return await _run_case_protocol(case, steps_by_turn, output_dir, limits=limits,
        environment_factory=environment_factory, cancel=cancel, shared_budget=shared_budget)


def _runtime_diagnostic(error):
    """Keep typed admission evidence without persisting raw provider messages."""
    if not error:
        return None
    text = str(error)
    kind = text.partition(":")[0]
    result = {"error_type": kind if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,70}", kind) else "RuntimeError",
              "reason": "runtime_failure"}
    if "Empty or duplicate tool name:" in text:
        result.update(reason="duplicate_tool_name", stage="admission")
        match = re.search(r"duplicate tool name: ['\"]([A-Za-z_][A-Za-z0-9_]{0,63})['\"]", text)
        if match:
            result["tool_name"] = match[1]
    elif "Configured model-iteration limit reached" in text:
        result["reason"] = "model_iteration_limit"
    elif "request budget exhausted" in text:
        result["reason"] = "request_budget"
    elif "Current model generation failed" in text:
        result["reason"] = "provider_failure"
    return result


def _usage(store, run_id):
    fields = ("input_tokens", "output_tokens", "reasoning_tokens", "cache_hit_tokens", "cache_miss_tokens")
    return [{key: value if type(value := to_dict(row).get(key)) is int and value >= 0 else None for key in fields}
            for row in store.usage(run_id)]


async def _run_case_protocol(case, steps_by_turn, output_dir, *, limits=None, environment_factory=None, cancel=None,
                              shared_budget=None, driver_factory=None, execution_mode="offline_scripted",
                              model_quality="unsupported", model_api_counter=None, agent_policy=None):
    """Shared trusted execution core; live admission exists only in v4_live.py."""
    from aster.evaluation.bfcl_v4_data import public_turn

    if agent_policy not in {None, "readiness_state_v1", "readiness_prompt_v1", "capability_check_v1", "readiness_check_v2"}:
        raise ValueError("Unknown explicit Agent execution policy")
    prompt_identity = None
    if agent_policy == "readiness_prompt_v1":
        from aster.engine.readiness_prompt import policy_identity

        prompt_identity = policy_identity()
    elif agent_policy == "capability_check_v1":
        from aster.engine.capability_gate import policy_identity

        prompt_identity = policy_identity()
    elif agent_policy == "readiness_check_v2":
        from aster.engine.readiness_gate_v2 import policy_identity

        prompt_identity = policy_identity()

    if driver_factory is None:
        if execution_mode != "offline_scripted" or model_quality != "unsupported" or model_api_counter is not None:
            raise ValueError("Offline script execution cannot be relabelled as model evaluation")
    elif (steps_by_turn is not None or execution_mode not in {"live_current_model", "test_transport"}
          or execution_mode == "test_transport" and model_quality != "unsupported"
          or execution_mode == "live_current_model" and model_quality != "evaluated"
          or shared_budget is None or not callable(model_api_counter)):
        raise ValueError("A live driver needs explicit controlled admission and cannot accept oracle scripts")
    case_started = time.monotonic()
    initial_api_calls = model_api_counter() if model_api_counter is not None else 0
    limits = limits or OfflineLimits()
    if not isinstance(limits, OfflineLimits):
        raise TypeError("OfflineLimits is required")
    if (shared_budget is not None and (not isinstance(shared_budget, OfflineCaseBudget)
            or shared_budget.limits != limits or shared_budget.cancel is not cancel)):
        raise ValueError("A shared case budget cannot be upgraded, rebound, or given a different cancellation target")
    scripts = _scripts(steps_by_turn, len(case["question"])) if driver_factory is None else None
    multi = case["category"].startswith("multi_turn_")
    expected = _expected(case) if multi else None
    reference_issues = reference_admission_issues(case) if multi else []
    output = Path(output_dir).expanduser().resolve()
    from aster.paths import resolve_data_dir

    if any(output.is_relative_to(root) for root in {(Path.home() / ".aster").resolve(), resolve_data_dir()}):
        raise ValueError("Offline evidence must remain outside the user Runtime")
    if output.exists():
        raise ValueError("Offline evidence must use a new output directory; no replay is automatic")
    output.mkdir(parents=True)
    factory = environment_factory or create_environment
    budget = shared_budget or OfflineCaseBudget(limits, path=output / "budget.json", cancel=cancel)
    start_counts = dict(budget.counts)
    checks, runs, ledger, all_messages, active = [], [], [], [], [None]
    with tempfile.TemporaryDirectory(prefix="aster-bfcl-v4-runtime-") as temporary:
        root = Path(temporary)
        workspace, data = root / "workspace", root / "runtime"
        workspace.mkdir()
        data.mkdir()
        model_env = factory(case, root=root / "model-simulator")
        reference = create_environment(case, root=root / "reference-simulator") if multi else None
        driver = (_ScriptedModel(scripts, model_env, budget) if driver_factory is None else
                  driver_factory(model_env, budget))
        names = [spec.name for spec in model_env.specs]
        config = {"model": driver.model, "reasoning_effort": getattr(driver, "reasoning_effort", "none"),
                  "max_output_tokens": getattr(driver, "max_output_tokens", 2048),
                  "allowed_roots": [str(workspace)],
                  "permissions": {"allowed_tools": names, "auto_approve_tools": names},
                  "hooks": [], "mcp_servers": [], "memory": {"semantic_enabled": False},
                  "runtime": {"max_iterations": limits.max_steps_per_turn, "max_model_requests": limits.max_requests,
                              "max_tool_calls": limits.max_tool_calls, "max_concurrency": 1, "model_retries": 0,
                              "deadline_seconds": limits.deadline_seconds,
                              "model_timeout_seconds": getattr(driver, "model_timeout_seconds", 5),
                              "semantic_compaction": False, "compaction_persist_learnings": False,
                              "file_restore_max_files": 0, "context_tokens": 96000, "reserve_tokens": 4096}}
        if agent_policy == "readiness_state_v1":
            config["runtime"].update(task_control=agent_policy,
                max_model_requests=min(limits.max_requests, limits.max_steps_per_turn))
        elif agent_policy == "readiness_prompt_v1":
            config["runtime"].update(prompt_policy=agent_policy, task_control="off",
                max_model_requests=min(limits.max_requests, limits.max_steps_per_turn))
        elif agent_policy in {"capability_check_v1", "readiness_check_v2"}:
            config["runtime"].update(capability_gate=agent_policy, prompt_policy="readiness_prompt_v1",
                task_control="off", max_model_requests=min(limits.max_requests, limits.max_steps_per_turn))
        (data / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
        coordinator = _OfflineCoordinator(data, environment=model_env, driver=driver, budget=budget)

        async def watch_cancel():
            if cancel is not None:
                await cancel.wait()
                if active[0] is not None:
                    coordinator.cancel(active[0])

        watcher = asyncio.create_task(watch_cancel())
        try:
            session = coordinator.create_session(workspace)
            answer = ""
            for index in range(len(case["question"])):
                if not budget.check():
                    break
                visible = public_turn(case, index)
                offered_original = {item["name"] for item in visible["functions"]}
                coordinator.offered = {alias for alias, name in model_env.original_names.items() if name in offered_original}
                context = model_env.context_for_turn() if hasattr(model_env, "context_for_turn") else ""
                (workspace / "AGENTS.md").write_text("\n\n".join(value for value in (visible["system"], context) if value),
                                                       encoding="utf-8")
                text = "\n\n".join(message["content"] for message in visible["messages"])
                if not text:
                    raise ValueError("Every submitted BFCL turn needs its official user text")
                driver.select_turn(index)
                before = len(coordinator.store.history(session["id"]))
                run = await coordinator.submit(session["id"], text, unattended=True)
                active[0] = run["id"]
                if cancel is not None and cancel.is_set():
                    coordinator.cancel(run["id"])
                outcome = await coordinator.wait(run["id"], timeout=limits.deadline_seconds + 5)
                active[0] = None
                budget.check()
                history = coordinator.store.history(session["id"])
                turn_messages = history[before:]
                decoded = []
                for message in turn_messages:
                    if message.role == "assistant" and message.tool_calls:
                        decoded.append([call.name for call in message.tool_calls])
                current_ledger = coordinator.store.tool_executions(run["id"])
                ledger.extend(current_ledger)
                runs.append({"turn_index": index, "run_id": run["id"], "runtime_status": outcome.status,
                             "runtime_budget": coordinator.store.run_budget(run["id"]), "answer": outcome.text,
                             "runtime_diagnostic": _runtime_diagnostic(outcome.error),
                             "usage": _usage(coordinator.store, run["id"])})
                if agent_policy is not None:
                    runs[-1]["agent_control_failure"] = (
                        (runs[-1]["runtime_diagnostic"] or {}).get("error_type") == "TaskControlError")
                    checkpoint = coordinator.store.get_checkpoint(session["id"])
                    runs[-1]["task_control"] = copy.deepcopy(
                        (checkpoint or {}).get("metadata", {}).get("task_control"))
                    receipts = coordinator.store.usage(run["id"])
                    runs[-1]["model_request_purposes"] = dict(Counter(
                        item.get("raw", {}).get("purpose", "unknown") for item in receipts))
                    runs[-1]["model_request_receipts"] = [{
                        "purpose": item.get("raw", {}).get("purpose", "unknown"),
                        "request_number": item.get("raw", {}).get("request_number"),
                        "status": item.get("raw", {}).get("status"),
                    } for item in receipts]
                    if prompt_identity is not None:
                        saved_config = coordinator.store.get_run(run["id"])["config"]
                        runs[-1].update(runtime_prompt_policy=saved_config.get("prompt_policy", "off"),
                            runtime_task_control=saved_config.get("task_control", "off"),
                            agent_policy_identity=copy.deepcopy(prompt_identity))
                    if agent_policy in {"capability_check_v1", "readiness_check_v2"}:
                        runs[-1].update(runtime_capability_gate=saved_config.get("capability_gate", "off"),
                            capability_gate=copy.deepcopy((checkpoint or {}).get("metadata", {}).get("capability_gate")))
                        observed = [item for item in driver.observations if item.get("turn_index") == index]
                        # Runtime has one writer and no model retries. Relate
                        # actual sequential driver inputs to durable receipts;
                        # absence/mismatch remains visible to the collector.
                        for observation, receipt in zip(observed, receipts):
                            observation.update(
                                request_purpose=receipt.get("raw", {}).get("purpose", "unknown"),
                                request_number=receipt.get("raw", {}).get("request_number"),
                                system_sha256=hashlib.sha256(observation["system"].encode("utf-8")).hexdigest())
                all_messages = to_dict(history)
                answer = outcome.text or ""
                if outcome.status != "completed" or budget.stopped:
                    checks.append({"valid": False, "error_type": budget.stopped or "multi_turn:force_terminated"})
                    break
                if multi:
                    checks.append(_turn_check(model_env, reference, expected[index], decoded, index))
            forced = len(runs) != len(case["question"]) or any(run["runtime_status"] != "completed" for run in runs)
            if not multi and not case.get("is_prerequisite"):
                from aster.evaluation.bfcl_v4_official import grade_agentic_answer

                checks.append(grade_agentic_answer(answer, case["expected"], track=case["category"]))
            rejected = any(message.get("role") == "tool" and message.get("metadata", {}).get("is_error")
                           for message in all_messages)
            valid = bool(checks) and all(item["valid"] for item in checks) and not forced and not budget.stopped and not rejected
            protocol_ok = not forced and not budget.stopped and not rejected
            if protocol_ok and hasattr(model_env, "finish_entry"):
                model_env.finish_entry()
            verification = {"valid": valid, "result": "pass" if valid else "fail", "turns": checks}
            if case.get("is_prerequisite"):
                verification = {"valid": None, "result": "not_scored", "turns": [],
                                "reason": "memory_prerequisite_writing_is_not_an_assessment"}
            entry_budget = budget.snapshot()
            entry_budget["case_counts"] = dict(budget.counts)
            entry_budget["counts"] = {key: budget.counts[key] - start_counts[key] for key in budget.counts}
            actual_api_calls = model_api_counter() - initial_api_calls if model_api_counter is not None else 0
            report = _safe({"id": case["id"], "mode": execution_mode, "model_quality": model_quality,
                            "official_score": False, "model_api_calls": actual_api_calls,
                            "validation_kind": "coordinator_protocol_and_scorer" if driver_factory is None else "current_model_new_track_smoke",
                            "protocol_result": "pass" if protocol_ok else "fail",
                            "scoring_verification": verification,
                            "reference_admission_issues": reference_issues,
                            "validation_expectation": "expected_rejection" if reference_issues else "normal_execution",
                            "runtime_admission_status": "blocked_dataset_conflict" if reference_issues else "admitted",
                            "forced_termination": forced, "budget": entry_budget, "runs": runs,
                            "elapsed_seconds": round(time.monotonic() - case_started, 4),
                            "usage": [usage for run in runs for usage in run["usage"]],
                            "session_id": session["id"], "step_observations": driver.observations,
                            "messages": all_messages, "tool_ledger": ledger, "simulator_results": model_env.results})
            if agent_policy is not None:
                report["agent_policy"] = agent_policy
                report["agent_control_failure"] = any(run.get("agent_control_failure") for run in runs)
            if prompt_identity is not None:
                report["agent_policy_identity"] = copy.deepcopy(prompt_identity)
            _write(output / "result.json", report)
            return report
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
            await coordinator.close()
            model_env.close()
            if reference is not None:
                reference.close()


def gold_replay_script(case):
    """Produce an explicitly labelled oracle driver for protocol tests only."""
    script = []
    environment = create_environment(case, root=Path(tempfile.gettempdir()))
    try:
        for turn in _expected(case):
            steps = [{"calls": [environment.decode_call(expression)]} for expression in turn]
            steps.append({"text": "Offline oracle-script turn complete; no language model prediction."})
            script.append(steps)
    finally:
        environment.close()
    return script


def reference_script(case):
    """Group an oracle fixture's turn into an ordered batch, then finish it.

    This is exclusively an offline environment/scorer test. It is never a real
    model prediction, and is never passed to any external model or provider.
    """
    return [[{"calls": [call for step in turn for call in step.get("calls", [])]}, turn[-1]]
            if any(step.get("calls") for step in turn) else [turn[-1]]
            for turn in gold_replay_script(case)]


def reference_admission_issues(case):
    """Keep upstream gold/schema conflicts separate from runtime/model errors."""
    environment = create_environment(case, root=Path(tempfile.gettempdir()))
    try:
        issues = []
        for turn_index, turn in enumerate(_expected(case)):
            for call_index, expression in enumerate(turn):
                issues.extend({"id": case["id"], "turn_index": turn_index, "call_index": call_index,
                               **issue} for issue in environment.admission_issues(expression))
        return issues
    finally:
        environment.close()
