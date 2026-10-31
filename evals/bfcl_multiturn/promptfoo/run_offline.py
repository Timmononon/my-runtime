"""Generate and run promptfoo locally against frozen Aster task bundles.

No generated prompt, provider, assertion or grader may call a model. Multiple
bundles enable comparisons only for configurations that were really recorded.
The CLI compares shared task IDs; unpaired IDs remain explicitly listed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path

from aster.evaluation.bfcl_multiturn_export import load_bundle

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[2]


def build_config(bundle_dirs, *, python_executable):
    groups = {}
    labels_by_bundle = {}
    identities = {}
    for directory in bundle_dirs:
        root = Path(directory).resolve()
        records = load_bundle(root)
        for row in records:
            label = row["config_label"]
            identity = json.dumps({key: row[key] for key in
                                   ("source_identity", "config_identity", "model_identity")}, sort_keys=True)
            if label in identities and identities[label] != identity:
                raise ValueError("A configuration label cannot mix different recorded identities")
            identities[label] = identity
            if label in labels_by_bundle and labels_by_bundle[label] != root:
                raise ValueError("A configuration label must identify only one bundle")
            labels_by_bundle[label] = root
            group = groups.setdefault(label, {})
            if row["task_id"] in group:
                raise ValueError("Duplicate task/configuration pair")
            group[row["task_id"]] = row
    if not groups:
        raise ValueError("At least one nonempty saved-result bundle is required")
    common = set.intersection(*(set(rows) for rows in groups.values()))
    union = set.union(*(set(rows) for rows in groups.values()))
    if not common:
        raise ValueError("No shared task IDs exist for a paired replay")
    shared = {task_id for task_id in common
              if all(rows[task_id]["scoring"]["passed"] is not None for rows in groups.values())}
    if not shared:
        raise ValueError("No scoreable shared tasks; data conflicts/infrastructure/not-run stay in the source bundle")
    for task_id in shared:
        bindings = {(rows[task_id]["subcategory"], rows[task_id]["provenance"]["question_sha256"],
                     rows[task_id]["scoring"]["source_revision"], rows[task_id]["scoring"]["scoring_rule"])
                    for rows in groups.values()}
        if len(bindings) != 1:
            raise ValueError("Shared task question/category/scoring identities differ")
    providers = [{
        "id": "file://" + (HERE / "provider.py").as_posix(), "label": label,
        "config": {"bundle_dir": labels_by_bundle[label].as_posix(), "config_label": label,
                   "pythonExecutable": str(python_executable), "workers": 1, "timeout": 60000},
    } for label in sorted(groups)]
    tests = [{"description": task_id, "vars": {"task_id": task_id},
              "metadata": {"subcategory": next(iter(groups.values()))[task_id]["subcategory"],
                           "model_capability_measurement": False,
                           "record_digests": {label: hashlib.sha256(json.dumps(rows[task_id], sort_keys=True,
                               separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
                               for label, rows in groups.items()}}}
             for task_id in sorted(shared)]
    config = {
        "description": "Offline Aster BFCL V4 complete-task saved-result replay; no new model score",
        "prompts": ["Replay saved complete task {{task_id}}"], "providers": providers,
        "tests": tests, "sharing": False,
        "defaultTest": {"assert": [{"type": "python", "metric": "saved_bfcl_success",
                                     "value": "file://assertion.py"}]},
        "evaluateOptions": {"maxConcurrency": 1, "showProgressBar": False},
    }
    comparison = {"mode": "saved_result_offline", "new_model_api_calls": 0,
                  "new_judge_api_calls": 0, "model_capability_measurement": False,
                  "paired_task_ids": sorted(shared), "unpaired_task_ids": sorted(union - common),
                  "excluded_unscoreable_task_ids": sorted(common - shared),
                  "excluded_unscoreable_records": [
                      {"task_id": task_id, "config_label": label, "outcome": rows[task_id]["outcome"]}
                      for label, rows in sorted(groups.items()) for task_id in sorted(common - shared)],
                  "configs": {}}
    for label, rows in sorted(groups.items()):
        paired = [rows[task_id] for task_id in sorted(shared)]
        outcomes = Counter(row["outcome"]["category"] for row in paired)
        scoreable = [row for row in paired if row["scoring"]["passed"] is not None]
        def total(field, paired=paired):
            values = [row["recorded_usage"].get(field) for row in paired]
            return {"known_total": sum(value for value in values if value is not None),
                    "unknown_tasks": sum(value is None for value in values)}
        comparison["configs"][label] = {
            "recorded_tasks": len(rows), "paired_tasks": len(paired),
            "source_identity": paired[0]["source_identity"],
            "config_identity": paired[0]["config_identity"], "model_identity": paired[0]["model_identity"],
            "missing_task_ids": sorted(union - set(rows)), "outcomes": dict(outcomes),
            "recorded_successes": sum(row["scoring"]["passed"] is True for row in scoreable),
            "scoreable_tasks": len(scoreable),
            "recorded_success_rate": (sum(row["scoring"]["passed"] is True for row in scoreable)
                                      / len(scoreable)) if scoreable else None,
            "recorded_usage": {key: total(key) for key in (
                "model_requests", "input_tokens", "output_tokens", "elapsed_seconds")},
            "recorded_failures": [{"task_id": row["task_id"], **row["outcome"]}
                                  for row in paired if row["outcome"]["category"] != "pass"],
            "expected_assertion_pass": {row["task_id"]: row["scoring"]["passed"] for row in paired},
        }
    return config, comparison


def child_environment(output, python_executable):
    # An explicit allowlist prevents provider keys/cloud credentials inherited
    # from the caller, and a dedicated cwd/empty env file avoids project .env.
    allowed = ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT")
    env = {name: os.environ[name] for name in allowed if name in os.environ}
    state = output / "local-state"
    for name in ("home", "tmp", "config", "cache"):
        (state / name).mkdir(parents=True)
    env.update({
        "HOME": str(state / "home"), "USERPROFILE": str(state / "home"),
        "TEMP": str(state / "tmp"), "TMP": str(state / "tmp"),
        "XDG_CONFIG_HOME": str(state / "config"), "XDG_CACHE_HOME": str(state / "cache"),
        "PROMPTFOO_CONFIG_DIR": str(state / "config"), "PROMPTFOO_CACHE_PATH": str(state / "cache"),
        "PROMPTFOO_DISABLE_TELEMETRY": "1", "PROMPTFOO_DISABLE_UPDATE": "1",
        "PROMPTFOO_DISABLE_SHARING": "1", "PROMPTFOO_DISABLE_REDTEAM_REMOTE_GENERATION": "1",
        "PROMPTFOO_ENABLE_OTEL": "false", "PROMPTFOO_TRACING_ENABLED": "false",
        "OTEL_SDK_DISABLED": "true", "CI": "1", "NO_COLOR": "1",
        "PROMPTFOO_PYTHON": str(python_executable), "PROMPTFOO_PYTHON_WORKERS": "1",
        "PYTHONPATH": os.pathsep.join((str(HERE / "guards"), str(PROJECT / "src"))),
        "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "NODE_OPTIONS": '--require="' + (HERE / "network_guard.cjs").as_posix() + '"',
        "ASTER_PROMPTFOO_NODE_GUARD": str(output / "node-guard"),
        "ASTER_PROMPTFOO_PYTHON_GUARD": str(output / "python-guard"),
    })
    return env


def validate_framework_results(payload, comparison):
    """A framework assertion failure is expected only if frozen BFCL failed."""
    errors = []
    results = payload.get("results", {})
    stats = results.get("stats", {})
    rows = results.get("results", [])
    expected = {(label, task_id): passed
                for label, config in comparison["configs"].items()
                for task_id, passed in config["expected_assertion_pass"].items()}
    if stats.get("errors") != 0:
        errors.append("framework_errors")
    if len(rows) != len(expected):
        errors.append("unexpected_result_count")
    seen = set()
    for row in rows:
        key = (row.get("provider", {}).get("label"), row.get("vars", {}).get("task_id"))
        if key in seen or key not in expected:
            errors.append("duplicate_or_unexpected_task_configuration")
            continue
        seen.add(key)
        if type(row.get("success")) is not bool or row["success"] != expected[key]:
            errors.append("saved_score_mismatch")
        components = row.get("gradingResult", {}).get("componentResults", [])
        if len(components) != 1 or not components[0].get("reason", "").startswith("Saved BFCL result: "):
            errors.append("custom_assertion_not_completed")
        response = row.get("response", {})
        try:
            output = json.loads(response.get("output", ""))
        except (TypeError, ValueError):
            errors.append("invalid_provider_record")
            continue
        if (output.get("task_id") != key[1] or output.get("config_label") != key[0]
                or output.get("scoring", {}).get("passed") != expected[key]
                or output.get("replay", {}).get("new_model_api_calls") != 0
                or output.get("replay", {}).get("new_judge_api_calls") != 0):
            errors.append("provider_record_mismatch")
    if seen != set(expected):
        errors.append("missing_result_pairs")
    expected_passes = sum(expected.values())
    if stats.get("successes") != expected_passes or stats.get("failures") != len(expected) - expected_passes:
        errors.append("framework_totals_mismatch")
    if stats.get("tokenUsage", {}).get("total") != 0:
        errors.append("replay_reported_new_tokens")
    return sorted(set(errors))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=PROJECT / ".venv/Scripts/python.exe")
    parser.add_argument("--node", default=shutil.which("node"))
    parser.add_argument("--generate-only", action="store_true")
    args = parser.parse_args(argv)
    python_executable = args.python.resolve()
    if not python_executable.is_file():
        parser.error("An installed project-local Python executable is required")
    output = args.output.resolve()
    if output.exists():
        parser.error("Use a new output directory; saved evidence cannot be overwritten")
    config, comparison = build_config(args.bundle, python_executable=python_executable)
    output.mkdir(parents=True)
    # promptfoo 0.125.1 treats ':' in assertion file references as a function
    # suffix, so an absolute Windows drive path is not a usable assertion URL.
    shutil.copyfile(HERE / "assertion.py", output / "assertion.py")
    (output / "promptfooconfig.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    (output / "comparison.json").write_text(json.dumps(comparison, indent=2), encoding="utf-8")
    (output / "empty.env").write_text("", encoding="utf-8")
    if args.generate_only:
        return 0
    cli = PROJECT / ".eval-tools/promptfoo/node_modules/promptfoo/dist/src/entrypoint.js"
    if not cli.is_file() or not args.node:
        parser.error("Install the pinned project-local promptfoo package first")
    package = json.loads((cli.parents[2] / "package.json").read_text(encoding="utf-8"))
    if package["version"] != "0.125.1":
        parser.error("This offline preload is verified only with pinned promptfoo 0.125.1")
    command = [args.node, str(cli), "eval", "--config", str(output / "promptfooconfig.json"),
               "--env-file", str(output / "empty.env"), "--output", str(output / "results.json"),
               "--no-cache", "--no-share", "--no-write", "--no-progress-bar", "--no-table",
               "--max-concurrency", "1"]
    completed = subprocess.run(command, cwd=output, env=child_environment(output, python_executable),
                               text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
    (output / "cli.stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (output / "cli.stderr.txt").write_text(completed.stderr, encoding="utf-8")
    guard_files = sorted(output.glob("*-guard.*.json"))
    guards = [json.loads(path.read_text(encoding="utf-8")) for path in guard_files]
    attempts = sum(row.get("external_attempts", row.get("external_or_process_attempts", 0)) for row in guards)
    roles = {row.get("entry_script") for row in guards}
    framework_validation = ["missing_results"]
    if (output / "results.json").is_file():
        try:
            framework_validation = validate_framework_results(
                json.loads((output / "results.json").read_text(encoding="utf-8")), comparison)
        except (TypeError, ValueError):
            framework_validation = ["malformed_results"]
    receipt = {"command": command, "exit_code": completed.returncode, "guard_files": [p.name for p in guard_files],
               "blocked_external_or_process_attempts": attempts, "new_model_api_calls": 0,
               "new_judge_api_calls": 0, "mode": "saved_result_offline",
               "model_capability_measurement": False, "framework_validation_errors": framework_validation,
               "local_provider_calls": len(config["tests"]) * len(config["providers"]),
               "python_guard_roles": sorted(role for role in roles if role), "promptfoo_version": package["version"]}
    (output / "run-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps(receipt))
    # promptfoo uses 100 for legitimate saved assertion failures. Preserve it in
    # the receipt but allow the offline harness to succeed after verifying JSON.
    if (completed.returncode not in {0, 100} or attempts or framework_validation
            or not any(path.name.startswith("node-guard.") for path in guard_files)
            or not {"persistent_wrapper.py", "wrapper.py"} <= roles):
        return 2
    if not (output / "results.json").is_file():
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
