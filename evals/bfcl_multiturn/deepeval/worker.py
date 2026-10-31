"""Guarded child entry point. Use run_offline.py rather than importing this file."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path


class OfflineGuard:
    """Deny outbound networking, child processes and default Aster state access.

    This guard audits Python operations, not arbitrary native DLL networking.
    No native transport/model is instantiated by this deterministic adapter.
    """

    def __init__(self, output: Path):
        self.output = output
        self.scratch = Path.cwd().resolve()
        self.blocked_runtime = Path(os.environ.get("ASTER_FRAMEWORK_BLOCKED_RUNTIME", Path.home() / ".aster")).resolve()
        self.events = {"network_attempts": 0, "child_process_attempts": 0,
                       "default_runtime_attempts": 0, "outside_write_attempts": 0}

    def audit(self, event: str, args: tuple):
        if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo", "socket.gethostbyname",
                     "socket.gethostbyname_ex", "socket.gethostbyaddr", "socket.sendto"}:
            self.events["network_attempts"] += 1
            raise PermissionError("Offline evaluation blocks networking")
        if event in {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn"}:
            self.events["child_process_attempts"] += 1
            raise PermissionError("Offline worker blocks child process creation")
        if event not in {"open", "os.listdir", "os.scandir", "os.mkdir", "os.remove", "os.rmdir", "os.rename",
                         "sqlite3.connect"} or not args:
            return
        raw = args[0]
        if not isinstance(raw, (str, bytes, os.PathLike)):
            return
        path = Path(os.fsdecode(raw)).resolve()
        if path == Path(os.devnull).resolve():
            return  # Windows resolves pytest's log sink to the \\.\NUL device.
        if path.is_relative_to(self.blocked_runtime):
            self.events["default_runtime_attempts"] += 1
            raise PermissionError("Default Aster Runtime is outside the offline evaluation")
        write = event in {"os.mkdir", "os.remove", "os.rmdir", "os.rename"}
        if event == "open":
            mode = args[1] or ""
            flags = args[2] or 0
            write = any(char in mode for char in "wax+") or bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT))
        if write and not any(path.is_relative_to(base) for base in (self.output, self.scratch)):
            self.events["outside_write_attempts"] += 1
            raise PermissionError("Offline worker writes only its scratch and new evidence")


def _write(path: Path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()
    if os.environ.get("DEEPEVAL_TELEMETRY_OPT_OUT") != "1" or os.environ.get("DEEPEVAL_DISABLE_DOTENV") != "1":
        parser.error("Use run_offline.py: offline child configuration is required")
    output = args.output.resolve()
    if output.exists():
        parser.error("Existing evidence cannot be overwritten")
    root = Path(__file__).resolve().parents[3]
    sys.path[:0] = [str(root / "src"), str(root)]
    sys.dont_write_bytecode = True
    guard = OfflineGuard(output)
    sys.addaudithook(guard.audit)
    output.mkdir(parents=True, exist_ok=False)
    status, error_type, exit_code = "failed", None, 1
    try:
        if args.test:
            import pytest

            exit_code = int(pytest.main([str(root / "tests" / "test_bfcl_deepeval.py"),
                                        "-c", str(Path(__file__).with_name("pytest.ini")), "-q", "-p", "no:cacheprovider",
                                        "--junitxml", str(output / "junit.xml")]))
            status = "passed" if exit_code == 0 else "failed"
        else:
            from aster.evaluation.bfcl_multiturn_export import load_bundle, summarize_records
            from evals.bfcl_multiturn.deepeval.adapter import auxiliary_observations, evaluate_records

            records = load_bundle(args.bundle.resolve())
            result, conversations, excluded = evaluate_records(records)
            _write(output / "conversations.json", [case.model_dump(mode="json") for case in conversations])
            rows = [] if result is None else [asdict(row) for row in result.test_results]
            # Pydantic nested values (MetricData, TurnApi) are normalized by the
            # actual framework model serializer, rather than stringified.
            native = None if result is None else result.model_dump(mode="json")
            _write(output / "framework-results.json", native)
            _write(output / "auxiliary.json", [{"task_id": record["task_id"],
                                                "auxiliary": auxiliary_observations(record)} for record in records])
            report = {"schema_version": 1, "framework": "deepeval", "version": importlib.metadata.version("deepeval"),
                      "mode": "saved_result_offline", "judge_enabled": False, "new_model_api_calls": 0,
                      "new_judge_api_calls": 0, "model_capability_measurement": False,
                      "scored_tasks": len(rows), "excluded_tasks": excluded,
                      "recorded_result_summary": summarize_records(records),
                      "metric": "BFCL saved complete-task result",
                      "passed": sum(row["success"] is True for row in rows),
                      "failed": sum(row["success"] is False for row in rows),
                      "native_case_count": len(conversations),
                      "full_leaderboard_score": None, "official_score": False}
            _write(output / "summary.json", report)
            expected = {(record["task_id"], record["config_label"]): record["scoring"]["passed"]
                        for record in records if type(record["scoring"]["passed"]) is bool}
            actual = {(row["metadata"]["aster_bfcl_record"]["task_id"],
                       row["metadata"]["aster_bfcl_record"]["config_label"]): row["success"] for row in rows}
            if len(rows) != len(expected) or len(actual) != len(rows) or actual != expected:
                raise RuntimeError("DeepEval outcome changed a saved official result")
            status, exit_code = "passed", 0
    except Exception as error:  # noqa: BLE001 - persist only safe exception type, never raw provider text
        error_type = type(error).__name__
        print(f"Offline integration failed: {error_type}", file=sys.stderr)
    finally:
        clean = not any(guard.events.values())
        if not clean:
            exit_code, status = 1, "failed_offline_guard"
        _write(output / "offline-guard.json", {"status": status, "error_type": error_type,
                                               "new_model_api_calls": 0, "new_judge_api_calls": 0,
                                               "scope": "Python audited networking/process/filesystem operations",
                                               **guard.events})
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
