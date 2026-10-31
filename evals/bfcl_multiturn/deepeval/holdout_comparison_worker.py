"""Strictly offline held-out result comparison; no provider or judge admission."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import sys
from pathlib import Path


def _write(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def snapshot_holdout_sources(root, output):
    names = ["evals/bfcl_multiturn/deepeval/" + name for name in (
        "run_holdout_comparison.py", "holdout_comparison_worker.py", "holdout_comparison_data.py",
    )] + ["tests/test_bfcl_holdout_deepeval.py"]
    rows = []
    for name in names:
        path = root / name
        if path.is_symlink():
            raise ValueError("Framework source snapshots cannot follow symlinks")
        payload = path.read_bytes()
        destination = output / "holdout-framework-source" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(payload)
        rows.append({"file": name, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
    _write(output / "holdout-framework-source-manifest.json", {
        "files": rows, "authority": "Exact offline holdout adapter source; no new model measurement",
        "new_model_api_calls": 0, "new_judge_api_calls": 0,
    })


def evaluate_campaign(campaign, output):
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
    cases, excluded = [], []
    for record in campaign["records"]:
        if record["scoring"]["passed"] is None:
            excluded.append({"task_id": record["task_id"], "score": None, "outcome": record["outcome"]})
            continue
        case = record_to_conversation(record)
        case.metadata.update(bfcl_aux_reference_call_counts=campaign["reference_call_counts"][record["task_id"]],
                             auxiliary_metrics_do_not_weight_main_score=True, evaluation_split="holdout")
        cases.append(case)
    _write(output / "canonical-records.json", campaign["records"])
    _write(output / "native-conversations.json", [case.model_dump(mode="json") for case in cases])
    main = evaluate_metric(cases, BFCLSavedTaskMetric(), identifier="aster-holdout60-" + campaign["label"] + "-main")
    _write(output / "framework-main.json", main.model_dump(mode="json") if main else None)
    native = main.test_results if main else []
    expected = {record["task_id"]: record["scoring"]["passed"] for record in campaign["records"]
                if record["scoring"]["passed"] is not None}
    if len(native) != len(expected) or {case.name: case.success for case in native} != expected:
        raise ValueError("Native DeepEval changed a saved official task result")
    auxiliaries = {}
    for kind, name in AUXILIARY_NAMES.items():
        eligible, observations = [], []
        for case in cases:
            record = case.metadata["aster_bfcl_record"]
            good, count = auxiliary_turn_counts(record, campaign["reference_call_counts"][record["task_id"]], kind)
            if count:
                eligible.append(case)
                observations.append({"task_id": record["task_id"], "passed_turns": good,
                                     "eligible_turns": count, "score": good / count})
        result = evaluate_metric(eligible, BFCLDeterministicAuxiliaryMetric(kind),
                                 identifier="aster-holdout60-" + campaign["label"] + "-" + kind)
        _write(output / ("framework-aux-" + kind + ".json"), result.model_dump(mode="json") if result else None)
        if result and {case.name: case.metrics_data[0].score for case in result.test_results} != {
                item["task_id"]: item["score"] for item in observations}:
            raise ValueError("Native DeepEval changed a deterministic auxiliary metric")
        numerator = sum(row["passed_turns"] for row in observations)
        denominator = sum(row["eligible_turns"] for row in observations)
        auxiliaries[kind] = {"name": name, "native_case_count": len(eligible),
            "passed_turns": numerator, "eligible_turns": denominator,
            "turn_fraction": numerator / denominator if denominator else None,
            "observations": observations, "affects_main_score": False, "semantic_judge_enabled": False}
    _write(output / "auxiliary-summary.json", auxiliaries)
    diagnostics = capability_diagnostics(campaign)
    diagnostics.pop("holdout_tasks_evaluated", None)
    diagnostics.update(evaluation_split="holdout", saved_holdout_tasks=60, new_holdout_inference_tasks=0)
    _write(output / "missing-functions-and-parameters.json", diagnostics)
    cross_check = verify_native_gate_projection(campaign, [case.model_dump(mode="json") for case in cases])
    _write(output / "native-gate-projection-cross-check.json", cross_check)
    result = {"configuration_label": campaign["label"], "identity": campaign["identity"],
        "saved_result_summary": campaign["summary"], "native_main_case_count": len(cases),
        "native_main_pass_count": sum(case.success is True for case in native),
        "native_main_fail_count": sum(case.success is False for case in native), "excluded": excluded,
        "auxiliary_metrics": auxiliaries, "capability_diagnostics": diagnostics,
        "gate_projection_cross_check": cross_check, "actual_evaluate_calls": 1 + len(AUXILIARY_NAMES),
        "new_model_api_calls": 0, "new_judge_api_calls": 0, "judge_enabled": False,
        "model_capability_measurement": False, "saved_holdout_tasks": 60, "new_holdout_inference_tasks": 0}
    _write(output / "summary.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("a0", "a3", "mf-gate", "output"):
        parser.add_argument("--" + name, type=Path, required=name == "output")
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args(argv)
    if any(os.environ.get(key) != "1" for key in (
            "DEEPEVAL_TELEMETRY_OPT_OUT", "DEEPEVAL_DISABLE_DOTENV", "DEEPEVAL_DISABLE_LEGACY_KEYFILE")):
        parser.error("Use the credential-free isolated holdout launcher")
    output = args.output.resolve()
    if output.exists():
        parser.error("Existing evidence cannot be overwritten")
    root = Path(__file__).resolve().parents[3]
    sys.path[:0] = [str(root / "src"), str(root)]
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("aster_holdout_offline_guard", Path(__file__).with_name("worker.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    guard = module.OfflineGuard(output)
    sys.addaudithook(guard.audit)
    output.mkdir(parents=True, exist_ok=False)
    status, error_type, exit_code = "failed", None, 1
    try:
        from evals.bfcl_multiturn.deepeval.comparison_worker import (
            configure_host_metadata,
            snapshot_framework_sources,
        )

        _write(output / "host-platform.json", configure_host_metadata())
        if importlib.metadata.version("deepeval") != "4.3.2":
            raise ValueError("Pinned DeepEval 4.3.2 is required")
        snapshot_framework_sources(root, output)
        snapshot_holdout_sources(root, output)
        if args.test:
            import pytest

            exit_code = int(pytest.main([str(root / "tests/test_bfcl_holdout_deepeval.py"),
                "-c", str(Path(__file__).with_name("pytest.ini")), "-q", "-p", "no:cacheprovider",
                "--basetemp", str(Path.cwd() / "t")]))
            status = "passed" if exit_code == 0 else "failed"
        else:
            from evals.bfcl_multiturn.deepeval.holdout_comparison_data import compare_campaigns, load_campaign

            if any(value is None for value in (args.a0, args.a3, args.mf_gate)):
                raise ValueError("All three saved holdout campaigns are required")
            campaigns = [load_campaign(path, label=label) for label, path in (
                ("A0", args.a0), ("A3", args.a3), ("MF-Gate-v1", args.mf_gate))]
            comparisons = {}
            for left, right in ((0, 1), (0, 2), (1, 2)):
                key = campaigns[left]["label"] + "-vs-" + campaigns[right]["label"]
                comparisons[key] = compare_campaigns(campaigns[left], campaigns[right])
                _write(output / (key.lower() + ".json"), comparisons[key])
            configurations = [evaluate_campaign(campaign, output / campaign["label"]) for campaign in campaigns]
            summary = {"schema_version": 1, "framework": "deepeval", "version": "4.3.2",
                "mode": "sealed_holdout60_saved_result_comparison", "configurations": configurations,
                "paired_comparisons": comparisons, "actual_native_evaluate_calls": 12,
                "native_main_cases": sum(item["native_main_case_count"] for item in configurations),
                "saved_holdout_tasks_per_configuration": 60, "new_holdout_inference_tasks": 0,
                "new_model_api_calls": 0, "new_judge_api_calls": 0, "judge_enabled": False,
                "new_model_capability_measurement": False,
                "interpretation": "Deterministic replay of three captured held-out measurements; no tuning or new model calls"}
            _write(output / "summary.json", summary)
            print(json.dumps({"status": "passed", "configurations": [{
                "label": row["configuration_label"], "cases": row["native_main_case_count"],
                "passed": row["native_main_pass_count"], "excluded": len(row["excluded"])} for row in configurations],
                "new_model_api_calls": 0, "new_judge_api_calls": 0}))
            status, exit_code = "passed", 0
    except Exception as error:  # noqa: BLE001 - never export raw provider or credential-bearing prose.
        error_type = type(error).__name__
        print("Offline holdout comparison failed: " + error_type, file=sys.stderr)
    finally:
        if any(guard.events.values()):
            status, exit_code = "failed_offline_guard", 1
        _write(output / "offline-guard.json", {"status": status, "error_type": error_type,
            "new_model_api_calls": 0, "new_judge_api_calls": 0, "judge_enabled": False, **guard.events})
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
