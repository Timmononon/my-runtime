"""Offline simulator subprocess, isolated from both paid models and DeepEval."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path


def _write(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("a0", "a3", "mf-gate", "output"):
        parser.add_argument("--" + name, type=Path, required=name == "output")
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args(argv)
    if os.environ.get("DEEPEVAL_TELEMETRY_OPT_OUT") != "1" or os.environ.get("DEEPEVAL_DISABLE_DOTENV") != "1":
        parser.error("Use the clean isolated official-audit launcher")
    output = args.output.resolve()
    if output.exists():
        parser.error("Existing evidence cannot be overwritten")
    root = Path(__file__).resolve().parents[3]
    sys.path[:0] = [str(root / "src"), str(root)]
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("aster_official_audit_guard", Path(__file__).with_name("worker.py"))
    guard_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard_module)
    guard = guard_module.OfflineGuard(output)
    sys.addaudithook(guard.audit)
    output.mkdir(parents=True, exist_ok=False)
    status, exit_code, error_type = "failed", 1, None
    try:
        from evals.bfcl_multiturn.deepeval.comparison_worker import configure_host_metadata

        _write(output / "host-platform.json", configure_host_metadata())
        names = ["evals/bfcl_multiturn/deepeval/" + name for name in (
            "run_holdout_official_audit.py", "holdout_official_audit_worker.py", "holdout_official_audit.py",
            "holdout_comparison_data.py", "comparison_data.py", "worker.py", "run_offline.py",
        )] + ["evals/bfcl_multiturn/promptfoo/compact_holdout_dataset.py",
              "tests/test_bfcl_holdout_official_audit.py"]
        files = []
        for name in names:
            source = root / name
            if source.is_symlink():
                raise ValueError("Audit source capture cannot follow symlinks")
            payload = source.read_bytes()
            target = output / "audit-source" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(payload)
            files.append({"file": name, "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)})
        _write(output / "audit-source-manifest.json", {"files": files, "new_model_api_calls": 0, "new_judge_api_calls": 0})
        if args.test:
            import pytest

            exit_code = int(pytest.main([str(root / "tests/test_bfcl_holdout_official_audit.py"),
                "-c", str(Path(__file__).with_name("pytest.ini")), "-q", "-p", "no:cacheprovider",
                "--basetemp", str(Path.cwd() / "t")]))
            status = "passed" if exit_code == 0 else "failed"
        else:
            from evals.bfcl_multiturn.deepeval.holdout_comparison_data import load_campaign
            from evals.bfcl_multiturn.deepeval.holdout_official_audit import audit_campaign

            if any(path is None for path in (args.a0, args.a3, args.mf_gate)):
                raise ValueError("All three sealed campaigns are required")
            audits = []
            for label, path in (("A0", args.a0), ("A3", args.a3), ("MF-Gate-v1", args.mf_gate)):
                campaign = load_campaign(path, label=label)
                audits.append(audit_campaign(campaign, output / label))
            _write(output / "summary.json", {"status": "passed", "independent_official_audits": audits,
                "new_model_api_calls": 0, "new_judge_api_calls": 0, "scores_replaced": False})
            print(json.dumps({"status": "passed", "audits": audits}))
            status, exit_code = "passed", 0
    except Exception as error:  # noqa: BLE001 - structural failure type only, never source or tool values.
        error_type = type(error).__name__
        print("Offline official audit failed: " + error_type, file=sys.stderr)
    finally:
        if any(guard.events.values()):
            status, exit_code = "failed_offline_guard", 1
        _write(output / "offline-guard.json", {"status": status, "error_type": error_type,
            "new_model_api_calls": 0, "new_judge_api_calls": 0, "judge_enabled": False, **guard.events})
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
