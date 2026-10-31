"""Once-only native promptfoo dispatch of frozen dev200 on A6 Flash/none.

Default preflight reads local frozen data and trusted DNS settings only.
--execute admits a fresh paid campaign only;
previous request outcomes, tasks and saved measurements are never replayed.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import shutil
import subprocess
import sys
import time
from collections import Counter
from copy import deepcopy
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[2]
DEFAULT_CAMPAIGN = PROJECT / "artifacts/bfcl/dev200-a6-flash-none-c100-20261030"
MODEL = "deepseek-v4-flash"
PROVIDER_ID = "provider-cd3755c2b87245fc9705011b4cef067d"
BASE_URL = "https://api.a6api.com/v1"
PROTOCOL = "openai_responses"
WIRE_POLICY = "a6_deepseek_v4_flash_none_v1"
PROVIDER_FAILURE_POLICY = "a6_known_gateway_task_isolation_v1"
BACKEND_MODE = "same_campaign_task_locks_v1"
PROVIDER_LABEL = "a6-deepseek-v4-flash-no-thinking-concurrent"

_SPEC = importlib.util.spec_from_file_location("aster_a6_development_helpers", HERE / "run_development.py")
_development = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_development)
_helpers, _dataset = _development._helpers, _development._dataset
load_development_cases = _development.load_development_cases
cli_command = _development.cli_command


def preflight(concurrency=100, *, http_dns_fallback="google"):
    if http_dns_fallback not in {"off", "google"}:
        raise ValueError("The captured DNS fallback policy must be off or google")
    plan = _development.preflight(concurrency)
    plan.update(campaign=DEFAULT_CAMPAIGN.name, variant="aster_bfcl_v4_development200_a6_same_campaign",
        provider_id=PROVIDER_ID, base_url=BASE_URL, protocol=PROTOCOL, model=MODEL, wire_policy=WIRE_POLICY,
        http_dns_fallback=http_dns_fallback, provider_failure_policy=PROVIDER_FAILURE_POLICY,
        provider_failure_policy_note="Known received A6 gateway 502/503/504 stops only that task with a null infrastructure result; no request or task retries",
        comparison_note="Provider and model identity change from native DeepSeek A0; not an Agent optimization gain",
        baseline_policy="An independent A6 configuration baseline only after all admissible tasks are scored with intact evidence")
    return plan


def _trusted_dns_fallback():
    from aster.config import read_yaml
    from aster.paths import resolve_data_dir

    source = resolve_data_dir()
    modes = [read_yaml(path).get("http_dns_fallback", "google") for path in (
        source / "config.yaml", source / "agents/default/config.yaml")]
    if any(value not in {"off", "google"} for value in modes):
        raise ValueError("The trusted DNS fallback policy must be off or google")
    return "off" if "off" in modes else "google"


def _network_admission(profile, dns_fallback):
    from aster.providers.profiles import verify_public_endpoint

    address = asyncio.run(verify_public_endpoint(profile.base_url, dns_fallback=dns_fallback))
    return {"schema_version": 1, "base_url": profile.base_url, "http_dns_fallback": dns_fallback,
        "verified_public_addresses": [address], "captured_at": _helpers.now(),
        "resolver_endpoint": "https://dns.google/resolve" if dns_fallback == "google" else None,
        "credentials_sent": False, "model_api_calls": 0,
        "policy": "Every actual request revalidates public DNS and pins transport; address drift is rejected by the child guard"}


def child_environment(output, control, network):
    env = _development.child_environment(output, control)
    endpoints = json.loads(env["ASTER_PF_LIVE_ENDPOINTS"])
    endpoints.extend([address, 443] for address in network["verified_public_addresses"])
    if network["http_dns_fallback"] == "google":
        endpoints.append(["dns.google", 443])
    env["ASTER_PF_LIVE_ENDPOINTS"] = json.dumps(endpoints)
    return env


def build_config(plan, campaign):
    config = _development.build_config(plan, campaign)
    config["description"] = "A6 deepseek-v4-flash/none; 200 frozen Aster development tasks; one concurrent campaign"
    config["providers"][0].update(id="file://" + (HERE / "a6_concurrent_provider.py").as_posix(), label=PROVIDER_LABEL)
    for test in config["tests"]:
        test["metadata"].update(model=MODEL, provider_id=PROVIDER_ID, reasoning_effort="none", wire_policy=WIRE_POLICY)
    config["defaultTest"]["assert"][0]["value"] = "file://a6_no_thinking_assertion.py"
    return config


def validate_native(payload, rows):
    """Verify A6 identity before reuse of the established matrix/receipt checks."""
    checked, errors = deepcopy(payload), []
    for item in checked.get("results", {}).get("results", []):
        if item.get("provider", {}).get("label") != PROVIDER_LABEL:
            errors.append("unexpected_a6_provider_identity")
        try:
            compact = json.loads(item.get("response", {}).get("output", ""))
        except (TypeError, ValueError):
            errors.append("missing_a6_provider_output")
            continue
        if (compact.get("model") != MODEL or compact.get("provider_id") != PROVIDER_ID
                or compact.get("reasoning_effort") != "none" or compact.get("wire_policy") != WIRE_POLICY
                or compact.get("backend_mode") != BACKEND_MODE or "api_count_limit" not in compact
                or compact["api_count_limit"] is not None):
            errors.append("unexpected_a6_execution_settings")
        item.setdefault("provider", {})["label"] = "current-aster-multiturn20"
    return sorted(set(errors + _helpers.validate_native(checked, rows)))


def _known_gateway_failure(row, calls):
    failures = [failure for failure in row.get("model_failures", []) if failure.get("category") != "budget"]
    return (row.get("result") == "case_error" and row.get("task_local_gateway_error") is True
        and row.get("error_type") in {None, "ProviderError"}
        and row.get("provider_failure_policy") == PROVIDER_FAILURE_POLICY and bool(calls) and bool(failures)
        and all(call.get("status") == "response_headers_received" and call.get("status_code") in {200, 502, 503, 504}
            for call in calls)
        and all(failure.get("type") == "ProviderError" and (failure.get("category"), failure.get("status_code"))
            in {("server", 502), ("overloaded", 503), ("server", 504)} for failure in failures)
        and [call["status_code"] for call in calls if call["status_code"] != 200]
            == [failure["status_code"] for failure in failures])


def collect_campaign(campaign, plan, payload, output):
    from aster.evaluation.bfcl_multiturn_a6 import read_task_receipt

    result = _development.collect_campaign(campaign, plan, payload, output, expected_model=MODEL,
        native_validator=validate_native, receipt_reader=read_task_receipt)
    campaign = Path(campaign).resolve()
    control = json.loads((campaign / "execution-control.json").read_bytes())
    ledger = json.loads((campaign / "results/http-budget.json").read_bytes())
    profile = control.get("profile", {})
    if (control.get("wire_policy") != WIRE_POLICY or ledger.get("wire_policy") != WIRE_POLICY
            or control.get("provider_failure_policy") != PROVIDER_FAILURE_POLICY
            or plan.get("provider_failure_policy") != PROVIDER_FAILURE_POLICY
            or any(profile.get(key) != value for key, value in {"provider_id": PROVIDER_ID,
                "base_url": BASE_URL, "protocol": PROTOCOL, "model": MODEL, "reasoning_effort": "none"}.items())):
        result["evidence_errors"].append("a6_profile_or_wire_policy_mismatch")
    verified = sum(call.get("wire_policy") == WIRE_POLICY and call.get("wire_protocol") == PROTOCOL
        and call.get("wire_store") is False for call in ledger["calls"])
    if verified != len(ledger["calls"]):
        result["evidence_errors"].append("a6_wire_protocol_or_store_policy_mismatch")
    rows = json.loads((campaign / "results/results.json").read_bytes())["results"]
    gateway_rows = [row for row in rows if row.get("task_local_gateway_error") is True]
    for row in gateway_rows:
        calls = [call for call in ledger["calls"] if call["case_id"] == row["id"]]
        if not _known_gateway_failure(row, calls):
            result["evidence_errors"].append("invalid_task_local_gateway_error_classification")
    result["provider_failure_policy"] = PROVIDER_FAILURE_POLICY
    result["task_local_gateway_error_count"] = len(gateway_rows)
    result["task_local_gateway_error_ids"] = [row["id"] for row in gateway_rows]
    result["task_local_gateway_http_status_codes"] = dict(Counter(str(failure.get("status_code"))
        for row in gateway_rows for failure in row.get("model_failures", []) if failure.get("category") != "budget"))
    admissible = [row for row in rows if row["id"] not in plan["reference_admission_issues"]]
    result["admissible_tasks_attempted"] = sum(row.get("http_attempts", 0) > 0 for row in admissible)
    result["all_admissible_tasks_attempted"] = result["admissible_tasks_attempted"] == result["admissible_tasks"]
    usage = [entry for row in rows for entry in row.get("usage", [])]
    count = len(ledger["calls"])
    fields = ("input_tokens", "output_tokens", "reasoning_tokens", "cache_hit_tokens", "cache_miss_tokens")
    coverage = {field: sum(entry.get(field) is not None for entry in usage) for field in fields}
    result["usage_coverage_calls"] = coverage
    result["usage_totals"] = {field: (sum(entry[field] for entry in usage if entry.get(field) is not None)
        if count and coverage[field] == count else None) for field in fields}
    result["known_usage_totals"] = {field: (sum(entry[field] for entry in usage if entry.get(field) is not None)
        if coverage[field] else None) for field in fields}
    if not coverage["reasoning_tokens"]:
        result["all_known_reasoning_tokens_zero"] = None
    result["all_reasoning_tokens_zero"] = (False if any(entry.get("reasoning_tokens") for entry in usage)
        else None if coverage["reasoning_tokens"] != count or not count else True)
    result["no_thinking_evidence"] = "Explicit reasoning.effort=none on every wire request; missing usage details remain unknown"
    result["a6_wire_protocol_verified_calls"] = verified
    result["comparison_note"] = plan["comparison_note"]
    result["baseline_assessment"]["kind"] = "A6_frozen_development_agent_configuration"
    if result["evidence_errors"]:
        result["baseline_assessment"]["capability_baseline_ready"] = False
        result["baseline_assessment"]["usage_baseline_ready"] = False
        reasons = result["baseline_assessment"]["readiness_limitations"]
        if "evidence_validation_errors" not in reasons:
            reasons.append("evidence_validation_errors")
    result["evidence_errors"] = sorted(set(result["evidence_errors"]))
    result["workflow_completed"] = (result["all_admissible_tasks_attempted"] and not result["evidence_errors"]
        and not result["shared_fatal"] and not result["unknown_request_outcomes"]
        and all(row.get("result") in {"pass", "fail"} or row.get("task_local_gateway_error") is True
            for row in admissible))
    result["workflow_completion_note"] = "Every admissible task was attempted once and reached a recorded terminal result; known gateway errors remain null and prevent a clean capability baseline"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-dir", type=Path, default=DEFAULT_CAMPAIGN)
    parser.add_argument("--concurrency", type=int, choices=range(1, 101), default=100)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    campaign, plan = args.campaign_dir.resolve(), preflight(args.concurrency, http_dns_fallback=_trusted_dns_fallback())
    plan["campaign"] = campaign.name
    campaign.mkdir(parents=True, exist_ok=True)
    if any((campaign / name).exists() for name in ("execution-started.json", "source-manifest.json", "results", "promptfoo")):
        parser.error("Campaign already started; resuming or replaying is forbidden")
    path = campaign / "campaign-plan.json"
    if path.exists():
        if json.loads(path.read_bytes()) != plan:
            parser.error("Saved A6 concurrent plan differs")
    else:
        _helpers.new_json(path, plan)
    if not args.execute:
        print(json.dumps({"mode": "preflight_only", "tasks": 200, "same_campaign": True,
            "concurrency": args.concurrency, "model": MODEL, "provider_id": PROVIDER_ID,
            "reasoning_effort": "none", "model_api_calls": 0, "judge_api_calls": 0}))
        return 0
    from aster.evaluation.bfcl_current import capture_current_profile
    from aster.evaluation.bfcl_multiturn_a6 import finalize_campaign, initialize_campaign
    from aster.paths import resolve_data_dir
    from aster.platform.filesystem import FileLock

    with FileLock(campaign / "launch.lock", timeout=.1):
        started = time.monotonic()
        _helpers.new_json(campaign / "execution-started.json", {"mode": plan["variant"],
            "started_at": _helpers.now(), "authorized_unlimited_api": True, "api_call_limit": None,
            "backend_mode": BACKEND_MODE, "maximum_concurrency": args.concurrency,
            "campaign_plan_sha256": _helpers.digest(_helpers.encoded(plan)),
            "launcher_sha256": _helpers.digest(Path(__file__).read_bytes())})
        try:
            _helpers.snapshot_sources(campaign)
            cases = load_development_cases()
            if [case["id"] for case in cases] != [group["group"] for group in plan["groups"]]:
                raise ValueError("Development cases differ from the frozen execution order")
            profile = capture_current_profile(resolve_data_dir(), provider_id=PROVIDER_ID,
                model=MODEL, reasoning_effort="none")
            if (profile.provider_id != PROVIDER_ID or profile.base_url != BASE_URL or profile.protocol != PROTOCOL
                    or profile.model != MODEL or profile.reasoning_effort != "none"):
                raise ValueError("The requested A6 no-thinking profile is unavailable")
            public = profile.public_summary()
            public = {key: public[key] for key in ("provider_id", "provider_name", "base_url", "protocol", "model",
                "reasoning_effort", "identity_revision", "capabilities") if key in public}
            public["capabilities"] = {key: public.get("capabilities", {})[key] for key in (
                "id", "name", "context_window", "max_output_tokens", "input_modalities", "effort_levels")
                if key in public.get("capabilities", {})}
            _helpers.new_json(campaign / "model-selection.json", public)
            network = _network_admission(profile, plan["http_dns_fallback"])
            _helpers.new_json(campaign / "network-admission.json", network)
            control = initialize_campaign(cases, campaign, profile=profile, maximum_concurrency=args.concurrency,
                max_api_calls=None, max_output_tokens=8192, authorized_unlimited_api=True,
                http_dns_fallback=plan["http_dns_fallback"], provider_failure_policy=PROVIDER_FAILURE_POLICY)
            output = campaign / "promptfoo"
            (output / "dispatches").mkdir(parents=True)
            _helpers.new_json(output / "promptfooconfig.json", build_config(plan, campaign))
            shutil.copyfile(HERE / "a6_no_thinking_assertion.py", output / "a6_no_thinking_assertion.py")
            (output / "empty.env").write_text("", encoding="utf-8")
            cli = PROJECT / ".eval-tools/promptfoo/node_modules/promptfoo/dist/src/entrypoint.js"
            if json.loads((cli.parents[2] / "package.json").read_bytes())["version"] != "0.125.1":
                raise ValueError("This launcher requires pinned promptfoo 0.125.1")
            print(json.dumps({"mode": plan["variant"], "tasks": 200, "same_campaign": True,
                "concurrency": args.concurrency, "provider_id": PROVIDER_ID, "model": MODEL,
                "reasoning_effort": "none"}), flush=True)
            env = child_environment(output, control, network)
            env["PROMPTFOO_PYTHON_WORKERS"] = str(args.concurrency)
            completed = subprocess.run(cli_command(output, args.concurrency), cwd=output, env=env,
                text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=14400, check=False)
            (output / "cli.stdout.txt").write_text(completed.stdout, encoding="utf-8")
            (output / "cli.stderr.txt").write_text(completed.stderr, encoding="utf-8")
            finalize_campaign(campaign)
            collected = collect_campaign(campaign, plan, json.loads((output / "results.json").read_bytes()), output)
            guards = [json.loads(path.read_bytes()) for path in output.glob("*-guard.*.json")]
            blocked = sum(row.get("blocked_attempts", row.get("external_attempts", 0)) for row in guards)
            roles = {row.get("entry_script") for row in guards}
            if not any("external_attempts" in row for row in guards) or not {"persistent_wrapper.py", "wrapper.py"} <= roles:
                collected["evidence_errors"].append("missing_node_provider_or_assertion_guards")
            if blocked:
                collected["evidence_errors"].append("endpoint_or_process_guard_violation")
            collected.update(finished_at=_helpers.now(), elapsed_seconds=round(time.monotonic() - started, 4),
                native_exit_code=completed.returncode, guard_blocked_attempts=blocked,
                authorized_unlimited_api=True, api_call_limit=None, provider_id=PROVIDER_ID,
                model=MODEL, reasoning_effort="none", wire_policy=WIRE_POLICY)
            collected["evidence_integrity_verified"] = completed.returncode in {0, 100} and not collected["evidence_errors"]
            collected["workflow_completed"] = collected["workflow_completed"] and collected["evidence_integrity_verified"]
            collected["integration_verified"] = (collected["evidence_integrity_verified"]
                and collected["baseline_assessment"]["capability_baseline_ready"])
            _helpers.new_json(campaign / "report.json", collected)
            _helpers.new_json(campaign / "execution-finished.json", collected)
            print(json.dumps(collected), flush=True)
            return 0 if collected["integration_verified"] else 2
        except BaseException as error:
            _helpers.new_json(campaign / "execution-failure.json", {"finished_at": _helpers.now(),
                "error_type": type(error).__name__, "elapsed_seconds": round(time.monotonic() - started, 4),
                "automatic_replay": False})
            raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - never expose credential/provider prose.
        print(json.dumps({"a6_concurrent_launcher_error_type": type(error).__name__, "automatic_replay": False}), file=sys.stderr)
        raise SystemExit(2) from None
