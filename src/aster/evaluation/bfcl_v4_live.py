"""Explicitly budgeted current-model smoke runs for the new BFCL V4 tracks.

No provider is selected or contacted on import. The caller supplies a captured
profile and a new campaign directory; every target HTTP attempt is durably
charged against one immutable campaign budget before dispatch.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from pathlib import Path

import httpx

from aster.domain import to_dict
from aster.evaluation.bfcl_current import CapturedProfile, HTTPCallBudget
from aster.evaluation.bfcl_v4_runner import OfflineCaseBudget, OfflineLimits, reference_admission_issues
from aster.platform.filesystem import FileLock, atomic_write
from aster.providers.compatible import CompatibleClient, ScopedModel
from aster.providers.deepseek import DeepSeekResponsesClient, ModelCapabilities, ProviderError
from aster.providers.profiles import PublicProviderTransport

MAX_V4_API_CALLS = 50


def _write(path, value):
    atomic_write(Path(path), json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode())


def _public_profile(profile):
    summary = profile.public_summary()
    public = {key: summary[key] for key in ("provider_id", "provider_name", "base_url", "protocol", "model",
                                           "reasoning_effort", "identity_revision")}
    public["capabilities"] = {key: summary["capabilities"][key] for key in (
        "id", "name", "context_window", "max_output_tokens", "input_modalities", "effort_levels")
        if key in summary.get("capabilities", {})}
    return public


class V4HTTPCallBudget(HTTPCallBudget):
    """Reuse checked dispatch hooks, with a separate explicit V4 maximum of 50."""

    def __init__(self, path, *, maximum, binding, case_caps=None, case_groups=None):
        if type(maximum) is not int or not 1 <= maximum <= MAX_V4_API_CALLS:
            raise ValueError("A V4 campaign requires an explicit API cap between 1 and 50")
        self.path, self.maximum = Path(path), maximum
        self.case_caps, self.case_groups = dict(case_caps or {}), dict(case_groups or {})
        if (any(not isinstance(key, str) or not key or type(cap) is not int or not 1 <= cap <= maximum
                for key, cap in self.case_caps.items())
                or any(not isinstance(key, str) or not isinstance(group, str) or group not in self.case_caps
                       for key, group in self.case_groups.items())):
            raise ValueError("V4 case caps must bind selected cases to bounded groups")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock():
            if self.path.exists():
                previous = self.snapshot_unlocked()
                if (previous.get("binding") != binding or previous.get("maximum") != maximum
                        or previous.get("case_caps", {}) != self.case_caps
                        or previous.get("case_groups", {}) != self.case_groups
                        or previous.get("calls")):
                    raise ValueError("Existing V4 API budget cannot be rebound, upgraded, or replayed")
            else:
                _write(self.path, {"schema_version": 1, "maximum": maximum, "binding": binding,
                                   "case_caps": self.case_caps, "case_groups": self.case_groups, "calls": []})

    def group(self, case_id):
        if self.case_groups and case_id not in self.case_groups:
            raise ProviderError("Case is outside the V4 budget binding", category="budget")
        return self.case_groups.get(case_id, case_id)

    def remaining_group(self, case_id):
        group = self.group(case_id)
        document = self.snapshot()
        maximum = self.case_caps.get(group, self.maximum)
        used = sum(row.get("budget_group", row["case_id"]) == group for row in document["calls"])
        return min(self.maximum - len(document["calls"]), maximum - used)

    async def before_request(self, request, *, profile, case_id):
        profile.validate()
        expected = profile.base_url + ("/chat/completions" if profile.protocol == "openai_chat" else "/responses")
        if request.method != "POST" or str(request.url) != expected:
            raise ProviderError("Unexpected V4 API operation rejected", category="endpoint")
        group = self.group(case_id)
        with self.lock():
            document = self.snapshot_unlocked()
            used = sum(row.get("budget_group", row["case_id"]) == group for row in document["calls"])
            if len(document["calls"]) >= self.maximum or used >= self.case_caps.get(group, self.maximum):
                raise ProviderError("V4 HTTP budget exhausted", category="budget")
            index = len(document["calls"])
            document["calls"].append({"case_id": case_id, "budget_group": group, "method": request.method,
                                      "path": request.url.path, "started_at": time.time(), "status": "outcome_unknown"})
            _write(self.path, document)
        request.extensions["aster_bfcl_api_index"] = index


class _LiveProtocolBudget(OfflineCaseBudget):
    def __init__(self, limits, *, http_budget, path, cancel=None, test_only=False):
        self.http_budget, self.test_only = http_budget, test_only
        super().__init__(limits, path=path, cancel=cancel)

    def snapshot(self):
        value = super().snapshot()
        attempts = len(self.http_budget.snapshot()["calls"])
        value["model_api_calls"] = 0 if self.test_only else attempts
        value["http_transport_attempts"] = attempts
        value["request_count_note"] = "Logical generation reservations are distinct from actual HTTP attempt receipts."
        return value


def _diagnostic_field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _tool_identity_witness(messages, turn):
    """Observe IDs without changing returned calls or exposing raw identity text."""
    history_calls, history_items = set(), set()
    for message in messages:
        for call in _diagnostic_field(message, "tool_calls", []) or []:
            identifier = _diagnostic_field(call, "id")
            if isinstance(identifier, str) and identifier:
                history_calls.add(identifier)
        for item in _diagnostic_field(message, "provider_items", []) or []:
            if _diagnostic_field(item, "type") == "function_call":
                identifier = _diagnostic_field(item, "call_id")
                if isinstance(identifier, str) and identifier:
                    history_items.add(identifier)
    calls = _diagnostic_field(turn, "tool_calls", []) or []
    identifiers = [_diagnostic_field(call, "id") for call in calls]
    valid = [identifier for identifier in identifiers if isinstance(identifier, str) and identifier]
    counts = Counter(valid)
    duplicated = sorted(identifier for identifier, count in counts.items() if count > 1)
    reused_calls = sorted(set(valid) & history_calls)
    reused_items = sorted(set(valid) & history_items)

    def hashes(values):
        return [hashlib.sha256(value.encode("utf-8")).hexdigest() for value in values[:64]]

    return {"schema_version": 1, "available": True, "hash_algorithm": "sha256_utf8",
        "hash_list_limit": 64, "returned_tool_call_count": len(identifiers),
        "missing_or_nonstring_id_count": len(identifiers) - len(valid),
        "duplicate_id_count": sum(count - 1 for count in counts.values()),
        "duplicate_unique_id_count": len(duplicated),
        "history_tool_call_unique_id_count": len(history_calls),
        "history_provider_call_unique_id_count": len(history_items),
        "reused_tool_call_id_count": sum(identifier in history_calls for identifier in valid),
        "reused_tool_call_unique_id_count": len(reused_calls),
        "reused_provider_call_id_count": sum(identifier in history_items for identifier in valid),
        "reused_provider_call_unique_id_count": len(reused_items),
        "returned_id_sha256": hashes(valid), "duplicate_id_sha256": hashes(duplicated),
        "reused_tool_call_id_sha256": hashes(reused_calls),
        "reused_provider_call_id_sha256": hashes(reused_items),
        "hash_lists_truncated": any(len(values) > 64 for values in (valid, duplicated, reused_calls, reused_items))}


def _failure_io_witness(error, stage, http_budget, protocol_budget):
    """Keep only typed OS error numbers and an exact known-target role."""
    witness = {"diagnostic_version": 1, "diagnostic_stage": stage,
        "errno": None, "winerror": None, "failure_target_role": "unknown"}
    current = error
    for _ in range(4):
        if isinstance(current, OSError):
            witness["errno"] = current.errno if type(current.errno) is int else None
            winerror = getattr(current, "winerror", None)
            witness["winerror"] = winerror if type(winerror) is int else None
            for role, owner in (("http_ledger", http_budget), ("protocol_budget", protocol_budget)):
                target = getattr(owner, "path", None)
                if not isinstance(target, (str, Path)):
                    continue
                for name in ("filename", "filename2"):
                    filename = getattr(current, name, None)
                    if isinstance(filename, (str, Path)) and Path(filename).resolve() == Path(target).resolve():
                        witness["failure_target_role"] = role
            break
        current = current.__cause__ or current.__context__
        if current is None:
            break
    return witness


class _CurrentModelDriver:
    """Transparent production client identity with no retries or metadata calls."""

    def __init__(self, profile, budget, case_id, *, protocol_budget, max_output_tokens, test_transport_factory=None,
                 dns_fallback="off"):
        profile.validate()
        self.profile, self.budget, self.case_id = profile, budget, case_id
        self.protocol_budget = protocol_budget
        self.model, self.reasoning_effort = profile.model, profile.reasoning_effort
        self.max_output_tokens, self.model_timeout_seconds = max_output_tokens, 120
        self.observations, self.failures, self.turn = [], [], 0
        self.closed = False
        self._diagnostic_stage, self._diagnostic_observation = "idle", None
        self._diagnostic_http_status_seen = None

        async def before(request):
            self._diagnostic_http_status_seen = None
            self._stage("request_validation")
            await budget.before_request(request, profile=profile, case_id=case_id)
            self._stage("request_reserved")

        async def after(response):
            status = response.status_code
            self._diagnostic_http_status_seen = status if type(status) is int and 100 <= status <= 599 else None
            self._stage("headers_received")
            await budget.after_response(response)
            self._stage("headers_recorded")

        transport = (test_transport_factory() if test_transport_factory is not None else
                     (None if profile.protocol == "deepseek_responses" else
                      PublicProviderTransport(profile.base_url, dns_fallback=dns_fallback)))
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=20), follow_redirects=False,
            trust_env=profile.protocol == "deepseek_responses", transport=transport,
            event_hooks={"request": [before], "response": [after]})
        common = {"model": profile.model, "base_url": profile.base_url, "effort": profile.reasoning_effort,
                  "max_output_tokens": min(max_output_tokens, profile.capabilities.get("max_output_tokens") or max_output_tokens),
                  "max_retries": 0, "timeout_seconds": 120, "client": self.client}
        client = (DeepSeekResponsesClient(profile.key, **common) if profile.protocol == "deepseek_responses" else
                  CompatibleClient(profile.key, protocol=profile.protocol, **common))
        capabilities = profile.capabilities
        client.capabilities = {profile.model: ModelCapabilities(profile.model, capabilities.get("context_window"),
            capabilities.get("max_output_tokens"), tuple(capabilities.get("input_modalities", ["text"])),
            tuple(capabilities.get("effort_levels", [])), "captured-no-metadata-request", {})}
        self.identity = {"provider_id": profile.provider_id, "model": profile.model,
                         "protocol": profile.protocol, "revision": profile.identity_revision}
        self.inner = ScopedModel(client, self.identity, profile.validate)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def select_turn(self, index):
        self.turn = index

    def _stage(self, stage):
        self._diagnostic_stage = stage
        if self._diagnostic_observation is not None:
            self._diagnostic_observation["driver_stage"] = stage

    async def generate(self, messages, tools, *, system, emit=None, cancel=None):
        self._diagnostic_observation = None
        self._diagnostic_http_status_seen = None
        self._stage("budget_preflight")
        try:
            if self.budget.remaining_group(self.case_id) <= 0:
                raise ProviderError("V4 case HTTP budget exhausted", category="budget")
            self._stage("protocol_budget_reservation")
            if not self.protocol_budget.reserve("requests"):
                raise ProviderError("V4 logical request/deadline budget exhausted", category="budget")
            observation = {"turn_index": self.turn, "messages": to_dict(messages),
                           "offered_tools": [tool.name for tool in tools], "system": system}
            self.observations.append(observation)
            self._diagnostic_observation = observation
            self._stage("model_dispatch")
            turn = await self.inner.generate(messages, tools, system=system, emit=emit, cancel=cancel)
            self._stage("model_turn_received")
            try:
                observation["tool_call_id_witness"] = _tool_identity_witness(messages, turn)
            except Exception:  # noqa: BLE001 - optional observation must never change a returned turn.
                observation["tool_call_id_witness"] = {"schema_version": 1, "available": False}
            return turn
        except Exception as error:  # noqa: BLE001 - no raw provider error text or headers leave this boundary.
            failure = {"type": type(error).__name__, "category": getattr(error, "category", "unknown"),
                       "status_code": getattr(error, "status_code", None)}
            try:
                failure.update(_failure_io_witness(error, self._diagnostic_stage, self.budget, self.protocol_budget))
            except Exception:  # noqa: BLE001 - preserve the original failure even if optional diagnostics fail.
                failure.update(diagnostic_version=1, diagnostic_stage=self._diagnostic_stage,
                               errno=None, winerror=None, failure_target_role="unknown")
            failure["diagnostic_http_status_seen"] = self._diagnostic_http_status_seen
            self.failures.append(failure)
            safe_message = ("V4 request budget exhausted" if failure["category"] == "budget" else
                            "Current model generation failed")
            raise ProviderError(safe_message, category=failure["category"],
                                status_code=failure["status_code"]) from None

    async def aclose(self):
        if not self.closed:
            self.closed = True
            await self.inner.aclose()
            await self.client.aclose()


def _status(result):
    if result.get("status") in {"not_run", "blocked_dataset_conflict", "case_error"}:
        return result["status"]
    check = result.get("scoring_verification", {})
    if check.get("result") == "not_scored":
        return "prerequisite_completed" if result.get("protocol_result") == "pass" else "fail"
    return "pass" if check.get("valid") is True and result.get("protocol_result") == "pass" else "fail"


async def run_current_v4_cases(cases, output_dir, *, profile, max_api_calls, max_output_tokens=2048,
                               limits=None, environment_factories=None, case_api_caps=None, case_groups=None,
                               cancel=None, _test_transport_factory=None):
    """Run an explicitly authorized new-track campaign once; never resume it.

    Search uses caller-supplied local fixtures. Memory factories use explicitly
    isolated snapshots. Neither can silently dispatch real business tools.
    Injected HTTP transports are always test-only and produce no model score.
    """
    from aster.evaluation.bfcl_v4_runner import _run_case_protocol
    from aster.paths import resolve_data_dir

    if not isinstance(profile, CapturedProfile):
        raise TypeError("An explicitly captured current provider is required")
    if type(max_api_calls) is not int or not 1 <= max_api_calls <= MAX_V4_API_CALLS:
        raise ValueError("An explicit V4 API limit from 1 to 50 is required")
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 2048:
        raise ValueError("V4 output tokens must be bounded by 2048")
    if not isinstance(cases, list) or not cases or any(not isinstance(case, dict) for case in cases):
        raise ValueError("An explicit nonempty list of V4 cases is required")
    ids = [case.get("id") for case in cases]
    if any(not isinstance(identifier, str) or not identifier for identifier in ids) or len(set(ids)) != len(ids):
        raise ValueError("Explicit V4 cases must have distinct IDs")
    if any(not (case.get("category", "").startswith("multi_turn_") or
                case.get("category") in {"web_search", "memory", "memory_prereq"}) for case in cases):
        raise ValueError("This helper accepts only the new multi-turn and Agentic tracks")
    if (case_groups is not None and (not isinstance(case_groups, dict) or not set(case_groups) <= set(ids))):
        raise ValueError("Budget groups must refer only to selected V4 entries")
    groups = {identifier: (case_groups or {}).get(identifier, identifier) for identifier in ids}
    if any(not isinstance(group, str) or not group for group in groups.values()):
        raise ValueError("A nonempty budget-group identity is required")
    caps = dict.fromkeys(set(groups.values()), max_api_calls) if case_api_caps is None else case_api_caps
    if (not isinstance(caps, dict) or set(caps) != set(groups.values())
            or any(type(cap) is not int or not 1 <= cap <= max_api_calls for cap in caps.values())):
        raise ValueError("Every selected V4 group needs an explicit bounded API cap")
    preceding = set()
    for case in cases:
        if case["category"] in {"memory", "memory_prereq"}:
            dependencies = case.get("depends_on", [])
            if (not isinstance(dependencies, list) or not set(dependencies) <= preceding
                    or any(groups[dependency] != groups[case["id"]] for dependency in dependencies)):
                raise ValueError("Memory dependencies must precede their assessment under one shared budget group")
        preceding.add(case["id"])
    limits = limits or OfflineLimits(max_requests=max_api_calls, deadline_seconds=600)
    if not isinstance(limits, OfflineLimits) or limits.max_requests > max_api_calls:
        raise ValueError("Logical request limits must be explicit and cannot exceed the HTTP budget")
    factories = {} if environment_factories is None else environment_factories
    if not isinstance(factories, dict) or not set(factories) <= set(ids) or any(not callable(value) for value in factories.values()):
        raise ValueError("Environment factories must be explicitly bound to selected case IDs")
    output = Path(output_dir).expanduser().resolve()
    if any(output.is_relative_to(root) for root in {profile.source, resolve_data_dir(), (Path.home() / ".aster").resolve()}):
        raise ValueError("V4 evidence must remain outside the source user Runtime")
    if output.exists():
        raise ValueError("Existing V4 campaign evidence cannot be resumed or replayed")
    profile.validate()
    public = _public_profile(profile)
    binding = hashlib.sha256(json.dumps({"profile": public, "cases": cases, "max_output_tokens": max_output_tokens,
                                         "limits": to_dict(limits), "case_caps": caps, "case_groups": groups},
                                        sort_keys=True, allow_nan=False).encode()).hexdigest()
    output.mkdir(parents=True)
    with FileLock(output / "campaign.lock", timeout=.1):
        http_budget = V4HTTPCallBudget(output / "http-budget.json", maximum=max_api_calls, binding=binding,
                                      case_caps=caps, case_groups=groups)
        test_only = _test_transport_factory is not None
        protocol_budget = _LiveProtocolBudget(limits, http_budget=http_budget, path=output / "protocol-budget.json",
                                             cancel=cancel, test_only=test_only)
        report = {"schema_version": 1, "mode": "test_transport" if test_only else "live_current_model",
                  "model_quality": "unsupported", "official_score": False,
                  "paid_calls_authorized": not test_only, "profile": public, "dataset_binding": binding,
                  "max_api_calls": max_api_calls, "max_output_tokens": max_output_tokens, "metadata_requests": 0,
                  "planned_cases": len(cases), "planned_budget_groups": len(caps), "case_api_caps": caps,
                  "case_groups": groups, "results": [], "totals": {}, "model_api_calls": 0,
                  "full_leaderboard_score": None}
        _write(output / "results.json", report)
        started, fatal, completed = time.monotonic(), None, {}
        for case in cases:
            drivers = []
            before_api = len(http_budget.snapshot()["calls"])
            if fatal is not None or not protocol_budget.check() or before_api >= max_api_calls:
                result = {"id": case["id"], "status": "not_run", "reason": fatal or protocol_budget.stopped or "http_budget_exhausted",
                          "model_quality": "unsupported", "model_api_calls": 0}
            elif http_budget.remaining_group(case["id"]) <= 0:
                result = {"id": case["id"], "status": "not_run", "reason": "case_group_budget_exhausted",
                          "model_quality": "unsupported", "model_api_calls": 0}
            elif any(completed.get(dependency) != "prerequisite_completed" for dependency in case.get("depends_on", [])):
                result = {"id": case["id"], "status": "not_run", "reason": "memory_dependency_not_completed",
                          "model_quality": "unsupported", "model_api_calls": 0}
            elif case["category"].startswith("multi_turn_") and (issues := reference_admission_issues(case)):
                result = {"id": case["id"], "status": "blocked_dataset_conflict", "reference_admission_issues": issues,
                          "model_quality": "unsupported", "model_api_calls": 0}
            else:
                def driver_factory(_environment, _protocol_budget, *, current_id=case["id"], current_drivers=drivers):
                    driver = _CurrentModelDriver(profile, http_budget, current_id, protocol_budget=_protocol_budget,
                                                 max_output_tokens=max_output_tokens,
                                                 test_transport_factory=_test_transport_factory)
                    current_drivers.append(driver)
                    return driver

                try:
                    result = await _run_case_protocol(case, None,
                        output / "cases" / hashlib.sha256(case["id"].encode()).hexdigest()[:24],
                        limits=limits, environment_factory=factories.get(case["id"]), cancel=cancel,
                        shared_budget=protocol_budget, driver_factory=driver_factory,
                        execution_mode="test_transport" if test_only else "live_current_model",
                        model_quality="unsupported" if test_only else "evaluated",
                        model_api_counter=lambda: 0 if test_only else len(http_budget.snapshot()["calls"]))
                except Exception as error:  # noqa: BLE001 - durable per-case receipt without raw error text.
                    result = {"id": case["id"], "status": "case_error", "error_type": type(error).__name__,
                              "model_quality": "unsupported", "model_api_calls": 0 if test_only else len(http_budget.snapshot()["calls"]) - before_api}
                    if getattr(error, "category", None) in {"authentication", "endpoint"}:
                        fatal = "fatal_provider_failure"
                finally:
                    for driver in drivers:
                        await driver.aclose()
                failures = [failure for driver in drivers for failure in driver.failures]
                if failures:
                    result["model_failures"] = failures
                    if any(failure["category"] != "budget" for failure in failures):
                        result["status"] = "case_error"
                        result["model_quality"] = "unsupported"
                    if any(failure["category"] in {"authentication", "endpoint"} for failure in failures):
                        fatal = "fatal_provider_failure"
                rows = [row for row in http_budget.snapshot()["calls"] if row["case_id"] == case["id"]]
                result["http_attempts"] = len(rows)
                result["http_status_codes"] = [row.get("status_code") for row in rows]
                if any(row.get("status_code") in {401, 402, 403} for row in rows):
                    fatal = "authentication_or_balance_failure"
            result["verification_result"] = _status(result)
            completed[case["id"]] = result["verification_result"]
            result["budget_group"] = groups[case["id"]]
            result["case_provenance"] = case.get("provenance", {})
            result["result"] = ("unsupported" if test_only and result["verification_result"] not in {
                "not_run", "blocked_dataset_conflict", "case_error"} else result["verification_result"])
            receipts = output / "receipts"
            receipts.mkdir(exist_ok=True)
            receipt = receipts / (hashlib.sha256(case["id"].encode()).hexdigest()[:24] + ".json")
            if receipt.exists():
                raise ValueError("Completed V4 case receipts must never be overwritten")
            _write(receipt, {"dataset_binding": binding, "result": result})
            report["results"].append(result)
            report["totals"] = dict(Counter(row["result"] for row in report["results"]))
            report["model_quality"] = ("evaluated" if not test_only and any(
                row.get("model_quality") == "evaluated" for row in report["results"]) else "unsupported")
            report["http_transport_attempts"] = len(http_budget.snapshot()["calls"])
            report["model_api_calls"] = 0 if test_only else report["http_transport_attempts"]
            report["logical_budget"] = protocol_budget.snapshot()
            protocol_budget._persist()
            _write(output / "results.json", report)
        report["elapsed_seconds"] = round(time.monotonic() - started, 4)
        report["http_transport_attempts"] = len(http_budget.snapshot()["calls"])
        report["model_api_calls"] = 0 if test_only else report["http_transport_attempts"]
        report["logical_budget"] = protocol_budget.snapshot()
        protocol_budget._persist()
        _write(output / "results.json", report)
        return report
