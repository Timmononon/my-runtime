"""Explicit once-only promptfoo orchestration of the frozen twenty Aster tasks."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[2]
CORPUS = PROJECT / "artifacts/bfcl/v4-coverage-20261029"
DEFAULT_CAMPAIGN = PROJECT / "artifacts/bfcl/multiturn20-live-20261030"
CATEGORIES = ("multi_turn_base", "multi_turn_miss_func", "multi_turn_miss_param", "multi_turn_long_context")
MANIFEST_SHA = "f0c5f6e2d07826c0db8a7029b32742a2ba4e12e6a792a2aace7ad94089917614"
PROVENANCE_SHA = "654544beaafcb071f46da5101b5e0c539295506b39a5e227c0ba00813cdded48"


def digest(value):
    return hashlib.sha256(value).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def new_json(path, value):
    with path.open("xb") as output:
        output.write(encoded(value))


def now():
    return datetime.now(UTC).isoformat()


def preflight():
    """Only local standard-library reads; no credentials, Runtime or API client."""
    manifest_payload = (CORPUS / "sample-manifest.json").read_bytes()
    provenance_payload = (CORPUS / "provenance.json").read_bytes()
    if digest(manifest_payload) != MANIFEST_SHA or digest(provenance_payload) != PROVENANCE_SHA:
        raise ValueError("Pinned corpus metadata changed")
    manifest, provenance = json.loads(manifest_payload), json.loads(provenance_payload)
    hashes = {"sample-manifest.json": MANIFEST_SHA, "provenance.json": PROVENANCE_SHA}
    for item in provenance["files"]:
        relative = Path(item["local_file"])
        target = (CORPUS / relative).resolve()
        if relative.is_absolute() or not target.is_relative_to(CORPUS.resolve()) or digest(target.read_bytes()) != item["sha256"]:
            raise ValueError("Pinned corpus source changed")
    samples = {sample["track"]: sample for sample in manifest["samples"]}
    by_category = {}
    for category in CATEGORIES:
        sample = samples[category]
        rows, answer_ids = None, None
        for entry in sample["files"]:
            payload = (CORPUS / entry["file"]).read_bytes()
            if digest(payload) != entry["sha256"]:
                raise ValueError("Frozen sample bytes changed")
            hashes[entry["file"]] = entry["sha256"]
            parsed = [json.loads(line) for line in payload.decode().splitlines() if line.strip()]
            if "/answers/" in entry["file"]:
                answer_ids = {row["id"] for row in parsed}
            else:
                rows = parsed
        if rows is None or len(rows) != 5 or {row["id"] for row in rows} != answer_ids:
            raise ValueError("Each selected Multi-Turn class must retain five paired complete tasks")
        by_category[category] = []
        for row in rows:
            canonical = json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
            expected = sample["row_sha256"][row["id"]]
            if digest(canonical) != expected:
                raise ValueError("Selected task differs from its pinned record")
            by_category[category].append({"group": row["id"], "entry_ids": [row["id"]], "category": category,
                "source_kind": "official_fixed_revision_complete_task", "user_turns": len(row["question"]),
                "question_sha256": expected, "api_call_cap": 20})
    groups = [by_category[category][index] for index in range(5) for category in CATEGORIES]
    return {"schema_version": 1, "campaign": "multiturn20-live-20261030",
        "variant": "current_aster_promptfoo_orchestrated_multiturn20", "source_revision": manifest["revision"],
        "corpus": CORPUS.name, "input_sha256": hashes, "groups": groups,
        "planned_entries": 20, "planned_assessments": 20, "planned_budget_groups": 20,
        "authorized_api_call_limit": 200, "max_output_tokens": 2048,
        "per_case_api_call_cap": 20, "reserve_for_each_later_admissible_task": 6,
        "known_admission_conflicts": ["multi_turn_base_173"],
        "order_policy": "Frozen sample file order interleaved across the four categories; no outcome-based selection.",
        "budget_policy": "One durable predispatch global 200 HTTP ledger; failed requests count; adaptive per-case cap <=20 reserves six calls for each later valid task.",
        "orchestrator": "promptfoo 0.125.1 native CLI invokes one Python provider per complete task, concurrency 1",
        "provider_retries": 0, "engine_retries": 0, "metadata_requests": 0, "cache": False,
        "official_score": False, "full_leaderboard_score": None,
        "oracle_policy": "Official answers/private cases stay evaluator-only; promptfoo receives task IDs, not gold.",
        "replay_policy": "Never resume this CLI campaign or rerun an intent with unknown outcome. Durable completed receipts are read-only deduplicated.",
        "scope_note": "Twenty fixed-subset tasks, not a held-out optimization experiment or complete leaderboard score."}


def source_files():
    paths = []
    for base in (PROJECT / "src/aster", HERE):
        for path in sorted(base.rglob("*")):
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            if path.is_symlink():
                raise ValueError("Source snapshot cannot follow symlinks")
            if path.is_file():
                paths.append(path)
    return paths + [PROJECT / "pyproject.toml", PROJECT / ".eval-tools/promptfoo/package-lock.json"]


def supplement_plan(original_dir):
    """Authorize only zero-dispatch not_run tasks under the original unused cap.

    This is a separate campaign, never a resume/retry of a prior task. Original
    executed and failed tasks stay immutable and cannot enter this selection.
    """
    original = Path(original_dir).resolve()
    if not original.is_dir():
        raise ValueError("An existing completed initial campaign is required")

    def read(name):
        return json.loads((original / name).read_text(encoding="utf-8"))

    initial, finished = read("campaign-plan.json"), read("execution-finished.json")
    control, report, ledger = read("execution-control.json"), read("results/results.json"), read("results/http-budget.json")
    if (initial.get("supplement") is not None or initial.get("planned_entries") != 20
            or initial.get("authorized_api_call_limit") != 200 or control.get("max_api_calls") != 200
            or ledger.get("maximum") != 200 or report.get("max_api_calls") != 200):
        raise ValueError("Only the original once-only 200-request campaign may be supplemented")
    binding = control.get("dataset_binding")
    if not binding or report.get("dataset_binding") != binding or ledger.get("binding") != binding:
        raise ValueError("Original control, results and HTTP ledger have different bindings")
    calls = ledger["calls"]
    if any(call.get("status") == "outcome_unknown" for call in calls):
        raise ValueError("Unknown original request outcomes block supplementary execution")
    used = len(calls)
    if (not 0 < used < 200 or finished.get("model_api_calls") != used
            or finished.get("http_transport_attempts") != used or report.get("model_api_calls") != used):
        raise ValueError("Original final accounting does not establish the remaining authorization")
    rows = report["results"]
    by_id = {row["id"]: row for row in rows}
    planned_ids = [identifier for group in initial["groups"] for identifier in group["entry_ids"]]
    if len(rows) != 20 or len(by_id) != 20 or set(by_id) != set(planned_ids):
        raise ValueError("Original campaign must retain exactly its twenty original task rows")
    eligible = set()
    for identifier, row in by_id.items():
        actual = sum(call.get("case_id") == identifier for call in calls)
        if actual != row.get("model_api_calls"):
            raise ValueError("Original task accounting differs from the transport ledger")
        if row.get("result") == "not_run" or row.get("status") == "not_run":
            if actual or row.get("model_api_calls") != 0 or row.get("runs") or row.get("messages"):
                raise ValueError("A previously entered or dispatched task cannot be selected")
            eligible.add(identifier)
    if not eligible:
        raise ValueError("No never-dispatched not_run tasks remain")
    frozen = preflight()
    frozen_ids = {group["group"] for group in frozen["groups"]}
    if set(planned_ids) != frozen_ids:
        raise ValueError("Original selection differs from the frozen twenty-task cohort")
    # Pin existing original evidence, not new diagnostic files later added by a
    # read-only audit. Verify source authority against its copied as-run bytes.
    critical = {Path(name) for name in ("campaign-plan.json", "execution-started.json", "execution-finished.json",
        "execution-control.json", "source-manifest.json", "model-selection.json", "results/results.json",
        "results/http-budget.json", "promptfoo/results.json")}
    for path in (original / "results").rglob("*"):
        if path.is_file():
            critical.add(path.relative_to(original))
    source = read("source-manifest.json")
    for entry in source["files"]:
        relative = Path(entry["file"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Original source snapshot paths are invalid")
        path = original / "as-run-source" / relative
        if digest(path.read_bytes()) != entry["sha256"]:
            raise ValueError("Original as-run source authority changed")
        critical.add(path.relative_to(original))
    hashes = {path.as_posix(): digest((original / path).read_bytes()) for path in sorted(critical)}
    frozen["groups"] = [group for group in frozen["groups"] if group["group"] in eligible]
    frozen.update(campaign="multiturn20-supplement-20261030",
        variant="current_aster_promptfoo_never_dispatched_tasks_only",
        planned_entries=len(eligible), planned_assessments=len(eligible), planned_budget_groups=len(eligible),
        authorized_api_call_limit=200-used,
        supplement={"original_campaign": str(original), "original_dataset_binding": binding,
            "original_authorized_api_call_limit": 200, "original_actual_http_attempts": used,
            "remaining_api_call_limit": 200-used, "selected_task_ids": [group["group"] for group in frozen["groups"]],
            "original_evidence_sha256": hashes, "original_profile": read("model-selection.json"),
            "policy": "Only original not_run plus zero HTTP and no entered user turn. Never retry a previously failed/entered task."})
    frozen["budget_policy"] = (f"Separate durable cap {200-used}, original {used} actual calls plus this cap <=200; "
                               "failed calls count; no previous task retry or recursive supplement.")
    return frozen


def snapshot_sources(campaign):
    destination = campaign / "as-run-source"
    destination.mkdir()
    records = []
    for path in source_files():
        relative, payload = path.relative_to(PROJECT), path.read_bytes()
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(payload)
        records.append({"file": relative.as_posix(), "sha256": digest(payload), "bytes": len(payload)})
    distributions = sorted({(item.metadata.get("Name", "unknown"), item.version)
                            for item in importlib.metadata.distributions()})
    manifest = {"schema_version": 1, "captured_at": now(), "files": records,
        "python_version": sys.version, "platform": platform.platform(),
        "installed_distributions": [{"name": name, "version": version} for name, version in distributions],
        "excluded": ["user Runtime", "credentials", "cache", "*.pyc"],
        "authority": "Exact copied source bytes, not Git HEAD alone."}
    new_json(campaign / "source-manifest.json", manifest)
    return manifest


def build_config(plan, campaign):
    return {"description": "LIVE Aster BFCL V4 twenty complete Multi-Turn tasks, global 200 API cap",
        "prompts": ["Execute the authoritative complete task {{task_id}}"], "sharing": False,
        "providers": [{"id": "file://" + (HERE / "live_provider.py").as_posix(),
            "label": "current-aster-multiturn20", "config": {"campaign_dir": campaign.as_posix(),
                "pythonExecutable": str(PROJECT / ".venv/Scripts/python.exe"), "workers": 1, "timeout": 720000}}],
        "tests": [{"description": group["group"], "vars": {"task_id": group["group"]},
            "metadata": {"subcategory": group["category"], "campaign_dir": campaign.as_posix()}}
            for group in plan["groups"]],
        "defaultTest": {"assert": [{"type": "python", "metric": "live_bfcl_complete_task",
            "value": "file://live_assertion.py"}]},
        "evaluateOptions": {"maxConcurrency": 1, "showProgressBar": False, "repeat": 1}}


def child_environment(output, control):
    from run_offline import child_environment as offline_environment

    env = offline_environment(output, PROJECT / ".venv/Scripts/python.exe")
    endpoints = []
    profile = control.get("profile", control.get("public_profile", {}))
    parsed = urlsplit(profile["base_url"])
    endpoints.append([parsed.hostname, parsed.port or 443])
    for key, value in os.environ.items():
        if key.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}:
            env[key] = value
            if key.upper() != "NO_PROXY":
                proxy = urlsplit(value if "://" in value else "http://" + value)
                if not proxy.hostname:
                    raise ValueError("Configured proxy does not have a valid hostname")
                endpoints.append([proxy.hostname, proxy.port or (443 if proxy.scheme == "https" else 80)])
    env["PYTHONPATH"] = os.pathsep.join((str(HERE / "live_guards"), str(PROJECT / "src")))
    env["ASTER_PF_LIVE_ENDPOINTS"] = json.dumps(endpoints)
    env["ASTER_PF_LIVE_SOURCE_RUNTIME"] = control["source_runtime"]
    env["PROMPTFOO_MAX_RETRIES"] = "0"
    return env


def compact_digest(row):
    return digest(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())


def validate_native(payload, rows):
    errors, seen = [], set()
    expected = {row["id"]: row for row in rows}
    native = payload.get("results", {})
    outputs = native.get("results", [])
    if len(outputs) != len(expected):
        errors.append("native_task_count_mismatch")
    excluded = 0
    for native_row in outputs:
        task_id = native_row.get("vars", {}).get("task_id")
        if task_id in seen or task_id not in expected:
            errors.append("duplicate_or_unknown_task")
            continue
        seen.add(task_id)
        row = expected[task_id]
        if native_row.get("provider", {}).get("label") != "current-aster-multiturn20":
            errors.append("unexpected_native_provider_identity")
        try:
            compact = json.loads(native_row.get("response", {}).get("output", ""))
        except (TypeError, ValueError):
            errors.append("missing_compact_provider_output")
            continue
        if (compact.get("id") != task_id or compact.get("result") != row.get("result")
                or compact.get("backend_result_sha256") != compact_digest(row)
                or compact.get("model_api_calls") != row.get("model_api_calls", 0)
                or compact.get("judge_api_calls") != 0):
            errors.append("native_backend_receipt_mismatch")
        if row.get("result") in {"pass", "fail"}:
            if (isinstance(compact.get("main_score"), bool)
                    or compact.get("main_score") != float(row["result"] == "pass")):
                errors.append("compact_main_score_mismatch")
            if native_row.get("success") is not (row["result"] == "pass"):
                errors.append("native_official_score_mismatch")
            components = native_row.get("gradingResult", {}).get("componentResults", [])
            if len(components) != 1 or not components[0].get("reason", "").startswith("Live BFCL official complete-task result:"):
                errors.append("live_assertion_did_not_complete")
        else:
            excluded += 1
            if compact.get("main_score") is not None or "EXCLUDED " not in str(native_row.get("error", "")):
                errors.append("unscorable_outcome_not_excluded")
    if seen != set(expected):
        errors.append("missing_native_tasks")
    if native.get("stats", {}).get("errors") != excluded:
        errors.append("unexpected_native_framework_errors")
    return sorted(set(errors))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-dir", type=Path, default=DEFAULT_CAMPAIGN)
    parser.add_argument("--uninvoked-from", type=Path,
                        help="Separate continuation for only original zero-dispatch not_run rows; no failed task retries")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    campaign = args.campaign_dir.resolve()
    if args.uninvoked_from and campaign == DEFAULT_CAMPAIGN.resolve():
        campaign = DEFAULT_CAMPAIGN.with_name("multiturn20-supplement-20261030")
    plan = supplement_plan(args.uninvoked_from) if args.uninvoked_from else preflight()
    if args.uninvoked_from and campaign == args.uninvoked_from.resolve():
        parser.error("Supplement must use a separate new campaign directory")
    task_count, api_limit = plan["planned_entries"], plan["authorized_api_call_limit"]
    campaign.mkdir(parents=True, exist_ok=True)
    if any((campaign / name).exists() for name in ("execution-started.json", "source-manifest.json", "results", "promptfoo")):
        parser.error("Campaign execution already exists; resuming or replaying is forbidden")
    plan_path = campaign / "campaign-plan.json"
    if plan_path.exists():
        if json.loads(plan_path.read_text(encoding="utf-8")) != plan:
            parser.error("Saved campaign plan differs from the frozen tasks")
    else:
        new_json(plan_path, plan)
    if not args.execute:
        print(json.dumps({"mode": "preflight_only", "tasks": task_count, "subcategories": 4,
            "api_limit_if_executed": api_limit, "model_api_calls": 0, "judge_api_calls": 0}))
        return 0
    from aster.evaluation.bfcl_current import capture_current_profile
    from aster.evaluation.bfcl_multiturn_live import finalize_campaign, initialize_campaign
    from aster.evaluation.bfcl_v4_data import load_corpus
    from aster.paths import resolve_data_dir
    from aster.platform.filesystem import FileLock

    with FileLock(campaign / "launch.lock", timeout=.1):
        started = time.monotonic()
        new_json(campaign / "execution-started.json", {"mode": "promptfoo_orchestrated_live",
            "started_at": now(), "authorized_api_call_limit": api_limit, "campaign_plan_sha256": digest(encoded(plan)),
            "launcher_sha256": digest(Path(__file__).read_bytes())})
        try:
            snapshot_sources(campaign)
            cases_by_id = {case["id"]: case for case in load_corpus(CORPUS, tracks=list(CATEGORIES))}
            cases = [cases_by_id[group["group"]] for group in plan["groups"]]
            profile = capture_current_profile(resolve_data_dir())
            public = profile.public_summary()
            public = {key: public[key] for key in ("provider_id", "provider_name", "base_url", "protocol", "model",
                "reasoning_effort", "identity_revision", "capabilities") if key in public}
            public["capabilities"] = {key: public.get("capabilities", {})[key] for key in (
                "id", "name", "context_window", "max_output_tokens", "input_modalities", "effort_levels")
                if key in public.get("capabilities", {})}
            new_json(campaign / "model-selection.json", public)
            if args.uninvoked_from:
                expected = plan["supplement"]["original_profile"]
                identity_keys = ("provider_id", "base_url", "protocol", "model", "reasoning_effort", "identity_revision")
                if any(public.get(key) != expected.get(key) for key in identity_keys):
                    raise ValueError("Supplement cannot change the original provider/model/effort identity")
                if supplement_plan(args.uninvoked_from) != plan:
                    raise ValueError("Original proof changed before supplementary dispatch")
            control = initialize_campaign(cases, campaign, profile=profile, max_api_calls=api_limit, max_output_tokens=2048)
            output = campaign / "promptfoo"
            output.mkdir()
            config = build_config(plan, campaign)
            new_json(output / "promptfooconfig.json", config)
            shutil.copyfile(HERE / "live_assertion.py", output / "live_assertion.py")
            (output / "empty.env").write_text("", encoding="utf-8")
            cli = PROJECT / ".eval-tools/promptfoo/node_modules/promptfoo/dist/src/entrypoint.js"
            package = json.loads((cli.parents[2] / "package.json").read_text(encoding="utf-8"))
            if package["version"] != "0.125.1":
                raise ValueError("Live runner is pinned to promptfoo 0.125.1")
            command = [shutil.which("node"), str(cli), "eval", "--config", str(output / "promptfooconfig.json"),
                "--env-file", str(output / "empty.env"), "--output", str(output / "results.json"), "--no-cache",
                "--no-share", "--no-write", "--no-progress-bar", "--no-table", "--max-concurrency", "1"]
            print(json.dumps({"mode": "promptfoo_orchestrated_live", "tasks": task_count, "api_limit": api_limit,
                              "model": public["model"], "effort": public["reasoning_effort"]}), flush=True)
            completed = subprocess.run(command, cwd=output, env=child_environment(output, control), text=True,
                encoding="utf-8", errors="replace", capture_output=True, timeout=2700, check=False)
            (output / "cli.stdout.txt").write_text(completed.stdout, encoding="utf-8")
            (output / "cli.stderr.txt").write_text(completed.stderr, encoding="utf-8")
            report = finalize_campaign(campaign)
            validation = validate_native(json.loads((output / "results.json").read_text(encoding="utf-8")), report["results"])
            guards = [json.loads(path.read_text(encoding="utf-8")) for path in output.glob("*-guard.*.json")]
            blocked = sum(row.get("blocked_attempts", row.get("external_attempts", 0)) for row in guards)
            if blocked:
                validation.append("offline_or_endpoint_guard_violation")
            roles = {guard.get("entry_script") for guard in guards}
            if (not any("external_attempts" in guard for guard in guards)
                    or not {"persistent_wrapper.py", "wrapper.py"} <= roles):
                validation.append("missing_node_or_provider_assertion_guards")
            receipt = {"finished_at": now(), "elapsed_seconds": round(time.monotonic() - started, 4),
                "orchestrator": "promptfoo", "native_exit_code": completed.returncode,
                "planned_entries": task_count, "completed_result_rows": len(report["results"]),
                "model_api_calls": report["model_api_calls"], "http_transport_attempts": report["http_transport_attempts"],
                "totals": report["totals"], "framework_validation_errors": validation, "guard_blocked_attempts": blocked,
                "judge_api_calls": 0, "official_score": False, "full_leaderboard_score": None,
                "native_errors_note": "Recognized unscorable task rows are excluded from the BFCL denominator, not model failures."}
            new_json(campaign / "execution-finished.json", receipt)
            print(json.dumps(receipt), flush=True)
            return 0 if completed.returncode in {0, 100} and not validation else 2
        except BaseException as error:
            new_json(campaign / "execution-failure.json", {"finished_at": now(), "error_type": type(error).__name__,
                "elapsed_seconds": round(time.monotonic() - started, 4), "automatic_replay": False})
            raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - never expose provider/credential exception prose.
        print(json.dumps({"live_launcher_error_type": type(error).__name__, "automatic_replay": False}), file=sys.stderr)
        raise SystemExit(2) from None
