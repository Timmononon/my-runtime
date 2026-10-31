"""Guarded comparison worker. Public entry: run_comparison.py."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import sys
from pathlib import Path


def configure_host_metadata():
    """Bound Windows metadata reads inside this isolated offline process only.

    Python 3.12's platform.system() can wait indefinitely in WMI through uname;
    aiohttp calls it while DeepEval is imported. Do not query WMI, spawn ``ver``,
    or invent CPU/host identity. The Windows kernel version comes from Python's
    direct OS API and unknown descriptive fields remain empty.
    """
    if sys.platform != "win32":
        return {"adapter": "not_required", "sys_platform": sys.platform}
    import platform
    from collections import namedtuple

    observed = sys.getwindowsversion()
    major, minor, build = getattr(observed, "platform_version", observed[:3])
    release, version = f"kernel-{major}.{minor}", f"{major}.{minor}.{build}"
    record = namedtuple("OfflineWindowsKernelIdentity", "system node release version machine processor")(
        "Windows", "", release, version, "", "")
    platform.uname = lambda: record
    platform.win32_ver = lambda release="", version="", csd="", ptype="": (
        record.release, record.version, "", "")
    return {"adapter": "isolated_windows_kernel_metadata", "source": "sys.getwindowsversion.platform_version",
            "sys_platform": sys.platform, "system": "Windows", "kernel_version": [major, minor, build],
            "release_label": release, "unknown_fields": ["node", "machine", "processor", "service_pack", "product_type"],
            "wmi_queried": False, "dependency_files_changed": False, "affects_metric_definitions": False}


def _write(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def snapshot_framework_sources(root, output):
    """Preserve this offline projection's implementation separately from model execution."""
    names = [
        "evals/bfcl_multiturn/deepeval/" + name for name in (
            "run_comparison.py", "comparison_worker.py", "comparison_data.py", "comparison_metrics.py",
            "adapter.py", "run_offline.py", "worker.py", "requirements.lock.txt", "installation.json", "pytest.ini",
        )
    ] + ["src/aster/evaluation/" + name for name in (
        "bfcl_multiturn_export.py", "bfcl_multiturn_unmetered_export.py", "bfcl_multiturn_live_export.py",
    )]
    files = []
    for name in names:
        source = root / name
        if source.is_symlink():
            raise ValueError("Framework source snapshot cannot follow symlinks")
        payload = source.read_bytes()
        destination = output / "framework-source" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(payload)
        files.append({"file": name, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
    _write(output / "framework-source-manifest.json", {
        "framework": "deepeval", "version": "4.3.2", "files": files,
        "authority": "Exact offline projection source bytes; these files did not create a new model measurement",
        "new_model_api_calls": 0, "new_judge_api_calls": 0,
    })


def evaluate_campaign(campaign, output):
    """Evaluate the main metric and each auxiliary in separate native runs."""
    from evals.bfcl_multiturn.deepeval.adapter import BFCLSavedTaskMetric, record_to_conversation
    from evals.bfcl_multiturn.deepeval.comparison_data import (
        auxiliary_turn_counts,
        capability_diagnostics,
        verify_native_gate_projection,
    )
    from evals.bfcl_multiturn.deepeval.comparison_metrics import (
        AUXILIARY_NAMES,
        BFCLDeterministicAuxiliaryMetric,
        evaluate_metric,
    )

    output.mkdir()
    conversations, excluded, count_by_task = [], [], campaign["reference_call_counts"]
    for record in campaign["records"]:
        if record["scoring"]["passed"] is None:
            excluded.append({"task_id": record["task_id"], "score": None, "outcome": record["outcome"]})
            continue
        conversation = record_to_conversation(record)
        conversation.metadata["bfcl_aux_reference_call_counts"] = count_by_task[record["task_id"]]
        conversation.metadata["auxiliary_metrics_do_not_weight_main_score"] = True
        conversations.append(conversation)
    _write(output / "conversations.json", [case.model_dump(mode="json") for case in conversations])
    main = evaluate_metric(conversations, BFCLSavedTaskMetric(), identifier="aster-" + campaign["label"] + "-main")
    _write(output / "framework-main.json", main.model_dump(mode="json") if main else None)
    native = main.test_results if main else []
    expected = {record["task_id"]: record["scoring"]["passed"] for record in campaign["records"]
                if record["scoring"]["passed"] is not None}
    if len(native) != len(expected) or {case.name: case.success for case in native} != expected:
        raise ValueError("DeepEval changed the saved official main result")
    auxiliaries = {}
    for kind, name in AUXILIARY_NAMES.items():
        eligible, inapplicable, observations = [], [], []
        for case in conversations:
            record = case.metadata["aster_bfcl_record"]
            passed, count = auxiliary_turn_counts(record, count_by_task[record["task_id"]], kind)
            if not count:
                inapplicable.append(record["task_id"])
                continue
            eligible.append(case)
            observations.append({"task_id": record["task_id"], "passed_turns": passed,
                                 "eligible_turns": count, "score": passed / count})
        result = evaluate_metric(eligible, BFCLDeterministicAuxiliaryMetric(kind),
                                 identifier="aster-" + campaign["label"] + "-" + kind)
        _write(output / ("framework-aux-" + kind + ".json"), result.model_dump(mode="json") if result else None)
        if result:
            actual = {case.name: case.metrics_data[0].score for case in result.test_results}
            if actual != {row["task_id"]: row["score"] for row in observations}:
                raise ValueError("DeepEval changed a deterministic auxiliary result")
        numerator = sum(row["passed_turns"] for row in observations)
        denominator = sum(row["eligible_turns"] for row in observations)
        auxiliaries[kind] = {
            "name": name, "native_case_count": len(eligible), "not_applicable_task_ids": inapplicable,
            "passed_turns": numerator, "eligible_turns": denominator,
            "turn_success_rate": numerator / denominator if denominator else None,
            "task_mean_score": sum(row["score"] for row in observations) / len(observations) if observations else None,
            "observations": observations, "affects_main_score": False, "semantic_quality_judge": False,
        }
    _write(output / "auxiliary-summary.json", auxiliaries)
    capability = capability_diagnostics(campaign)
    _write(output / "missing-functions-and-gate.json", capability)
    _write(output / "missing-parameters-summary.json", capability["missing_parameters"])
    cross_check = verify_native_gate_projection(campaign, [case.model_dump(mode="json") for case in conversations])
    _write(output / "gate-projection-cross-check.json", cross_check)
    return {
        "config_label": campaign["label"], "identity": campaign["identity"],
        "saved_result_summary": campaign["summary"], "native_main_case_count": len(conversations),
        "native_main_passed": sum(case.success is True for case in native),
        "native_main_failed": sum(case.success is False for case in native), "excluded": excluded,
        "auxiliary_metrics": auxiliaries, "capability_diagnostics": capability,
        "native_gate_projection_cross_check": cross_check, "judge_enabled": False,
        "new_model_api_calls": 0, "new_judge_api_calls": 0, "new_capability_measurement": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--baseline-label", default="A0")
    parser.add_argument("--candidate-label", default="A3")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--no-junit", action="store_true")
    args = parser.parse_args(argv)
    if any(os.environ.get(key) != "1" for key in (
            "DEEPEVAL_TELEMETRY_OPT_OUT", "DEEPEVAL_DISABLE_DOTENV", "DEEPEVAL_DISABLE_LEGACY_KEYFILE")):
        parser.error("Use the isolated offline comparison launcher")
    output = args.output.resolve()
    if output.exists():
        parser.error("Existing evidence cannot be overwritten")
    root = Path(__file__).resolve().parents[3]
    sys.path[:0] = [str(root / "src"), str(root)]
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("aster_existing_framework_guard", Path(__file__).with_name("worker.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    guard = module.OfflineGuard(output)
    sys.addaudithook(guard.audit)
    output.mkdir(parents=True, exist_ok=False)
    exit_code, status, error_type = 1, "failed", None
    try:
        _write(output / "host-platform.json", configure_host_metadata())
        if importlib.metadata.version("deepeval") != "4.3.2":
            raise ValueError("Pinned DeepEval 4.3.2 is required")
        snapshot_framework_sources(root, output)
        if args.test:
            import pytest

            options = [str(root / "tests/test_bfcl_deepeval_comparison.py"),
                       "-c", str(Path(__file__).with_name("pytest.ini")), "-q", "-p", "no:cacheprovider"]
            if not args.no_junit:
                options.extend(["--junitxml", str(output / "junit.xml")])
            exit_code = int(pytest.main(options))
            status = "passed" if exit_code == 0 else "failed"
        else:
            from evals.bfcl_multiturn.deepeval.comparison_data import (
                compare_campaigns,
                diagnostic_warnings,
                load_campaign,
            )

            baseline = load_campaign(args.baseline.resolve(), label=args.baseline_label)
            candidate = load_campaign(args.candidate.resolve(), label=args.candidate_label) if args.candidate else None
            comparison = compare_campaigns(baseline, candidate) if candidate else None
            results = [evaluate_campaign(baseline, output / "baseline")]
            if candidate:
                results.append(evaluate_campaign(candidate, output / "candidate"))
            _write(output / "comparison.json", comparison)
            summary = {
                "schema_version": 1, "framework": "deepeval", "version": "4.3.2",
                "mode": "sealed_saved_result_offline_comparison", "configurations": results,
                "paired_comparison": comparison, "diagnostic_warnings": diagnostic_warnings(),
                "new_model_api_calls": 0, "new_judge_api_calls": 0, "judge_enabled": False,
                "model_capability_measurement": False, "holdout_tasks_evaluated": 0,
                "note": "Native DeepEval deterministically rechecks saved measurements; no new model capability score is produced.",
            }
            _write(output / "summary.json", summary)
            print(json.dumps({"framework": "deepeval", "version": "4.3.2", "configurations": [
                {"label": result["config_label"], "cases": result["native_main_case_count"],
                 "passed": result["native_main_passed"], "failed": result["native_main_failed"]} for result in results],
                "new_model_api_calls": 0, "new_judge_api_calls": 0}, ensure_ascii=True))
            status, exit_code = "passed", 0
    except Exception as error:  # noqa: BLE001 - no trace, source or credential-bearing prose in diagnostics.
        error_type = type(error).__name__
        print("Offline comparison failed: " + error_type, file=sys.stderr)
    finally:
        if any(guard.events.values()):
            exit_code, status = 1, "failed_offline_guard"
        _write(output / "offline-guard.json", {"status": status, "error_type": error_type,
            "new_model_api_calls": 0, "new_judge_api_calls": 0, "judge_enabled": False,
            "scope": "Python audited networking/process/filesystem operations", **guard.events})
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
