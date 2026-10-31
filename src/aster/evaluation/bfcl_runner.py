"""Isolated BFCL call-prediction runs through the production Coordinator.

This module never executes dataset-supplied code or contacts the APIs described
by a BFCL function. A complete first model turn is the prediction; virtual tool
receipts exercise Aster's real validation, policy and durable dispatch path.
"""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import time
from collections import Counter
from pathlib import Path

import yaml

from aster.config import read_yaml
from aster.customization.credentials import CredentialStore
from aster.domain import ModelTurn, ToolResult, to_dict
from aster.engine.telemetry import performance
from aster.evaluation.bfcl import grade_case, load_cases, public_case, tool_specs
from aster.paths import resolve_data_dir
from aster.platform.filesystem import FileLock, atomic_write
from aster.service.coordinator import RunCoordinator


def _json_write(path, value):
    atomic_write(Path(path), json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode())


def _usage_counts(usage):
    fields = ("input_tokens", "output_tokens", "reasoning_tokens", "cache_hit_tokens", "cache_miss_tokens")
    value = to_dict(usage)
    return {key: value.get(key) if type(value.get(key)) is int and value[key] >= 0 else None for key in fields}


class PredictionBudget:
    """Persistent pre-dispatch request reservations; unknown calls stay charged."""

    def __init__(self, path, *, max_requests, max_output_tokens, binding):
        if type(max_requests) is not int or max_requests < 1:
            raise ValueError("BFCL live mode requires a positive explicit request budget")
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 2048:
            raise ValueError("BFCL output limit must be between 1 and 2048")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.identity = {"max_requests": max_requests, "max_output_tokens": max_output_tokens, "binding": binding}
        with self._lock():
            if self.path.exists():
                previous = self._read()
                if any(previous.get(key) != value for key, value in self.identity.items()):
                    raise ValueError("An existing BFCL budget cannot be reset or rebound")
            else:
                _json_write(self.path, {**self.identity, "calls": []})

    def _lock(self):
        return FileLock(self.path.with_suffix(self.path.suffix + ".lock"), timeout=10)

    def _read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def snapshot(self):
        with self._lock():
            return self._read()

    async def generate(self, client, case_id, *args, **kwargs):
        with self._lock():
            data = self._read()
            if len(data["calls"]) >= self.identity["max_requests"]:
                raise RuntimeError("BFCL request budget exhausted")
            # Reserve under the lock before yielding to the provider, so concurrent
            # callers cannot reuse a remaining slot or recycle an unknown outcome.
            index = len(data["calls"])
            data["calls"].append({"case_id": case_id, "started_at": time.time(), "status": "outcome_unknown", "usage": None})
            _json_write(self.path, data)
        try:
            turn = await client.generate(*args, **kwargs)
        except BaseException as error:
            with self._lock():
                data = self._read()
                # Exception text may contain credentials or source content.
                data["calls"][index]["failure_type"] = type(error).__name__
                _json_write(self.path, data)
            raise
        with self._lock():
            data = self._read()
            data["calls"][index].update(status="response_received", usage=_usage_counts(turn.usage))
            _json_write(self.path, data)
        return turn


class BudgetedPredictionModel:
    def __init__(self, client, budget, case_id):
        self.inner, self.budget, self.case_id = client, budget, case_id

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def generate(self, *args, **kwargs):
        return await self.budget.generate(self.inner, self.case_id, *args, **kwargs)


class _NoCredentials:
    def get(self, *args, **kwargs):
        return None

    def resolve(self, reference):
        raise RuntimeError("Offline BFCL runs do not resolve credentials")


class _ExplicitCredentials:
    """Read only the explicitly named credential file, never ambient env keys."""

    storage = "file"

    def __init__(self, data_dir):
        self.inner = CredentialStore(data_dir=data_dir)

    def get(self, name="deepseek", *, use_environment=False):
        return self.inner.get(name, use_environment=False)

    def resolve(self, reference):
        if not reference.startswith("keyring:"):
            raise ValueError("BFCL uses explicit file credentials only")
        return self.inner.resolve(reference)


class _OfflineModel:
    model = "offline-validation-driver"

    def __init__(self):
        self.identity = {"kind": "offline-validation-driver"}

    async def generate(self, messages, tools, *, system, emit=None, cancel=None):
        return ModelTurn(text="Offline BFCL schema and Coordinator check; no language-model prediction.")


class _CaptureTool:
    def __init__(self, spec):
        self.spec = spec

    async def execute(self, arguments, context):
        return ToolResult("BFCL evaluation receipt: call captured only; no real function or external service was executed.",
                          data={"evaluation_only": True, "function_executed": False})


