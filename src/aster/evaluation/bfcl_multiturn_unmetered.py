"""One-shot Multi-Turn evaluation without an artificial API request quota.

The caller explicitly authorizes unlimited requests for the selected tasks.
BFCL still permits at most twenty model steps in each user turn. Every actual
HTTP attempt is durably recorded before dispatch, with an allowlisted witness
that deepseek-flash thinking is disabled. No provider is selected on import.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from pathlib import Path

from aster.evaluation.bfcl_current import CapturedProfile, HTTPCallBudget, capture_current_profile
from aster.evaluation.bfcl_v4_live import (
    _CurrentModelDriver,
    _LiveProtocolBudget,
    _public_profile,
    _status,
    _write,
)
from aster.evaluation.bfcl_v4_runner import OfflineLimits, _run_case_protocol, reference_admission_issues
from aster.platform.filesystem import FileLock
from aster.providers.deepseek import ProviderError

OFFICIAL_MAX_STEPS_PER_TURN = 20
MAX_OUTPUT_TOKENS = 8192
CASE_DEADLINE_SECONDS = 3600
CASE_TOOL_SAFETY_LIMIT = 10000
INHERITED_RUNTIME_SAFETY = {
    "max_total_tokens_per_user_run": 300000,
    "duplicate_limit": 3,
    "context_tokens": 96000,
    "reserve_tokens": 4096,
    "semantic_compaction": False,
    "model_retries": 0,
}
SUBCATEGORIES = ("multi_turn_base", "multi_turn_miss_func", "multi_turn_miss_param", "multi_turn_long_context")
A6_WIRE_POLICY = "a6_deepseek_v4_flash_none_v1"
A6_PROVIDER_ID = "provider-cd3755c2b87245fc9705011b4cef067d"
A6_GATEWAY_FAILURE_POLICY = "a6_known_gateway_task_isolation_v1"


def _agent_policy_identity(policy):
    """Bind the opt-in prompt bytes without changing legacy receipt shapes."""
    if policy not in {None, "readiness_state_v1", "readiness_prompt_v1", "capability_check_v1", "readiness_check_v2"}:
        raise ValueError("Unknown explicit Agent execution policy")
    if policy == "readiness_prompt_v1":
        from aster.engine.readiness_prompt import policy_identity

        return policy_identity()
    if policy == "capability_check_v1":
        from aster.engine.capability_gate import policy_identity

        return policy_identity()
    if policy == "readiness_check_v2":
        from aster.engine.readiness_gate_v2 import policy_identity

        return policy_identity()
    return None


def _validate_prompt_result(control, result):
    if control.get("agent_policy") in {"readiness_prompt_v1", "capability_check_v1", "readiness_check_v2"} and (
            result.get("agent_policy") != control["agent_policy"]
            or result.get("agent_policy_identity") != control.get("agent_policy_identity")):
        raise ValueError("Task prompt policy identity differs from admission")


def _wire_spec(policy):
    """Finite policies only; legacy absence retains the original native wire."""
    if policy is None:
        return {"protocol": "deepseek_responses", "base_url": "https://api.deepseek.com",
                "model": "deepseek-flash", "reasoning_effort": "none"}
    if policy == A6_WIRE_POLICY:
        return {"provider_id": A6_PROVIDER_ID, "protocol": "openai_responses",
                "base_url": "https://api.a6api.com/v1", "model": "deepseek-v4-flash",
                "reasoning_effort": "none"}
    raise ValueError("Unknown thinking-disabled wire policy")


def _matches_wire_profile(profile, policy):
    return all(getattr(profile, name, None) == value for name, value in _wire_spec(policy).items())


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _suffix(identifier):
    return hashlib.sha256(identifier.encode()).hexdigest()[:24]


def _profile_guard(profile):
    return _digest({"public": _public_profile(profile), "credential_binding": profile.credential_digest})


class UnmeteredMultiTurnHTTPBudget(HTTPCallBudget):
    """Count requests without a campaign quota; enforce only protocol bounds.

    A task with N user turns can make no more than 20*N model requests under
    the official step rule. This derived upper bound is not a separate task
    quota. The aggregate ledger has maximum=null and no cross-task reservation.
    """

    def __init__(self, path, *, binding, case_step_bounds, max_output_tokens=MAX_OUTPUT_TOKENS, create=False,
                 wire_policy=None):
        if (not isinstance(case_step_bounds, dict) or not case_step_bounds
                or any(not isinstance(key, str) or not key or type(value) is not int or value < 20
                       or value % OFFICIAL_MAX_STEPS_PER_TURN for key, value in case_step_bounds.items())
                or max_output_tokens != MAX_OUTPUT_TOKENS):
            raise ValueError("Explicit official step bounds and fixed output size are required")
        self.path, self.maximum = Path(path), None
        self.case_step_bounds = dict(case_step_bounds)
        self.case_groups = {identifier: identifier for identifier in self.case_step_bounds}
        self.case_caps = dict.fromkeys(self.case_step_bounds)
        self.max_output_tokens = max_output_tokens
        self.wire_policy, self.wire_spec = wire_policy, _wire_spec(wire_policy)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock():
            if create:
                if self.path.exists():
                    raise ValueError("Existing campaign HTTP evidence cannot be reset")
                document = {"schema_version": 1, "maximum": None, "binding": binding,
                    "authorized_unlimited_api": True, "api_unlimited": True,
                    "case_caps": self.case_caps, "case_groups": self.case_groups,
                    "official_max_steps_per_turn": OFFICIAL_MAX_STEPS_PER_TURN,
                    "case_official_step_bounds": self.case_step_bounds,
                    "wire_max_output_tokens": self.max_output_tokens, "calls": []}
                if wire_policy is not None:
                    document["wire_policy"] = wire_policy
                _write(self.path, document)
            else:
                document = self.snapshot_unlocked()
                if (document.get("binding") != binding or document.get("maximum") is not None
                        or document.get("authorized_unlimited_api") is not True
                        or document.get("api_unlimited") is not True
                        or document.get("case_groups") != self.case_groups
                        or document.get("case_caps") != self.case_caps
                        or document.get("case_official_step_bounds") != self.case_step_bounds
                        or document.get("wire_max_output_tokens") != self.max_output_tokens
                        or document.get("wire_policy") != wire_policy):
                    raise ValueError("Campaign unlimited API/protocol identity changed")

    def remaining_group(self, case_id):
        if case_id not in self.case_step_bounds:
            raise ProviderError("Task is outside the admitted protocol", category="budget")
        document = self.snapshot()
        used = sum(call["case_id"] == case_id for call in document["calls"])
        return self.case_step_bounds[case_id] - used

    async def before_request(self, request, *, profile, case_id):
        profile.validate()
        if (not _matches_wire_profile(profile, self.wire_policy) or request.method != "POST"
                or str(request.url) != profile.base_url + "/responses"):
            raise ProviderError("Unexpected model endpoint or thinking policy", category="endpoint")
        try:
            body = json.loads(request.content)
            valid = (body.get("model") == self.wire_spec["model"]
                     and body.get("reasoning") == {"effort": "none"}
                     and type(body.get("max_output_tokens")) is int
                     and body["max_output_tokens"] == self.max_output_tokens
                     and (self.wire_policy is None or body.get("store") is False))
        except (ValueError, TypeError, AttributeError, UnicodeError):
            valid = False
        if not valid:
            raise ProviderError("Wire request did not explicitly disable thinking", category="protocol")
        if case_id not in self.case_step_bounds:
            raise ProviderError("Task is outside the admitted protocol", category="budget")
        with self.lock():
            document = self.snapshot_unlocked()
            used = sum(call["case_id"] == case_id for call in document["calls"])
            if used >= self.case_step_bounds[case_id]:
                raise ProviderError("Official multi-turn step bound exhausted", category="budget")
            index = len(document["calls"])
            call = {"case_id": case_id, "budget_group": case_id,
                "method": request.method, "path": request.url.path, "started_at": time.time(),
                "status": "outcome_unknown", "thinking_disabled_verified": True,
                "wire_model": self.wire_spec["model"], "wire_reasoning_effort": "none",
                "wire_max_output_tokens": self.max_output_tokens}
            if self.wire_policy is not None:
                call.update(wire_policy=self.wire_policy, wire_protocol=profile.protocol, wire_store=False)
            document["calls"].append(call)
            _write(self.path, document)
        request.extensions["aster_bfcl_api_index"] = index


def initialize_campaign(cases, campaign_dir, *, profile, max_api_calls=None, max_output_tokens=MAX_OUTPUT_TOKENS,
                        authorized_unlimited_api=True, binding=None, _test_only=False, wire_policy=None,
                        http_dns_fallback="off", provider_failure_policy=None, agent_policy=None):
    """Admit exactly one explicit no-quota, thinking-disabled campaign locally."""
    if not isinstance(profile, CapturedProfile):
        raise TypeError("A read-only captured profile is required")
    prompt_identity = _agent_policy_identity(agent_policy)
    if max_api_calls is not None or authorized_unlimited_api is not True:
        raise ValueError("This mode requires explicit unlimited API authorization")
    if not _matches_wire_profile(profile, wire_policy):
        raise ValueError("The captured profile does not match the explicit thinking-disabled wire policy")
    if http_dns_fallback not in {"off", "google"} or wire_policy is None and http_dns_fallback != "off":
        raise ValueError("An explicit supported compatible-provider DNS policy is required")
    if (provider_failure_policy not in {None, A6_GATEWAY_FAILURE_POLICY}
            or provider_failure_policy is not None and wire_policy != A6_WIRE_POLICY):
        raise ValueError("An explicit supported provider failure policy is required")
    capacity = profile.capabilities.get("max_output_tokens")
    if (type(max_output_tokens) is not int or max_output_tokens != MAX_OUTPUT_TOKENS
            or type(capacity) is int and capacity < max_output_tokens):
        raise ValueError("The admitted output size must be exactly 8192 and supported by the captured model")
    if (not isinstance(cases, list) or not cases
            or any(not isinstance(case, dict) or case.get("category") not in SUBCATEGORIES
                   or not isinstance(case.get("question"), list) or not case["question"] for case in cases)):
        raise ValueError("Only explicit complete Multi-Turn tasks are admitted")
    if type(_test_only) is not bool:
        raise ValueError("Test transport admission must be explicit")
    ids = [case["id"] for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("Tasks must have distinct IDs")
    root = Path(campaign_dir).resolve()
    if root.is_relative_to(profile.source) or root.is_relative_to(Path.home() / ".aster"):
        raise ValueError("Evidence cannot be written into user Runtime")
    if not _test_only:
        if any(not (root / name).is_file() for name in ("source-manifest.json", "campaign-plan.json")):
            raise ValueError("An approved plan and immutable source snapshot are required")
        plan = _read(root / "campaign-plan.json")
        planned = [identifier for group in plan["groups"] for identifier in group["entry_ids"]]
        if (planned != ids or plan.get("authorized_unlimited_api") is not True
                or plan.get("api_unlimited") is not True
                or plan.get("authorized_api_call_limit") is not None
                or plan.get("max_output_tokens") != max_output_tokens
                or plan.get("inherited_runtime_safety") != INHERITED_RUNTIME_SAFETY
                or plan.get("case_deadline_seconds") != CASE_DEADLINE_SECONDS
                or plan.get("case_tool_safety_limit") != CASE_TOOL_SAFETY_LIMIT
                or plan.get("wire_policy") != wire_policy
                or wire_policy is not None and plan.get("http_dns_fallback") != http_dns_fallback
                or plan.get("provider_failure_policy") != provider_failure_policy
                or plan.get("agent_policy") != agent_policy
                or plan.get("agent_policy_identity") != prompt_identity):
            raise ValueError("Execution tasks/policy differ from the approved no-quota plan")
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(root / "campaign.lock", timeout=.1):
        if (root / "execution-control.json").exists() or (root / "results").exists():
            raise ValueError("Campaign execution cannot be initialized twice")
        profile.validate()
        issues = {case["id"]: reference_admission_issues(case) for case in cases}
        bounds = {case["id"]: OFFICIAL_MAX_STEPS_PER_TURN * len(case["question"]) for case in cases}
        policy = {"authorized_unlimited_api": True, "api_unlimited": True, "max_api_calls": None,
            "per_case_maximum": None, "future_valid_case_reserve": 0,
            "official_max_steps_per_turn": OFFICIAL_MAX_STEPS_PER_TURN, "case_official_step_bounds": bounds,
            "max_output_tokens": max_output_tokens, "case_deadline_seconds": CASE_DEADLINE_SECONDS,
            "case_tool_safety_limit": CASE_TOOL_SAFETY_LIMIT,
            "inherited_runtime_safety": dict(INHERITED_RUNTIME_SAFETY)}
        if wire_policy is not None:
            policy["wire_policy"] = wire_policy
            policy["http_dns_fallback"] = http_dns_fallback
        if provider_failure_policy is not None:
            policy["provider_failure_policy"] = provider_failure_policy
        if agent_policy is not None:
            policy["agent_policy"] = agent_policy
        if prompt_identity is not None:
            policy["agent_policy_identity"] = prompt_identity
        calculated = _digest({"profile": _public_profile(profile), "cases": cases, "policy": policy,
                              "test_only": _test_only})
        if binding is not None and binding != calculated:
            raise ValueError("Explicit campaign binding does not match selected execution inputs")
        control = {"schema_version": 1, "dataset_binding": calculated, "profile": _public_profile(profile),
            "profile_guard": _profile_guard(profile), "source_runtime": str(profile.source),
            "case_ids": ids, "cases_sha256": _digest(cases), "reference_admission_issues": issues,
            "test_only": _test_only, "started_at": time.time(), "fatal": None, **policy}
        for name in ("source-manifest.json", "campaign-plan.json"):
            if (root / name).is_file():
                control[name + "_sha256"] = hashlib.sha256((root / name).read_bytes()).hexdigest()
        (root / "results/receipts").mkdir(parents=True)
        (root / "results/intents").mkdir()
        _write(root / "private-cases.json", cases)
        _write(root / "execution-control.json", control)
        UnmeteredMultiTurnHTTPBudget(root / "results/http-budget.json", binding=calculated,
            case_step_bounds=bounds, max_output_tokens=max_output_tokens, create=True, wire_policy=wire_policy)
        _merge_report(root, control)
    return control


def _verified_control(root):
    control = _read(root / "execution-control.json")
    cases = _read(root / "private-cases.json")
    bounds = {case["id"]: OFFICIAL_MAX_STEPS_PER_TURN * len(case["question"]) for case in cases}
    if (control.get("max_api_calls") is not None or control.get("authorized_unlimited_api") is not True
            or control.get("api_unlimited") is not True or control.get("per_case_maximum") is not None
            or control.get("future_valid_case_reserve") != 0
            or control.get("case_official_step_bounds") != bounds
            or control.get("official_max_steps_per_turn") != OFFICIAL_MAX_STEPS_PER_TURN
            or control.get("max_output_tokens") != MAX_OUTPUT_TOKENS
            or control.get("case_deadline_seconds") != CASE_DEADLINE_SECONDS
            or control.get("case_tool_safety_limit") != CASE_TOOL_SAFETY_LIMIT
            or control.get("inherited_runtime_safety") != INHERITED_RUNTIME_SAFETY
            or any(control.get("profile", {}).get(key) != value
                   for key, value in _wire_spec(control.get("wire_policy")).items())
            or control.get("http_dns_fallback", "off") not in {"off", "google"}
            or control.get("wire_policy") is None and control.get("http_dns_fallback", "off") != "off"
            or control.get("provider_failure_policy") not in {None, A6_GATEWAY_FAILURE_POLICY}
            or control.get("provider_failure_policy") is not None and control.get("wire_policy") != A6_WIRE_POLICY
            or control.get("agent_policy") not in {
                None, "readiness_state_v1", "readiness_prompt_v1", "capability_check_v1", "readiness_check_v2"}):
        raise ValueError("Campaign no-quota/thinking policy changed")
    if control.get("agent_policy_identity") != _agent_policy_identity(control.get("agent_policy")):
        raise ValueError("Campaign prompt policy identity changed")
    if _digest(cases) != control["cases_sha256"] or [case["id"] for case in cases] != control["case_ids"]:
        raise ValueError("Campaign task binding changed")
    for name in ("source-manifest.json", "campaign-plan.json"):
        expected = control.get(name + "_sha256")
        if expected and hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise ValueError("Campaign source/plan binding changed")
        if name == "campaign-plan.json" and expected and not control["test_only"]:
            plan = _read(root / name)
            if (plan.get("provider_failure_policy") != control.get("provider_failure_policy")
                    or plan.get("agent_policy") != control.get("agent_policy")
                    or plan.get("agent_policy_identity") != control.get("agent_policy_identity")
                    or plan.get("wire_policy") != control.get("wire_policy")
                    or control.get("wire_policy") is not None
                    and plan.get("http_dns_fallback") != control.get("http_dns_fallback")):
                raise ValueError("Campaign provider policy differs from its bound plan")
    if not control["test_only"]:
        workspace = Path(__file__).resolve().parents[3]
        for entry in _read(root / "source-manifest.json")["files"]:
            relative = Path(entry["file"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Unsafe source snapshot path")
            if hashlib.sha256((workspace / relative).read_bytes()).hexdigest() != entry["sha256"]:
                raise ValueError("Execution sources changed after campaign admission")
    return control, cases


def _receipt_path(root, identifier):
    return root / "results/receipts" / (_suffix(identifier) + ".json")


def read_task_receipt(campaign_dir, case_id):
    """Read the exact durable local outcome; never start or resume execution."""
    root = Path(campaign_dir).resolve()
    control, _cases = _verified_control(root)
    if case_id not in control["case_ids"]:
        raise ValueError("Task is outside the campaign")
    receipt = _read(_receipt_path(root, case_id))
    if (receipt.get("dataset_binding") != control["dataset_binding"]
            or receipt.get("result", {}).get("id") != case_id):
        raise ValueError("Task receipt identity differs")
    _validate_prompt_result(control, receipt["result"])
    return receipt["result"]


def _merge_report(root, control):
    rows = []
    for identifier in control["case_ids"]:
        path = _receipt_path(root, identifier)
        if path.is_file():
            receipt = _read(path)
            if receipt.get("dataset_binding") != control["dataset_binding"]:
                raise ValueError("Task receipt campaign binding differs")
            _validate_prompt_result(control, receipt["result"])
            rows.append(receipt["result"])
    http = _read(root / "results/http-budget.json")
    report = {"schema_version": 1, "mode": "test_transport" if control["test_only"] else "live_current_model",
              "model_quality": "unsupported" if control["test_only"] else "evaluated", "official_score": False,
              "paid_calls_authorized": not control["test_only"], "profile": control["profile"],
              "dataset_binding": control["dataset_binding"], "max_api_calls": control["max_api_calls"],
              "max_output_tokens": control["max_output_tokens"], "metadata_requests": 0,
              "planned_cases": len(control["case_ids"]), "results": rows,
              "totals": dict(Counter(row["result"] for row in rows)), "case_api_caps": http["case_caps"],
              "case_groups": http["case_groups"], "model_api_calls": 0 if control["test_only"] else len(http["calls"]),
              "http_transport_attempts": len(http["calls"]),
              "elapsed_seconds": round(time.time() - control["started_at"], 4), "full_leaderboard_score": None,
              "orchestrator": "promptfoo", "new_judge_api_calls": 0,
              "authorized_unlimited_api": True, "api_unlimited": True, "thinking_disabled": True,
              "official_max_steps_per_turn": OFFICIAL_MAX_STEPS_PER_TURN,
              "case_official_step_bounds": control["case_official_step_bounds"],
              "per_case_api_call_cap": None, "future_case_reservation": 0,
              "case_deadline_seconds": control["case_deadline_seconds"],
              "case_tool_safety_limit": control["case_tool_safety_limit"],
              "inherited_runtime_safety": control["inherited_runtime_safety"],
              "wire_policy_verified_calls": sum(call.get("thinking_disabled_verified") is True for call in http["calls"])}
    if control.get("wire_policy") is not None:
        report["wire_policy"] = control["wire_policy"]
        report["http_dns_fallback"] = control["http_dns_fallback"]
    if control.get("provider_failure_policy") is not None:
        report["provider_failure_policy"] = control["provider_failure_policy"]
    if control.get("agent_policy") is not None:
        report["agent_policy"] = control["agent_policy"]
    if control.get("agent_policy_identity") is not None:
        report["agent_policy_identity"] = control["agent_policy_identity"]
    _write(root / "results/results.json", report)
    return report


def _termination_kind(result, protocol_budget):
    if result.get("status") == "blocked_dataset_conflict":
        return "dataset_conflict"
    if result.get("status") == "case_error":
        return "provider_or_infrastructure_error"
    if result.get("status") == "not_run":
        return "not_run"
    stop = getattr(protocol_budget, "stopped", None)
    if stop == "case_deadline_exhausted" or any(run.get("runtime_status") == "timed_out" for run in result.get("runs", [])):
        return "safety_deadline"
    if stop == "case_tool_calls_exhausted":
        return "safety_tool_call_limit"
    if any(run.get("runtime_status") == "cancelled" for run in result.get("runs", [])):
        return "cancelled"
    if any(isinstance(run.get("runtime_diagnostic"), dict)
           and run["runtime_diagnostic"].get("reason") == "model_iteration_limit" for run in result.get("runs", [])):
        return "official_per_turn_step_limit"
    if result.get("agent_policy") in {
            "readiness_state_v1", "readiness_prompt_v1", "capability_check_v1", "readiness_check_v2"} and any(
            run.get("runtime_status") == "budget_exhausted"
            and (run.get("runtime_budget", {}).get("iterations", 0) >= OFFICIAL_MAX_STEPS_PER_TURN
                 or run.get("runtime_budget", {}).get("model_requests", 0) >= OFFICIAL_MAX_STEPS_PER_TURN)
            for run in result.get("runs", [])):
        return "official_per_turn_step_limit"
    if any(message.get("role") == "tool" and message.get("metadata", {}).get("error_category") == "loop_detected"
           for message in result.get("messages", [])):
        return "repeated_no_progress"
    for run in result.get("runs", []):
        usage = run.get("usage", [])
        if (run.get("runtime_status") == "budget_exhausted" and usage
                and all(type(item.get("input_tokens")) is int and type(item.get("output_tokens")) is int
                        for item in usage)
                and sum(item["input_tokens"] + item["output_tokens"] for item in usage) >= 300000):
            return "token_budget"
    if any(run.get("runtime_status") == "budget_exhausted" for run in result.get("runs", [])):
        return "runtime_safety_limit_unclassified"
    return "completed" if result.get("protocol_result") == "pass" else "task_result"


async def run_one_current_multiturn(case_id, campaign_dir, *, _profile=None, _test_transport_factory=None):
    """Execute exactly one admitted full task; promptfoo calls this serially.

    A known completed receipt is a read. An intent without a receipt is an
    unknown prior outcome and can never start the task again.
    """
    root = Path(campaign_dir).resolve()
    with FileLock(root / "campaign.lock", timeout=.1):
        control, cases = _verified_control(root)
        if case_id not in control["case_ids"]:
            raise ValueError("Task is outside the immutable campaign")
        test_only = control["test_only"]
        if test_only != (_test_transport_factory is not None):
            raise ValueError("Test and paid transports cannot be interchanged")
        receipt_path = _receipt_path(root, case_id)
        if receipt_path.exists():
            receipt = _read(receipt_path)
            if receipt.get("dataset_binding") != control["dataset_binding"]:
                raise ValueError("Known receipt has a different binding")
            return receipt["result"]
        expected_next = next((identifier for identifier in control["case_ids"]
                              if not _receipt_path(root, identifier).exists()), None)
        if expected_next != case_id:
            raise ValueError("Only the next serial promptfoo task may begin")
        intent_path = root / "results/intents" / (_suffix(case_id) + ".json")
        if intent_path.exists():
            raise ValueError("A task with an unknown prior outcome cannot be replayed")
        if not test_only and _profile is not None:
            raise ValueError("Paid dispatch resolves only the campaign-pinned provider")
        profile = _profile or capture_current_profile(control["source_runtime"],
            provider_id=control["profile"]["provider_id"], model=control["profile"]["model"],
            reasoning_effort=control["profile"]["reasoning_effort"])
        if _profile_guard(profile) != control["profile_guard"]:
            raise ValueError("The campaign provider/model/credential identity changed")
        profile.validate()
        case = next(item for item in cases if item["id"] == case_id)
        bound = control["case_official_step_bounds"][case_id]
        budget = UnmeteredMultiTurnHTTPBudget(root / "results/http-budget.json", binding=control["dataset_binding"],
            case_step_bounds=control["case_official_step_bounds"], max_output_tokens=control["max_output_tokens"],
            wire_policy=control.get("wire_policy"))
        prior_http = len(budget.snapshot()["calls"])
        issues = control["reference_admission_issues"][case_id]
        _write(intent_path, {"dataset_binding": control["dataset_binding"], "task_id": case_id,
            "started_at": time.time(), "api_call_cap": None, "prior_http_attempts": prior_http,
            "official_model_step_bound": bound, "authorized_unlimited_api": True,
            "status": "execution_intent_no_automatic_replay"})
        drivers = []
        protocol_budget = None
        if issues:
            result = {"id": case_id, "status": "blocked_dataset_conflict", "reference_admission_issues": issues,
                      "model_quality": "unsupported", "model_api_calls": 0}
        elif control.get("fatal"):
            result = {"id": case_id, "status": "not_run", "reason": control["fatal"],
                      "model_quality": "unsupported", "model_api_calls": 0}
        else:
            limits = OfflineLimits(max_requests=bound, max_tool_calls=CASE_TOOL_SAFETY_LIMIT,
                max_steps_per_turn=OFFICIAL_MAX_STEPS_PER_TURN, deadline_seconds=CASE_DEADLINE_SECONDS)
            protocol_budget = _LiveProtocolBudget(limits, http_budget=budget,
                path=root / "results/protocol-budgets" / (_suffix(case_id) + ".json"), test_only=test_only)
            execution_exception_kind = None

            def factory(_environment, _protocol):
                driver = _CurrentModelDriver(profile, budget, case_id, protocol_budget=_protocol,
                    max_output_tokens=control["max_output_tokens"], test_transport_factory=_test_transport_factory,
                    dns_fallback=control.get("http_dns_fallback", "off"))
                drivers.append(driver)
                return driver

            try:
                result = await _run_case_protocol(case, None, root / "results/cases" / _suffix(case_id),
                    limits=limits, shared_budget=protocol_budget, driver_factory=factory,
                    execution_mode="test_transport" if test_only else "live_current_model",
                    model_quality="unsupported" if test_only else "evaluated",
                    model_api_counter=lambda: 0 if test_only else len(budget.snapshot()["calls"]),
                    **({"agent_policy": control["agent_policy"]} if control.get("agent_policy") is not None else {}))
            except Exception as error:  # noqa: BLE001 - never persist raw provider exception prose.
                execution_exception_kind = type(error).__name__
                result = {"id": case_id, "status": "case_error", "error_type": type(error).__name__,
                          "model_quality": "unsupported", "model_api_calls": 0 if test_only else
                          len(budget.snapshot()["calls"]) - prior_http}
                control["fatal"] = "execution_infrastructure_error"
            finally:
                for driver in drivers:
                    await driver.aclose()
            failures = [failure for driver in drivers for failure in driver.failures]
            # Engine admission still enforces official per-turn steps and
            # the declared token/duplicate/deadline/tool safety protections.
            if (any(run.get("runtime_status") == "budget_exhausted" for run in result.get("runs", []))
                    and not any(failure["category"] == "budget" for failure in failures)):
                termination = _termination_kind(result, protocol_budget)
                result["termination_kind"] = termination
                failures.append({"type": "AsterRuntimeBudgetStop", "category": "budget", "status_code": None,
                    "origin": "official_per_turn_steps" if termination == "official_per_turn_step_limit"
                              else "runtime_safety_limit", "termination_kind": termination})
            if failures:
                result["model_failures"] = failures
                nonbudget = [failure for failure in failures if failure["category"] != "budget"]
                if nonbudget:
                    result["status"], result["model_quality"] = "case_error", "unsupported"
                    known_calls = [call for call in budget.snapshot()["calls"] if call["case_id"] == case_id]
                    task_local_incomplete = (
                        execution_exception_kind in {None, "ProviderError"}
                        and all(failure["category"] == "incomplete" for failure in nonbudget)
                        and bool(known_calls)
                        and all(call.get("status_code") == 200 and call.get("status") == "response_headers_received"
                                for call in known_calls)
                    )
                    if task_local_incomplete:
                        # A known HTTP 200 with incomplete model output cannot
                        # execute partial arguments or retry this task. A later,
                        # independent task may still use the remaining budget.
                        result["task_local_provider_incomplete"] = True
                        control["fatal"] = None
                    else:
                        control["fatal"] = "provider_infrastructure_error"
        if protocol_budget is not None:
            protocol_budget._persist()
        calls = [call for call in budget.snapshot()["calls"] if call["case_id"] == case_id]
        result["http_attempts"] = len(calls)
        result["http_status_codes"] = [call.get("status_code") for call in calls]
        if any(call.get("status") == "outcome_unknown" for call in calls):
            control["fatal"] = "unknown_provider_request_outcome"
        result["verification_result"] = _status(result)
        result["result"] = ("unsupported" if test_only and result["verification_result"] not in {
            "blocked_dataset_conflict", "not_run", "case_error"} else result["verification_result"])
        result["budget_group"] = case_id
        result["case_provenance"] = case.get("provenance", {})
        result["allocated_api_call_cap"] = None
        result["authorized_unlimited_api"] = True
        result["official_model_step_bound"] = bound
        if control.get("agent_policy") is not None:
            result["agent_policy"] = control["agent_policy"]
        if control.get("agent_policy_identity") is not None:
            result["agent_policy_identity"] = control["agent_policy_identity"]
        if "termination_kind" not in result:
            result["termination_kind"] = _termination_kind(result, protocol_budget)
        _write(receipt_path, {"dataset_binding": control["dataset_binding"], "result": result})
        _write(root / "execution-control.json", control)
        _merge_report(root, control)
        return result


def finalize_campaign(campaign_dir):
    """Collect durable outcomes; never execute or resume an uninvoked task."""
    root = Path(campaign_dir).resolve()
    with FileLock(root / "campaign.lock", timeout=.1):
        control, cases = _verified_control(root)
        for case in cases:
            identifier = case["id"]
            target = _receipt_path(root, identifier)
            if target.exists():
                continue
            intent = root / "results/intents" / (_suffix(identifier) + ".json")
            result = {"id": identifier, "status": "case_error" if intent.exists() else "not_run",
                      "reason": "unknown_prior_task_outcome" if intent.exists() else "promptfoo_did_not_invoke_task",
                      "result": "case_error" if intent.exists() else "not_run", "model_quality": "unsupported",
                      "model_api_calls": 0 if control["test_only"] else sum(call["case_id"] == identifier for call in
                          _read(root / "results/http-budget.json")["calls"]),
                      "case_provenance": case.get("provenance", {}), "budget_group": identifier}
            if control.get("agent_policy_identity") is not None:
                result.update(agent_policy=control["agent_policy"],
                    agent_policy_identity=control["agent_policy_identity"])
            _write(target, {"dataset_binding": control["dataset_binding"], "result": result})
        return _merge_report(root, control)
