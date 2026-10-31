"""Read sealed compact-dev40 campaigns and compare saved evidence, offline only.

This module imports no model, Runtime, judge or DeepEval dependency. The private
reference answers are read only to derive evaluator-only turn call counts.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
from collections import Counter
from copy import deepcopy
from pathlib import Path
from statistics import median

from aster.evaluation.bfcl_multiturn_export import (
    SUBCATEGORIES,
    TOKEN_FIELDS,
    _digest,
    _hash,
    _read,
    validate_record,
)
from aster.evaluation.bfcl_multiturn_unmetered_export import load_unmetered_campaign

COMPACT_MANIFEST_SHA256 = "477aa07cff732f1158ce59138e5f1b4bf8f701c3f2de211a43a2c0a01f6d58c8"
DEVELOPMENT_SHA256 = "b1d7ec61a7ca160f21e1322d021435ab1f5ccb370bbcb03c1939e2c1be9f7381"
HOLDOUT_SHA256 = "8c40803ac4aed94e855a0d75a43611895bb743c5af9eef4013dd08afe3d05e2d"
MODEL_FIELDS = ("provider_id", "base_url", "protocol", "model", "reasoning_effort", "identity_revision")
COMPARABLE_POLICY_FIELDS = (
    "max_output_tokens", "max_api_calls", "per_case_maximum", "official_max_steps_per_turn",
    "case_official_step_bounds", "inherited_runtime_safety", "case_deadline_seconds", "case_tool_safety_limit",
)
SPLIT_RELATIVE = Path("evals/bfcl_multiturn/splits/compact100-v2")
PROMPT_POLICY = "readiness_prompt_v1"
PROMPT_SOURCE = "src/aster/engine/readiness_prompt.py"
GATE_POLICY = "capability_check_v1"
GATE_SOURCE = "src/aster/engine/capability_gate.py"
CLARIFY_POLICY = "readiness_check_v2"
CLARIFY_SOURCE = "src/aster/engine/readiness_gate_v2.py"
GATE_SOURCES = {GATE_POLICY: GATE_SOURCE, CLARIFY_POLICY: CLARIFY_SOURCE}
GATE_POLICIES = frozenset(GATE_SOURCES)
SUPPORTED_POLICIES = frozenset({"off", "readiness_state_v1", PROMPT_POLICY, *GATE_POLICIES})


def _saved_literals(root: Path, relative: str, names: set[str]) -> dict:
    """Read verified literal constants, without importing captured Python."""
    source = root / "as-run-source" / relative
    entries = [item for item in _read(root / "source-manifest.json")["files"] if item.get("file") == relative]
    if len(entries) != 1 or source.is_symlink():
        raise ValueError("Policy source must occur exactly once in the frozen manifest")
    payload = source.read_bytes()
    if hashlib.sha256(payload).hexdigest() != entries[0].get("sha256") or len(payload) != entries[0].get("bytes"):
        raise ValueError("Frozen policy source differs from its manifest")
    result = {}
    for node in ast.parse(payload.decode("utf-8-sig")).body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in names:
                if target.id in result:
                    raise ValueError("Frozen policy has duplicate literal assignments")
                result[target.id] = ast.literal_eval(node.value)
    if set(result) != names:
        raise ValueError("Frozen policy literal contract is incomplete")
    return result


def saved_gate_contract(root: Path, policy: str = GATE_POLICY) -> tuple[dict, str, str]:
    if policy not in GATE_SOURCES:
        raise ValueError("Unsupported frozen gate policy")
    native_identity, native_prefix = _saved_prompt_contract(root)
    constants = _saved_literals(root, GATE_SOURCES[policy], {"POLICY", "CHECK_SYSTEM"})
    if constants["POLICY"] != policy or not isinstance(constants["CHECK_SYSTEM"], str):
        raise ValueError("Unsupported frozen capability-check contract")
    return {"policy": policy,
            "gate_prompt_sha256": hashlib.sha256(constants["CHECK_SYSTEM"].encode("utf-8")).hexdigest(),
            "native_prompt_policy": PROMPT_POLICY, "native_prompt_sha256": native_identity["prompt_sha256"],
            "check_frequency": "once_per_user_turn", "parse_failure_policy": "fallback_to_native"}, constants["CHECK_SYSTEM"], native_prefix


def _saved_prompt_contract(root: Path) -> tuple[dict, str]:
    """Read a frozen literal without importing or executing captured source code."""
    source = root / "as-run-source" / PROMPT_SOURCE
    manifest = _read(root / "source-manifest.json")
    entries = [item for item in manifest["files"] if item.get("file") == PROMPT_SOURCE]
    if len(entries) != 1 or source.is_symlink():
        raise ValueError("Prompt policy source must be present exactly once in the frozen manifest")
    payload = source.read_bytes()
    if (hashlib.sha256(payload).hexdigest() != entries[0].get("sha256")
            or len(payload) != entries[0].get("bytes")):
        raise ValueError("Frozen prompt policy source differs from its manifest")
    constants = {}
    for node in ast.parse(payload.decode("utf-8-sig")).body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in {"POLICY", "SYSTEM_PREAMBLE"}:
                if target.id in constants:
                    raise ValueError("Frozen prompt identity contains duplicate assignments")
                constants[target.id] = ast.literal_eval(node.value)
    if constants.get("POLICY") != PROMPT_POLICY or not isinstance(constants.get("SYSTEM_PREAMBLE"), str):
        raise ValueError("Frozen prompt policy identity is not the supported literal contract")
    return {"policy": PROMPT_POLICY,
            "prompt_sha256": hashlib.sha256(constants["SYSTEM_PREAMBLE"].encode("utf-8")).hexdigest()}, constants["SYSTEM_PREAMBLE"]


def saved_prompt_identity(root: Path) -> dict:
    return _saved_prompt_contract(root)[0]


def validate_prompt_campaign(root: Path, plan: dict, control: dict, report: dict, rows: list[dict]) -> dict:
    """A prompt-only label must agree with source, receipts and actual run settings."""
    identity, prefix = _saved_prompt_contract(root)
    objects = [plan, control, report, *rows]
    if any(item.get("agent_policy") != PROMPT_POLICY or item.get("agent_policy_identity") != identity
           for item in objects):
        raise ValueError("Prompt policy identity is not bound consistently into the saved campaign")
    for row in rows:
        if row.get("agent_control_failure") is True:
            raise ValueError("A prompt-only task cannot contain a controller-contract failure")
        if any(not isinstance(item.get("system"), str) or not item["system"].startswith(prefix)
               for item in row.get("step_observations", [])):
            raise ValueError("Recorded model inputs do not contain the captured prompt policy")
        for run in row.get("runs", []):
            if (run.get("runtime_prompt_policy") != PROMPT_POLICY or run.get("runtime_task_control") != "off"
                    or run.get("agent_policy_identity") != identity
                    or run.get("task_control") is not None):
                raise ValueError("Prompt-only task has a different captured Runtime policy")
            receipts, purposes = run.get("model_request_receipts"), run.get("model_request_purposes")
            if (not isinstance(receipts, list) or len(receipts) > 20 or not isinstance(purposes, dict)
                    or set(purposes) - {"main"}
                    or any(type(value) is not int or value < 0 for value in purposes.values())
                    or sum(purposes.values()) != len(receipts)
                    or any(receipt.get("purpose") != "main" for receipt in receipts)
                    or run.get("runtime_budget", {}).get("model_requests") != len(receipts)):
                raise ValueError("Prompt-only model receipts must use main requests within the shared turn bound")
    return identity


def _gate_digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _gate_projection(run: dict) -> dict | None:
    """Only structural state and one-way identities enter framework metadata."""
    trace = run.get("capability_gate")
    if not isinstance(trace, dict):
        return None
    fields = ("index", "status", "parse_status", "user_turn_sha256", "input_sha256", "tools_sha256",
              "response_sha256", "request_number")
    requests = []
    for index, receipt in enumerate(run.get("model_request_receipts", [])):
        usage = run.get("usage", [])[index] if index < len(run.get("usage", [])) else {}
        requests.append({"purpose": receipt.get("purpose"), "request_number": receipt.get("request_number"),
            "status": receipt.get("status"), "recorded_usage": {field: (
                value if type(value := usage.get(field)) is int and value >= 0 else None) for field in TOKEN_FIELDS}})
    return {"turn_index": run["turn_index"], "runtime_status": run.get("runtime_status"),
        "policy": trace.get("policy"), "policy_identity": deepcopy(trace.get("policy_identity")),
        "checks": [{key: item.get(key) for key in fields if key in item} for item in trace.get("checks", [])],
        "check_requests": trace.get("check_requests"), "main_requests": trace.get("main_requests"),
        "model_request_purposes": deepcopy(run.get("model_request_purposes", {})),
        "model_requests": requests}


def validate_gate_campaign(root: Path, plan: dict, control: dict, report: dict, rows: list[dict]) -> dict:
    """Verify saved capability checks and actual tool-free request envelopes.

    A check's semantic answer remains a recorded Agent decision, not an oracle.
    The official checks determine task success; a fallback is never relabelled
    as a gate abstention just because its native answer happened to use no tool.
    """
    policy = control.get("agent_policy")
    identity, gate_system, native_prefix = saved_gate_contract(root, policy)
    terminal = {"missing_capability", "needs_clarification"} if policy == CLARIFY_POLICY else {"missing_capability"}
    if any(item.get("agent_policy") != policy or item.get("agent_policy_identity") != identity
           for item in (plan, control, report, *rows)):
        raise ValueError("Capability-check identity is not consistently bound into the saved campaign")
    for row in rows:
        if row.get("agent_control_failure") is True:
            raise ValueError("Capability checks cannot use task-control contract failures")
        observations = row.get("step_observations", [])
        if not isinstance(observations, list):
            raise TypeError("Capability-check observations must be a list")
        for run in row.get("runs", []):
            if (run.get("runtime_prompt_policy") != PROMPT_POLICY or run.get("runtime_task_control") != "off"
                    or run.get("runtime_capability_gate") != policy
                    or run.get("agent_policy_identity") != identity or run.get("task_control") is not None):
                raise ValueError("Capability-check Runtime policy differs from admission")
            receipts, purposes = run.get("model_request_receipts"), run.get("model_request_purposes")
            if (not isinstance(receipts, list) or len(receipts) > 20 or not isinstance(purposes, dict)
                    or set(purposes) - {"main", "capability_check"}
                    or any(type(value) is not int or value < 0 for value in purposes.values())
                    or dict(Counter(item.get("purpose") for item in receipts)) != purposes
                    or run.get("runtime_budget", {}).get("model_requests") != len(receipts)
                    or run.get("runtime_budget", {}).get("iterations", 0) > 20):
                raise ValueError("Capability-check receipts exceed or omit the shared turn budget")
            trace = run.get("capability_gate")
            if not isinstance(trace, dict):
                raise TypeError("Capability-check trace is missing")
            if (trace.get("policy") != policy or trace.get("policy_identity") != identity
                    or trace.get("run_id") != run.get("run_id") or trace.get("session_id") != row.get("session_id")):
                raise ValueError("Capability-check trace has a different scope or policy")
            checks = trace.get("checks")
            if (not isinstance(checks, list) or len(checks) > 1
                    or trace.get("check_requests") != purposes.get("capability_check", 0)
                    or trace.get("main_requests") != purposes.get("main", 0)):
                raise ValueError("Capability checks must occur once per original user turn")
            complete = run.get("runtime_status") == "completed"
            if complete and (len(checks) != 1 or purposes.get("capability_check") != 1):
                raise ValueError("A completed user turn requires one capability check")
            turn_observations = [item for item in observations if item.get("turn_index") == run["turn_index"]]
            if complete and len(turn_observations) != len(receipts):
                raise ValueError("Completed capability-check observations omit model requests")
            gate_observations = []
            for item in turn_observations:
                system, purpose = item.get("system"), item.get("request_purpose")
                if (not isinstance(system, str) or not system.startswith(native_prefix)
                        or item.get("system_sha256") != hashlib.sha256(system.encode("utf-8")).hexdigest()
                        or purpose not in {"main", "capability_check"}
                        or not any(receipt.get("request_number") == item.get("request_number")
                                   and receipt.get("purpose") == purpose for receipt in receipts)):
                    raise ValueError("Actual capability-check model input identity differs from its receipt")
                if purpose == "capability_check":
                    if not system.endswith("\n\n" + gate_system) or item.get("offered_tools") != []:
                        raise ValueError("A capability check must use the captured tool-free system")
                    gate_observations.append(item)
                elif system.endswith("\n\n" + gate_system):
                    raise ValueError("A gate request cannot be relabelled as native execution")
            if complete and len(gate_observations) != 1:
                raise ValueError("Completed user turn has a missing or duplicate gate request")
            for item in gate_observations:
                messages = item.get("messages", [])
                if len(messages) != 1 or messages[0].get("role") != "user" or messages[0].get("tool_calls"):
                    raise ValueError("Gate request must contain its single public data envelope")
                payload = json.loads(messages[0]["content"])
                if (not isinstance(payload, dict) or set(payload) != {"policy", "conversation", "tools", "tools_sha256"}
                        or payload["policy"] != policy or not isinstance(payload["conversation"], list)
                        or not isinstance(payload["tools"], list)
                        or payload["tools_sha256"] != _gate_digest(payload["tools"])):
                    raise ValueError("Gate public input/tool schema envelope differs from the captured contract")
                for message in payload["conversation"]:
                    expected = {"role", "content", "tool_calls", "tool_call_id"}
                    if message.get("role") == "tool":
                        expected |= {"tool_outcome", "tool_error", "tool_name"}
                    if set(message) != expected or message.get("role") not in {"user", "assistant", "tool", "system"}:
                        raise ValueError("Gate envelope contains fields outside the public conversation projection")
                if any(not isinstance(tool, dict) or set(tool) != {"name", "description", "parameters"}
                       for tool in payload["tools"]):
                    raise ValueError("Gate tool projection differs from the captured schema contract")
                if len(checks) != 1:
                    raise ValueError("Gate request has no matching saved decision")
                check = checks[0]
                if (check.get("input_sha256") != _gate_digest(payload)
                        or check.get("tools_sha256") != payload["tools_sha256"]
                        or check.get("request_number") != item.get("request_number")):
                    raise ValueError("Saved gate decision is not bound to its actual model input")
                latest = next((message for message in reversed(payload["conversation"])
                               if message.get("role") == "user"), None)
                if check.get("user_turn_sha256") != _gate_digest({"user": latest, "mailbox_id": None}):
                    raise ValueError("Saved gate decision is stale for this original user turn")
                native_system = item["system"][:-len("\n\n" + gate_system)]
                for native in turn_observations:
                    if native.get("request_purpose") == "main" and (
                            native.get("system") != native_system
                            or native.get("offered_tools") != [tool["name"] for tool in payload["tools"]]):
                        raise ValueError("Native execution changed the captured system or effective tool inventory")
            if checks:
                status = checks[0].get("status")
                parse_status = checks[0].get("parse_status")
                if status not in {"ready", "fallback_to_native", "pending", *terminal}:
                    raise ValueError("Unknown saved capability-check decision")
                if complete and status == "pending":
                    raise ValueError("A pending capability check cannot finish a user turn")
                if (status in {"ready", *terminal} and parse_status != "ok"
                        or status == "fallback_to_native" and parse_status not in {
                            "invalid_json", "invalid_contract", "unexpected_tools", "incomplete_response",
                            "response_too_large", "recovered_without_decision"}):
                    raise ValueError("Malformed capability checks cannot masquerade as valid abstentions")
                if status in terminal and (purposes.get("main", 0) or any(
                        entry.get("run_id") == run.get("run_id") for entry in row.get("tool_ledger", []))):
                    raise ValueError("A terminal gate decision cannot execute business tools")
                if complete and status in {"ready", "fallback_to_native"} and purposes.get("main", 0) < 1:
                    raise ValueError("Ready or malformed checks must enter the native fallback/execution path")
    return identity


def runtime_infrastructure(row: dict) -> bool:
    """A typed Runtime exception is not a failed Agent decision."""
    if known_agent_control_failure(row):
        return False
    return bool(row.get("error_type")) or any(
        run.get("runtime_status") == "failed" and (run.get("runtime_diagnostic") or {}).get("error_type")
        for run in row.get("runs", [])
    )


def known_agent_control_failure(row: dict) -> bool:
    """Only an explicit, typed Agent control-contract failure is a scored zero."""
    runs = row.get("runs", [])
    failed = [run for run in runs if run.get("runtime_status") == "failed"]
    flagged = row.get("agent_control_failure") is True and all(
        run.get("agent_control_failure") is True for run in failed)
    errors = [(run.get("runtime_diagnostic") or {}).get("error_type") for run in failed]
    if row.get("error_type"):
        errors.append(row["error_type"])
    return bool(flagged and errors and all(error == "TaskControlError" for error in errors))


def normalize_runtime_outcomes(records: list[dict], rows: list[dict]) -> list[dict]:
    """Add the typed exception boundary absent from the historical exporter."""
    by_id = {row["id"]: row for row in rows}
    result = deepcopy(records)
    for record in result:
        row = by_id[record["task_id"]]
        if known_agent_control_failure(row):
            if row.get("result") != "fail":
                raise ValueError("An Agent control-contract failure must be a recorded task failure")
            record["scoring"]["passed"] = False
            record["outcome"]["category"] = "task_failed"
            record["outcome"]["reasons"] = sorted(set(record["outcome"]["reasons"] + ["agent_control_failure"]))
        elif runtime_infrastructure(row):
            if row.get("result") == "pass":
                raise ValueError("A task with a Runtime exception cannot be recorded as passed")
            record["scoring"]["passed"] = None
            record["outcome"]["category"] = "infrastructure_error"
            record["outcome"]["reasons"] = sorted(set(record["outcome"]["reasons"] + ["typed_runtime_exception"]))
        elif (record["outcome"]["category"] not in {"infrastructure_error", "dataset_conflict", "not_run"}
              and any(run.get("runtime_status") == "budget_exhausted" for run in row.get("runs", []))):
            record["outcome"]["category"] = "budget_terminated"
            record["scoring"]["passed"] = False
        record["record_sha256"] = _digest({key: value for key, value in record.items() if key != "record_sha256"})
        validate_record(record)
    return result


def _profile_identity(control: dict) -> dict:
    profile = control.get("profile", {})
    identity = {field: profile.get(field) for field in MODEL_FIELDS}
    if any(identity.get(field) != value for field, value in {
        "provider_id": "deepseek", "base_url": "https://api.deepseek.com",
        "protocol": "deepseek_responses", "model": "deepseek-flash", "reasoning_effort": "none",
    }.items()):
        raise ValueError("Expected the captured official DeepSeek flash/none identity")
    return identity


def _sealed(root: Path) -> tuple[dict, dict, dict]:
    """Require final evidence before opening any mutable execution receipt."""
    if not (root / "execution-finished.json").is_file() or not (root / "report.json").is_file():
        raise ValueError("Only sealed completed campaigns can be replayed")
    finished, report = _read(root / "execution-finished.json"), _read(root / "report.json")
    if finished != report:
        raise ValueError("Completion marker and report differ")
    control = _read(root / "execution-control.json")
    mode = control.get("concurrent_tasks", {})
    if mode.get("finalized") is not True or set(mode.get("states", {}).values()) != {"completed"}:
        raise ValueError("Concurrent campaign is not sealed")
    if control.get("test_only") is not False:
        raise ValueError("Synthetic transport tests are not saved model measurements")
    return report, control, mode


def load_campaign(campaign_dir: str | Path, *, label: str) -> dict:
    """Verify one saved forty-task run using its immutable as-run source copies."""
    root = Path(campaign_dir).resolve()
    report, control, mode = _sealed(root)
    plan = _read(root / "campaign-plan.json")
    for name, key in (("source-manifest.json", "source-manifest.json_sha256"),
                      ("campaign-plan.json", "campaign-plan.json_sha256")):
        if control.get(key) != _hash(root / name):
            raise ValueError("Captured source or plan identity changed")
    split = root / "as-run-source" / SPLIT_RELATIVE
    if (_hash(split / "split-manifest.json") != COMPACT_MANIFEST_SHA256
            or _hash(split / "development-ids.txt") != DEVELOPMENT_SHA256
            or _hash(split / "holdout-ids.txt") != HOLDOUT_SHA256):
        raise ValueError("Saved compact40 membership identity differs")
    ids = (split / "development-ids.txt").read_text(encoding="utf-8").splitlines()
    heldout = set((split / "holdout-ids.txt").read_text(encoding="utf-8").splitlines())
    if len(ids) != 40 or len(set(ids)) != 40 or len(heldout) != 60 or set(ids) & heldout:
        raise ValueError("Expected forty development tasks and sixty disjoint holdout tasks")
    planned = [task_id for group in plan.get("groups", []) for task_id in group["entry_ids"]]
    if planned != ids or control.get("case_ids") != ids:
        raise ValueError("Campaign did not use the exact frozen development order")
    if (plan.get("maximum_concurrency") != 20 or mode.get("maximum") != 20
            or report.get("maximum_concurrency") != 20):
        raise ValueError("This comparison requires twenty concurrent workers")
    model = _profile_identity(control)
    public_profile = _read(root / "model-selection.json")
    if any(public_profile.get(field) != value for field, value in model.items()):
        raise ValueError("Model selection differs from captured execution identity")
    admitted_case_hash = hashlib.sha256(json.dumps(
        _read(root / "private-cases.json"), sort_keys=True, ensure_ascii=False, allow_nan=False,
    ).encode()).hexdigest()
    if (admitted_case_hash != control.get("cases_sha256")
            or report.get("dataset_binding") != control.get("dataset_binding")):
        raise ValueError("Private cases or reported campaign binding changed")
    raw = _read(root / "results/results.json")
    rows = raw.get("results", [])
    if [row.get("id") for row in rows] != ids:
        raise ValueError("Saved results do not cover the frozen forty tasks")
    agent_policy = control.get("agent_policy") or "off"
    if agent_policy not in SUPPORTED_POLICIES:
        raise ValueError("Unsupported saved Agent policy")
    if agent_policy == "readiness_state_v1":
        if plan.get("agent_policy") != agent_policy or any(row.get("agent_policy") != agent_policy for row in rows):
            raise ValueError("A3 policy is not bound into every task receipt")
        for row in rows:
            for run in row.get("runs", []):
                receipts = run.get("model_request_receipts")
                purposes = run.get("model_request_purposes")
                if (not isinstance(receipts, list) or len(receipts) > 20 or not isinstance(purposes, dict)
                        or any(type(value) is not int or value < 0 for value in purposes.values())
                        or sum(purposes.values()) != len(receipts)):
                    raise ValueError("A3 model request receipts exceed or omit the shared per-turn bound")
    prompt_identity = (validate_prompt_campaign(root, plan, control, report, rows)
                       if agent_policy == PROMPT_POLICY else None)
    if agent_policy in GATE_POLICIES:
        prompt_identity = validate_gate_campaign(root, plan, control, report, rows)
    records = normalize_runtime_outcomes(load_unmetered_campaign(root, config_label=label), rows)
    if [record["task_id"] for record in records] != ids:
        raise ValueError("Exported record order changed")
    counts = Counter(record["subcategory"] for record in records)
    if counts != dict.fromkeys(SUBCATEGORIES, 10):
        raise ValueError("Each Multi-Turn category must contain ten development tasks")
    reference = _read(root / "private-cases.json")
    reference_counts = {}
    for case in reference:
        expected = case.get("expected")
        if (case.get("id") not in ids or not isinstance(expected, list)
                or any(not isinstance(turn, list) for turn in expected)):
            raise ValueError("Invalid private reference task boundary")
        reference_counts[case["id"]] = [len(turn) for turn in expected]
    if set(reference_counts) != set(ids):
        raise ValueError("Reference task membership differs from saved results")
    identity = {
        "model": model, "maximum_concurrency": 20,
        "agent_policy": agent_policy,
        "runtime_policy": {field: control.get(field) for field in COMPARABLE_POLICY_FIELDS},
        "source_manifest_sha256": _hash(root / "source-manifest.json"),
        "source_files_digest": _digest(_read(root / "source-manifest.json")["files"]),
        "plan_sha256": _hash(root / "campaign-plan.json"),
        "control_sha256": _hash(root / "execution-control.json"),
        "split_manifest_sha256": COMPACT_MANIFEST_SHA256,
        "development_ids_sha256": DEVELOPMENT_SHA256, "holdout_ids_sha256": HOLDOUT_SHA256,
    }
    if prompt_identity is not None:
        identity["agent_policy_identity"] = prompt_identity
    for record in records:
        snapshot = record["config_identity"]["snapshot"]
        snapshot.update(maximum_concurrency=20, agent_policy=identity["agent_policy"],
                        source_manifest_sha256=identity["source_manifest_sha256"])
        if prompt_identity is not None:
            snapshot["agent_policy_identity"] = deepcopy(prompt_identity)
        if agent_policy in GATE_POLICIES:
            row = next(item for item in rows if item["id"] == record["task_id"])
            record["capability_gate"] = {"policy_identity": deepcopy(prompt_identity),
                "turns": [projection for run in row.get("runs", [])
                          if (projection := _gate_projection(run)) is not None],
                "semantic_correctness_authority": "Saved official BFCL checks, not the gate's self-report"}
        record["config_identity"]["sha256"] = _digest(snapshot)
        record["record_sha256"] = _digest({key: value for key, value in record.items() if key != "record_sha256"})
        validate_record(record)
    return {"label": label, "root": str(root), "records": records, "reference_call_counts": reference_counts,
            "identity": identity, "report": report, "rows": rows, "summary": summarize_campaign(records, report)}


def auxiliary_turn_counts(record: dict, reference_counts: list[int], kind: str) -> tuple[int, int]:
    """Return passed/eligible turns; no language-quality judgment is implied."""
    turns = record["turns"]
    if len(turns) != len(reference_counts) or any(type(value) is not int or value < 0 for value in reference_counts):
        raise ValueError("Reference turn counts do not match the complete task")
    if kind == "no_premature_calls":
        selected = [index for index, count in enumerate(reference_counts) if count == 0]
    elif kind == "post_blocked_continuation":
        blocked = [index for index, count in enumerate(reference_counts) if count == 0]
        selected = [index for index, count in enumerate(reference_counts) if blocked and index > blocked[0] and count > 0]
    elif kind == "nonempty_turn_checks":
        selected = [index for index, count in enumerate(reference_counts) if count > 0]
    else:
        raise ValueError("Unknown deterministic auxiliary metric")
    checks = record["scoring"]["turn_checks"]
    good = 0
    for index in selected:
        turn = turns[index]
        completed = turn.get("runtime_status") == "completed"
        if kind == "no_premature_calls":
            calls = sum(len(message["tool_calls"]) for message in turn["messages"] if message["role"] == "assistant")
            valid = completed and calls == 0
        else:
            valid = completed and index < len(checks) and checks[index].get("valid") is True
        good += bool(valid)
    return good, len(selected)


def _complete_sum(values):
    return sum(values) if values and all(value is not None for value in values) else None


def summarize_campaign(records: list[dict], report: dict) -> dict:
    scorable = [record for record in records if type(record["scoring"]["passed"]) is bool]
    passed = sum(record["scoring"]["passed"] is True for record in scorable)
    categories = {}
    for category in SUBCATEGORIES:
        group = [record for record in records if record["subcategory"] == category]
        scored = [record for record in group if type(record["scoring"]["passed"]) is bool]
        good = sum(record["scoring"]["passed"] is True for record in scored)
        categories[category] = {"allocated": len(group), "scorable": len(scored), "passed": good,
                                "failed": len(scored) - good, "rate": good / len(scored) if scored else None}
    rates = [group["rate"] for group in categories.values()]
    elapsed = [record["recorded_usage"].get("elapsed_seconds") for record in records]
    known_elapsed = sorted(value for value in elapsed if value is not None)
    all_scored = len(scorable) == len(records)
    return {
        "tasks": len(records), "scorable": len(scorable), "passed": passed, "failed": len(scorable) - passed,
        "excluded": [{"task_id": record["task_id"], "outcome": record["outcome"], "score": None}
                     for record in records if record["scoring"]["passed"] is None],
        "main_success_rate": passed / len(records) if all_scored and records else None,
        "scorable_subset_success_rate": passed / len(scorable) if scorable else None,
        "category_macro_success_rate": sum(rates) / 4 if all_scored and all(rate is not None for rate in rates) else None,
        "categories": categories, "outcomes": dict(Counter(record["outcome"]["category"] for record in records)),
        "recorded_model_requests": _complete_sum([record["recorded_usage"]["model_requests"] for record in records]),
        "recorded_tokens": {field: _complete_sum([record["recorded_usage"].get(field) for record in records])
                            for field in TOKEN_FIELDS},
        "recorded_wall_elapsed_seconds": report.get("elapsed_seconds"),
        "recorded_task_latency_seconds": {
            "known": len(known_elapsed), "unknown": len(elapsed) - len(known_elapsed),
            "median": median(known_elapsed) if known_elapsed else None,
            "p95_nearest_rank": known_elapsed[math.ceil(0.95 * len(known_elapsed)) - 1] if known_elapsed else None,
        },
        "failure_reasons": dict(Counter(reason for record in records for reason in record["outcome"]["reasons"])),
    }


def _category_diagnostics(campaign: dict, category: str) -> dict:
    """Fixed-category official turn boundaries, with no semantic judge."""
    records, rows = campaign["records"], campaign["rows"]
    by_id = {row["id"]: row for row in rows}
    cases = []
    for record in records:
        if record["subcategory"] != category:
            continue
        counts = campaign["reference_call_counts"][record["task_id"]]
        checks, turns = record["scoring"]["turn_checks"], record["turns"]
        blocked_indices = [index for index, value in enumerate(counts) if value == 0]
        blocked = []
        for index in blocked_indices:
            turn = turns[index]
            calls = sum(len(item["tool_calls"]) for item in turn["messages"] if item["role"] == "assistant")
            run = next((item for item in by_id[record["task_id"]].get("runs", [])
                        if item.get("turn_index") == index), {})
            trace = run.get("capability_gate", {})
            decision = trace.get("checks", [{}])[-1] if trace.get("checks") else {}
            blocked.append({"turn_index": index, "runtime_status": turn["runtime_status"],
                "completed_zero_business_calls": turn["runtime_status"] == "completed" and calls == 0,
                "business_tool_calls": calls,
                "official_valid": index < len(checks) and checks[index].get("valid") is True,
                "gate_status": decision.get("status"), "gate_parse_status": decision.get("parse_status"),
                "native_model_requests": run.get("model_request_purposes", {}).get("main"),
                "gate_model_requests": run.get("model_request_purposes", {}).get("capability_check")})
        later = [index for index, count in enumerate(counts)
                 if blocked_indices and index > blocked_indices[0] and count > 0]
        def valid(index, saved_turns=turns, saved_checks=checks):
            return (saved_turns[index].get("runtime_status") == "completed"
                    and index < len(saved_checks) and saved_checks[index].get("valid") is True)
        first = later[0] if later else None
        cases.append({"task_id": record["task_id"], "passed": record["scoring"]["passed"],
            "blocked_turns": blocked, "post_blocked_passed": sum(valid(index) for index in later),
            "post_blocked_eligible": len(later), "first_restored_turn_index": first,
            "first_restored_check_passed": valid(first) if first is not None else None,
            "all_post_blocked_checks_passed": all(valid(index) for index in later) if later else None,
            "only_blocked_turns_failed": (bool(blocked_indices) and all(
                valid(index) for index in range(len(turns)) if index not in blocked_indices)
                and all(turn.get("runtime_status") == "completed" for turn in turns)
                and any(not item["official_valid"] for item in blocked))})
    blocked = [turn for case in cases for turn in case["blocked_turns"]]
    return {"allocated_tasks": len(cases),
        "scorable_tasks": sum(type(case["passed"]) is bool for case in cases),
        "passed_tasks": sum(case["passed"] is True for case in cases),
        "blocked_turns": len(blocked),
        "completed_zero_business_call_turns": sum(item["completed_zero_business_calls"] for item in blocked),
        "gate_missing_capability_blocked_turns": sum(item["gate_status"] == "missing_capability"
            and item["gate_parse_status"] == "ok" for item in blocked),
        "gate_needs_clarification_blocked_turns": sum(item["gate_status"] == "needs_clarification"
            and item["gate_parse_status"] == "ok" for item in blocked),
        "native_completed_zero_business_call_turns": sum(item["completed_zero_business_calls"]
            and item["gate_status"] not in {"missing_capability", "needs_clarification"} for item in blocked),
        "fallback_completed_zero_business_call_turns": sum(item["completed_zero_business_calls"]
            and item["gate_status"] == "fallback_to_native" for item in blocked),
        "official_blocked_turn_passes": sum(item["official_valid"] for item in blocked),
        "blocked_business_tool_calls": sum(item["business_tool_calls"] for item in blocked),
        "post_blocked_checks_passed": sum(case["post_blocked_passed"] for case in cases),
        "post_blocked_checks_eligible": sum(case["post_blocked_eligible"] for case in cases),
        "first_restored_checks_passed": sum(case["first_restored_check_passed"] is True for case in cases),
        "first_restored_checks_eligible": sum(case["first_restored_turn_index"] is not None for case in cases),
        "only_blocked_turns_failed_tasks": sum(case["only_blocked_turns_failed"] for case in cases),
        "semantic_clarification_quality_judge": False,
        "cases": cases}


def capability_diagnostics(campaign: dict) -> dict:
    """MF/MP deterministic breakdown and gate accounting, separate from score.

    Private reference call counts select evaluator-only turn boundaries. They
    are never Agent inputs. A reference-zero turn alone cannot establish that
    a generated question is semantically correct.
    """
    records, rows = campaign["records"], campaign["rows"]
    purposes, states, parse_states = Counter(), Counter(), Counter()
    gate_rows, blocked_expected_execution = [], []
    purpose_tokens, unknown_purpose_tokens = {}, {}
    for row in rows:
        for run in row.get("runs", []):
            purposes.update(run.get("model_request_purposes", {}))
            for index, receipt in enumerate(run.get("model_request_receipts", [])):
                purpose = receipt.get("purpose", "unknown")
                purpose_tokens.setdefault(purpose, Counter())
                unknown_purpose_tokens.setdefault(purpose, Counter())
                usage = run.get("usage", [])[index] if index < len(run.get("usage", [])) else {}
                for field in TOKEN_FIELDS:
                    value = usage.get(field)
                    if type(value) is int and value >= 0:
                        purpose_tokens[purpose][field] += value
                    else:
                        unknown_purpose_tokens[purpose][field] += 1
            for check in (run.get("capability_gate") or {}).get("checks", []):
                states[check.get("status", "unknown")] += 1
                parse_states[check.get("parse_status", "unknown")] += 1
                gate_rows.append({"task_id": row["id"], "turn_index": run["turn_index"],
                    "subcategory": next(record["subcategory"] for record in records if record["task_id"] == row["id"]),
                    "status": check.get("status"), "parse_status": check.get("parse_status"),
                    "request_number": check.get("request_number"),
                    "reference_requires_execution": campaign["reference_call_counts"][row["id"]][run["turn_index"]] > 0,
                    "runtime_status": run.get("runtime_status"),
                    "model_request_purposes": deepcopy(run.get("model_request_purposes", {}))})
                if (check.get("status") in {"missing_capability", "needs_clarification"}
                        and campaign["reference_call_counts"][row["id"]][run["turn_index"]] > 0):
                    blocked_expected_execution.append({"task_id": row["id"], "turn_index": run["turn_index"],
                        "gate_status": check["status"],
                        "subcategory": next(record["subcategory"] for record in records if record["task_id"] == row["id"]),
                        "runtime_status": run.get("runtime_status"), "parse_status": check.get("parse_status")})
    other = [record for record in records if record["subcategory"] != "multi_turn_miss_func"]
    return {"configuration_label": campaign["label"], "affects_main_score": False,
        "definition": "Deterministic saved-trajectory breakdown; private reference boundaries are evaluation-only",
        "missing_functions": _category_diagnostics(campaign, "multi_turn_miss_func"),
        "missing_parameters": _category_diagnostics(campaign, "multi_turn_miss_param"),
        "other_categories": {"allocated_tasks": len(other),
            "scorable_tasks": sum(type(item["scoring"]["passed"]) is bool for item in other),
            "passed_tasks": sum(item["scoring"]["passed"] is True for item in other)},
        "base_and_long_context": {"allocated_tasks": sum(record["subcategory"] in {
                "multi_turn_base", "multi_turn_long_context"} for record in records),
            "passed_tasks": sum(record["scoring"]["passed"] is True for record in records if record["subcategory"] in {
                "multi_turn_base", "multi_turn_long_context"})},
        "capability_gate": {"enabled": campaign["identity"]["agent_policy"] in GATE_POLICIES,
            "decisions": dict(states), "parse_statuses": dict(parse_states),
            "model_request_purposes": dict(purposes), "checks": gate_rows,
            "recorded_usage_by_purpose": {purpose: {field: (
                totals[field] if unknown_purpose_tokens[purpose][field] == 0 else None)
                for field in TOKEN_FIELDS} for purpose, totals in purpose_tokens.items()},
            "unknown_usage_fields_by_purpose": {purpose: dict(values) for purpose, values in unknown_purpose_tokens.items()},
            "blocked_expected_execution_turns": blocked_expected_execution,
            "non_mf_blocked_expected_execution_turns": [item for item in blocked_expected_execution
                if item["subcategory"] != "multi_turn_miss_func"],
            "needs_clarification_reference_zero_turns": [item for item in gate_rows
                if item["status"] == "needs_clarification" and not item["reference_requires_execution"]],
            "needs_clarification_expected_execution_turns": [item for item in gate_rows
                if item["status"] == "needs_clarification" and item["reference_requires_execution"]],
            "fallback_policy": "Malformed checks return to native execution and do not become capability abstentions",
            "semantic_correctness": "Self-reported gate states do not establish capability judgment accuracy"},
        "new_model_api_calls": 0, "new_judge_api_calls": 0, "judge_enabled": False,
        "counterfactual_passes_claimed": False, "holdout_tasks_evaluated": 0}


def verify_native_gate_projection(campaign: dict, native_cases: list[dict]) -> dict:
    """Compare each native-case status/hash/receipt to the primary saved run."""
    expected = {row["id"]: row for row in campaign["rows"]}
    seen, projected, checked = set(), [], 0
    for case in native_cases:
        record = case.get("metadata", {}).get("aster_bfcl_record", {})
        task_id = record.get("task_id")
        if task_id not in expected or task_id in seen:
            raise ValueError("Native conversation projection has unknown or duplicate tasks")
        seen.add(task_id)
        if campaign["identity"]["agent_policy"] not in GATE_POLICIES:
            continue
        gate = record.get("capability_gate")
        turns = [item for run in expected[task_id].get("runs", []) if (item := _gate_projection(run)) is not None]
        if (not isinstance(gate, dict) or gate.get("policy_identity") != campaign["identity"]["agent_policy_identity"]
                or gate.get("turns") != turns):
            raise ValueError("Native conversation gate metadata differs from primary evidence")
        checked += sum(len(turn["checks"]) for turn in turns)
        projected.append({"task_id": task_id, "turns": turns})
    eligible = {record["task_id"] for record in campaign["records"] if record["scoring"]["passed"] is not None}
    if seen != eligible:
        raise ValueError("Native conversation projection omits scored tasks")
    return {"enabled": campaign["identity"]["agent_policy"] in GATE_POLICIES,
        "native_cases_verified": len(seen), "checks_verified": checked,
        "projection_sha256": _digest(projected),
        "primary_results_sha256": _hash(Path(campaign["root"]) / "results/results.json"),
        "new_model_api_calls": 0, "new_judge_api_calls": 0}


def compare_campaigns(baseline: dict, candidate: dict) -> dict:
    """Paired fixed-task descriptive comparison; no component-level causality."""
    first, second = baseline["identity"], candidate["identity"]
    if baseline["root"] == candidate["root"]:
        raise ValueError("One saved run cannot be relabeled as a new candidate")
    if first["agent_policy"] not in SUPPORTED_POLICIES or second["agent_policy"] not in SUPPORTED_POLICIES:
        raise ValueError("Expected supported recorded Agent policy identities")
    for field in ("model", "maximum_concurrency", "runtime_policy", "split_manifest_sha256",
                  "development_ids_sha256", "holdout_ids_sha256"):
        if first[field] != second[field]:
            raise ValueError("A paired comparison requires matching model, task, budget and concurrency identities")
    left, right = baseline["records"], candidate["records"]
    for campaign in (baseline, candidate):
        for record in campaign["records"]:
            validate_record(record)
            if (record["config_label"] != campaign["label"]
                    or record["config_identity"]["snapshot"].get("agent_policy") != campaign["identity"]["agent_policy"]
                    or (campaign["identity"]["agent_policy"] in {PROMPT_POLICY, *GATE_POLICIES}
                        and record["config_identity"]["snapshot"].get("agent_policy_identity")
                        != campaign["identity"].get("agent_policy_identity"))):
                raise ValueError("Task record and campaign policy identities differ")
    if [record["task_id"] for record in left] != [record["task_id"] for record in right]:
        raise ValueError("Paired tasks differ")
    transitions = {"gained": [], "regressed": [], "both_pass": [], "both_fail": [], "unscorable_pair": []}
    for old, new in zip(left, right, strict=True):
        before, after = old["scoring"]["passed"], new["scoring"]["passed"]
        key = ("unscorable_pair" if before is None or after is None else "gained" if not before and after
               else "regressed" if before and not after else "both_pass" if before else "both_fail")
        transitions[key].append(old["task_id"])
    old_rate, new_rate = baseline["summary"]["main_success_rate"], candidate["summary"]["main_success_rate"]
    delta = new_rate - old_rate if old_rate is not None and new_rate is not None else None
    def difference(old, new):
        return new - old if old is not None and new is not None else None
    return {
        "baseline_label": baseline["label"], "candidate_label": candidate["label"],
        "agent_policies": {"baseline": first["agent_policy"], "candidate": second["agent_policy"]},
        "unique_tasks": len(left), "paired_scorable_tasks": len(left) - len(transitions["unscorable_pair"]),
        "transitions": transitions, "main_success_rate_delta": delta,
        "missing_function_transitions": {key: [task_id for task_id in values
            if task_id.startswith("multi_turn_miss_func_")] for key, values in transitions.items()},
        "missing_parameter_transitions": {key: [task_id for task_id in values
            if task_id.startswith("multi_turn_miss_param_")] for key, values in transitions.items()},
        "base_and_long_context_transitions": {key: [task_id for task_id in values
            if task_id.startswith(("multi_turn_base_", "multi_turn_long_context_"))]
            for key, values in transitions.items()},
        "other_category_transitions": {key: [task_id for task_id in values
            if not task_id.startswith("multi_turn_miss_func_")] for key, values in transitions.items()},
        "main_success_rate_delta_percentage_points": delta * 100 if delta is not None else None,
        "recorded_model_request_delta": difference(baseline["summary"]["recorded_model_requests"],
                                                   candidate["summary"]["recorded_model_requests"]),
        "recorded_token_deltas": {field: difference(baseline["summary"]["recorded_tokens"][field],
                                                     candidate["summary"]["recorded_tokens"][field]) for field in TOKEN_FIELDS},
        "recorded_wall_elapsed_delta_seconds": difference(baseline["summary"]["recorded_wall_elapsed_seconds"],
                                                           candidate["summary"]["recorded_wall_elapsed_seconds"]),
        "interpretation": (
            "One A0/A3 fixed-development paired run; all three candidate changes are combined. Individual A1/A2 effects were not measured."
            if first["agent_policy"] == "off" and second["agent_policy"] == "readiness_state_v1" else
            "One fixed-development paired comparison of captured Agent policies. A single run does not establish stable gains or effects of unmeasured components."),
        "new_model_api_calls": 0, "new_judge_api_calls": 0, "holdout_tasks_evaluated": 0,
    }


def diagnostic_warnings() -> dict:
    return {
        "family4_source_semantic_warning": [f"{category}_4" for category in SUBCATEGORIES],
        "strict_response_path_warning": ["multi_turn_base_5", "multi_turn_long_context_5"],
        "score_policy": "Original strict scores and membership remain unchanged; warnings do not exclude tasks or alter denominators.",
        "analysis": "docs/bfcl-dev40-failure-analysis-20261030.md",
        "clarification_quality": {"status": "semantic_judge_disabled", "score": None},
    }