class _PredictionCoordinator(RunCoordinator):
    def __init__(self, *args, prediction_tools, budget=None, case_id, **kwargs):
        super().__init__(*args, tools=[_CaptureTool(spec) for spec in prediction_tools], **kwargs)
        self._prediction_names = {spec.name for spec in prediction_tools}
        self._prediction_budget, self._prediction_case_id = budget, case_id

    async def _build_engine(self, run, payload, **kwargs):
        engine, services, resources = await super()._build_engine(run, payload, **kwargs)
        # Set visibility before the run starts; admission still uses frozen policy.
        services["active_tools"] = set(self._prediction_names)
        engine.services["active_tools"] = set(self._prediction_names)
        if self._prediction_budget is not None:
            engine.model = BudgetedPredictionModel(engine.model, self._prediction_budget, self._prediction_case_id)
        return engine, services, resources


def _selected_settings(credential_data_dir, model, reasoning_effort):
    try:
        source = read_yaml(Path(credential_data_dir) / "config.yaml")
    except (ValueError, TypeError, OSError, yaml.YAMLError):
        raise ValueError("Invalid BFCL test configuration") from None
    chosen_model = model if model is not None else source.get("model", "deepseek-flash")
    chosen_effort = reasoning_effort if reasoning_effort is not None else source.get("reasoning_effort", "low")
    if not isinstance(chosen_model, str) or not chosen_model.strip():
        raise ValueError("BFCL requires an explicit nonempty model ID")
    if chosen_effort not in {"none", "low", "high", "max"}:
        raise ValueError("Unsupported native DeepSeek BFCL reasoning effort")
    return chosen_model, chosen_effort


def _runtime_settings(workspace, names, *, model, reasoning_effort, max_output_tokens):
    return {"model": model, "reasoning_effort": reasoning_effort, "max_output_tokens": max_output_tokens,
            "allowed_roots": [str(workspace)], "permissions": {"allowed_tools": sorted(names)},
            "runtime": {"max_iterations": 1, "max_model_requests": 1, "max_tool_calls": 64,
                        "max_concurrency": 1, "model_retries": 0, "deadline_seconds": 180,
                        "model_timeout_seconds": 120, "context_tokens": 96000, "reserve_tokens": 4096,
                        "semantic_compaction": False, "compaction_persist_learnings": False,
                        "file_restore_max_files": 0}}


async def _run_case(case, scratch, evidence_root, *, mode, factory, credentials, budget, settings):
    identifier = hashlib.sha256(case["id"].encode()).hexdigest()[:24]
    root = Path(scratch) / identifier
    workspace, data = root / "workspace", root / "runtime"
    workspace.mkdir(parents=True)
    data.mkdir()
    specs = tool_specs(case)
    names = {spec.name for spec in specs}
    config = _runtime_settings(workspace, names, **settings)
    (data / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    coordinator = _PredictionCoordinator(data, prediction_tools=specs, budget=budget, case_id=case["id"],
                                         model_factory=factory, credentials=credentials, browser_enabled=False,
                                         max_parallel_runs=1)
    started = time.monotonic()
    try:
        session = coordinator.create_session(workspace)
        visible = public_case(case)
        # Supported BFCL first-turn cases have exactly one user message. The
        # evaluator keeps expected answers outside all runtime input surfaces.
        run = await coordinator.submit(session["id"], visible["prompt"], unattended=True)
        outcome = await coordinator.wait(run["id"], timeout=210)
        messages = coordinator.store.history(session["id"])
        assistant = next((message for message in messages if message.role == "assistant"), None)
        calls = [{"name": call.name, "arguments": copy.deepcopy(call.arguments)} for call in assistant.tool_calls] if assistant else []
        answer = assistant.content if assistant else ""
        complete = assistant is not None
        prediction = grade_case(case, calls, answer) if complete else {
            "result": "fail", "passed": False, "failures": ["no_complete_model_prediction"],
            "grading_scope": "local_bfcl_structured_calls", "official_score": False}
        ledger = coordinator.store.tool_executions(run["id"])
        # A syntactically matching prediction must not hide Runtime rejections.
        expected_boundary = (outcome.status == "budget_exhausted" and complete and bool(calls)
                             and outcome.error == "Configured model-iteration limit reached")
        admissible = (outcome.status == "completed" and complete) or expected_boundary
        if not admissible:
            prediction = {**prediction, "result": "fail", "passed": False,
                          "failures": [*prediction.get("failures", []), "runtime_prediction_failed"]}
        rejected_prediction = any(message.role == "tool" and message.metadata.get("is_error") for message in messages)
        if rejected_prediction:
            prediction = {**prediction, "result": "fail", "passed": False,
                          "failures": [*prediction.get("failures", []), "dispatcher_rejected_prediction"]}
        trace_path = Path(evidence_root) / identifier / "trace.json"
        trace_path.parent.mkdir(parents=True)
        _json_write(trace_path, {"case_id": case["id"], "scope": "private_raw_prediction_evidence",
                                "workspace": str(workspace), "runtime_data_dir": str(data),
                                "messages": to_dict(messages), "tool_ledger": ledger,
                                "performance": performance(coordinator.store, run["id"]),
                                "runtime_config": {key: value for key, value in coordinator.store.get_run(run["id"])["config"].items()
                                                   if not key.startswith("_")}})
        return {"id": case["id"], "category": case["category"],
                "result": prediction["result"] if mode == "live" else "unsupported",
                "model_quality": "evaluated" if mode == "live" else "unsupported",
                "capability_scope": "single_turn_tool_selection" if mode == "live" else "runtime_protocol_only",
                "official_score": False, "prediction_check": prediction,
                "protocol_result": "pass" if admissible and not rejected_prediction else "fail",
                "runtime_status": outcome.status, "prediction_boundary_reached": expected_boundary,
                "tool_outcomes": [{"name": row["tool_name"], "state": row["state"]} for row in ledger],
                "usage": [_usage_counts(row) for row in coordinator.store.usage(run["id"])],
                "runtime_budget": coordinator.store.run_budget(run["id"]),
                "elapsed_seconds": round(time.monotonic() - started, 4), "evidence_file": str(trace_path)}
    finally:
        await coordinator.close()


def _write_report(output, report):
    _json_write(output, report)
    lines = ["# Aster BFCL structured-call evaluation", "",
             "This is a derived local structured-call subset, not an official BFCL leaderboard score.",
             "Offline and injected-model checks never measure language-model capability.",
             "A one-iteration budget boundary after call capture is retained as the actual Runtime status.",
             "Virtual tools only record calls; no real function or external service is executed.", "",
             "| Case | Category | Result | Model quality | Runtime status |", "|---|---|---|---|---|"]
    for row in report["results"]:
        lines.append("| " + " | ".join(str(row.get(key, "")).replace("|", "\\|").replace("\n", " ")
                                      for key in ("id", "category", "result", "model_quality", "runtime_status")) + " |")
    lines += ["", "Totals: " + json.dumps(report["totals"], sort_keys=True)]
    atomic_write(Path(output).with_suffix(".md"), "\n".join(lines).encode())


async def run_bfcl(data_path, answers_path, output, *, mode="offline", max_requests=None, max_output_tokens=1024,
                   case_ids=None, category=None, model_factory=None, credential_data_dir=None,
                   model=None, reasoning_effort=None):
    """Run first-turn BFCL call prediction; live requires a separate test key directory.

    Existing output or started budgets are never replayed automatically. Native
    DeepSeek is the first live transport; compatible profiles are not imported
    from another Runtime and no caller's runtime.db is opened.
    """
    if mode not in {"offline", "live"}:
        raise ValueError("BFCL mode must be offline or live")
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 2048:
        raise ValueError("BFCL output limit must be between 1 and 2048")
    if case_ids is not None:
        if isinstance(case_ids, (str, bytes)):
            raise ValueError("BFCL case selection must be a list of distinct IDs")
        case_ids = list(case_ids)
        if (not case_ids or any(not isinstance(case_id, str) or not case_id for case_id in case_ids)
                or len(case_ids) != len(set(case_ids))):
            raise ValueError("BFCL case selection must contain distinct nonempty IDs")
    if mode == "live":
        if type(max_requests) is not int or max_requests < 1 or credential_data_dir is None:
            raise ValueError("BFCL live mode requires an explicit request budget and credential data directory")
        if model_factory is not None:
            raise ValueError("Injected models cannot produce a live BFCL capability score")
        credential_data_dir = Path(credential_data_dir).expanduser().resolve()
        if (credential_data_dir in {(Path.home() / ".aster").resolve(), resolve_data_dir()}
                or (credential_data_dir / "runtime.db").exists()):
            raise ValueError("Use a separate test credential directory, not the default user Runtime")
        if not (credential_data_dir / "credentials.yaml").is_file():
            raise ValueError("The explicit BFCL test credential file does not exist")
    output = Path(output).expanduser().resolve()
    if output.suffix.lower() != ".json":
        raise ValueError("BFCL output must have a .json extension")
    protected = {Path(data_path).expanduser().resolve(), Path(answers_path).expanduser().resolve()}
    generated = {output, output.with_suffix(".md"), output.with_suffix(".budget.json"), output.with_suffix(".campaign.lock")}
    if generated & protected:
        raise ValueError("BFCL report paths must not overlap the dataset or answer files")
    if any(output.is_relative_to(root) for root in {(Path.home() / ".aster").resolve(), resolve_data_dir()}):
        raise ValueError("BFCL reports must be outside the default user Runtime")
    if mode == "live" and output.is_relative_to(credential_data_dir):
        raise ValueError("BFCL reports must be outside the test credential directory")
    cases = load_cases(data_path, answers_path, category=category)
    requested = set(case_ids) if case_ids is not None else None
    if requested is not None:
        cases = [case for case in cases if case["id"] in requested]
        if not cases or requested != {case["id"] for case in cases}:
            raise ValueError("Unknown or empty BFCL case selection")
    if not cases:
        raise ValueError("No supported BFCL cases selected")
    if output.exists() or output.with_suffix(".md").exists() or output.with_suffix(".evidence").exists():
        raise ValueError("BFCL output already exists; reconcile the previous run before choosing a fresh output")
    output.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(output.with_suffix(".campaign.lock"), timeout=.1):
        if output.exists() or output.with_suffix(".md").exists() or output.with_suffix(".evidence").exists():
            raise ValueError("BFCL output already exists; automatic replay is prohibited")
        chosen_model, chosen_effort = (_selected_settings(credential_data_dir, model, reasoning_effort) if mode == "live"
                                      else ("offline-validation-driver", "low"))
        binding = hashlib.sha256(json.dumps({"cases": [{"id": case["id"], "provenance": case["provenance"]} for case in cases],
                                            "model": chosen_model, "effort": chosen_effort}, sort_keys=True).encode()).hexdigest()
        budget = (PredictionBudget(output.with_suffix(".budget.json"), max_requests=max_requests,
                                   max_output_tokens=max_output_tokens, binding=binding) if mode == "live" else None)
        if budget and budget.snapshot()["calls"]:
            raise ValueError("A BFCL budget has already dispatched requests; automatic replay is prohibited")
        credentials = _ExplicitCredentials(credential_data_dir) if mode == "live" else _NoCredentials()
        if mode == "live" and not credentials.get():
            raise ValueError("The explicit test directory has no configured DeepSeek credential")
        report = {"schema_version": 1, "benchmark": "BFCL local structured-call subset", "mode": mode,
                  "official_score": False, "model_quality": "evaluated" if mode == "live" else "unsupported",
                  "paid_calls_authorized": mode == "live", "model": chosen_model, "reasoning_effort": chosen_effort,
                  "dataset_binding": binding, "planned_cases": len(cases), "results": [], "totals": {}}
        factory = None if mode == "live" else (model_factory or (lambda _: _OfflineModel()))
        evidence_root = output.with_suffix(".evidence")
        with tempfile.TemporaryDirectory(prefix="aster-bfcl-") as scratch:
            for case in cases:
                if budget and len(budget.snapshot()["calls"]) >= max_requests:
                    result = {"id": case["id"], "category": case["category"], "result": "not_run",
                              "model_quality": "unsupported", "runtime_status": "not_started", "reason": "request_budget_exhausted"}
                else:
                    try:
                        result = await _run_case(case, scratch, evidence_root, mode=mode, factory=factory,
                                                 credentials=credentials, budget=budget,
                                                 settings={"model": chosen_model, "reasoning_effort": chosen_effort,
                                                           "max_output_tokens": max_output_tokens})
                    except Exception as error:  # noqa: BLE001 - isolate a failing grader or case in the report.
                        result = {"id": case["id"], "category": case["category"],
                                  "result": "fail" if mode == "live" else "unsupported", "model_quality": "unsupported",
                                  "runtime_status": "case_error", "protocol_result": "fail",
                                  "error_type": type(error).__name__, "official_score": False}
                report["results"].append(result)
                report["totals"] = dict(Counter(row["result"] for row in report["results"]))
                report["protocol_totals"] = dict(Counter(row.get("protocol_result", "not_run") for row in report["results"]))
                _write_report(output, report)
        if budget:
            report["generation_attempts"] = len(budget.snapshot()["calls"])
        _write_report(output, report)
        return report
