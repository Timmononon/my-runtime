"""Native 100-worker/200-task stress test, with synthetic mocked HTTP only."""
from __future__ import annotations

import ctypes
import importlib.util
import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from textwrap import dedent

import pytest
from bfcl_test_prerequisites import require_native
from test_bfcl_current import source_at
from test_bfcl_multiturn_concurrent import case_at

from aster.evaluation import bfcl_multiturn_concurrent as backend
from aster.evaluation.bfcl_current import capture_current_profile

PROJECT = Path(__file__).resolve().parents[1]
ADAPTER = PROJECT / "evals/bfcl_multiturn/promptfoo"
EVIDENCE = PROJECT / "artifacts/bfcl/dev200-c100-native-offline-20261030"
TASKS, WORKERS = 200, 100
# This is the measured indent=2 backend size of the real development cases.
# Inert fixture metadata exercises the same full-case hashing volume under locks.
MINIMUM_CASE_BYTES = 7_438_491
pytestmark = [pytest.mark.bfcl_native, pytest.mark.windows, pytest.mark.skipif(os.name != "nt", reason="Native Windows workers")]


FIXTURE_PROVIDER = dedent(r'''
    import asyncio
    import atexit
    import ctypes
    import hashlib
    import importlib.util
    import json
    import os
    import sys
    import time
    import traceback
    import types
    from pathlib import Path

    import httpx

    PROJECT = Path(__PROJECT__)
    CAMPAIGN = Path(__file__).resolve().parents[1]
    sys.dont_write_bytecode = True
    guard_path = PROJECT / "artifacts/bfcl/v4-integration-20261029/offline_gate.py"
    guard_module = types.ModuleType("native_dev200_fixture_offline_guard")
    guard_module.__file__ = str(guard_path)
    exec(compile(guard_path.read_bytes(), str(guard_path), "exec"), guard_module.__dict__)
    guard = guard_module.OfflineGuard(protected_root=__DEFAULT_RUNTIME__)
    guard.install()

    def write_new(path, value):
        with path.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)

    def finish_guard():
        write_new(CAMPAIGN / "promptfoo" / ("mock-guard." + str(os.getpid()) + ".json"),
                  {"pid": os.getpid(), **guard.summary()})

    atexit.register(finish_guard)
    spec = importlib.util.spec_from_file_location(
        "native_fixture_production_provider", PROJECT / "evals/bfcl_multiturn/promptfoo/concurrent_provider.py")
    provider = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(provider)
    from aster.evaluation import bfcl_multiturn_concurrent as backend
    from aster.evaluation.bfcl_current import capture_current_profile
    from aster.platform.filesystem import FileLock
    original_run = backend.run_one_current_multiturn
    original_acquire = FileLock.acquire

    def traced_acquire(lock):
        try:
            return original_acquire(lock)
        except TimeoutError:
            # Zero-time probes are expected for active task locks. Record only
            # blocking lock timeouts, with code locations and no error prose.
            if lock.timeout > 0:
                write_new(CAMPAIGN / "lock-timeouts" / (str(os.getpid()) + "-" + str(time.time_ns()) + ".json"),
                          {"pid": os.getpid(), "lock_basename": lock.path.name,
                           "timeout_seconds": lock.timeout,
                           "frames": [{"file": frame.filename, "function": frame.name, "line": frame.lineno}
                                      for frame in traceback.extract_stack(limit=14)]})
            raise

    FileLock.acquire = traced_acquire

    async def fixture_run(case_id, campaign_dir):
        root = Path(campaign_dir).resolve()
        state = json.loads((root / "execution-control.json").read_bytes())
        assert root == CAMPAIGN and state["test_only"] is True
        assert state["concurrent_tasks"]["maximum"] == 100
        profile = capture_current_profile(root.parent / "source", model="deepseek-flash", reasoning_effort="none")
        # Test-only campaigns skip the live-source branch. Exercise equivalent
        # public-source read/hash work explicitly, outside shared batch locks.
        sources = json.loads((root / "source-manifest.json").read_bytes())["files"]
        assert len(sources) >= 277
        for entry in sources:
            candidate = (PROJECT / entry["file"]).resolve()
            assert candidate.is_relative_to(PROJECT)
            assert hashlib.sha256(candidate.read_bytes()).hexdigest() == entry["sha256"]
        index = int(case_id.rsplit("_", 1)[1])
        phases = [
            {"calls": [("touch", {"file_name": "memo.txt"}),
                       ("echo", {"content": "alpha-" + str(index), "file_name": "memo.txt"})]},
            {"text": "Created."},
        ]
        calls = 0

        async def handle(request):
            nonlocal calls
            offset, calls = calls, calls + 1
            assert request.method == "POST" and request.url.path == "/responses"
            body = json.loads(request.content)
            assert body["model"] == "deepseek-flash" and body["reasoning"] == {"effort": "none"}
            assert body["max_output_tokens"] == 8192
            write_new(root / "mock-requests" / (case_id + "-" + str(offset) + ".json"),
                      {"task_id": case_id, "offset": offset, "pid": os.getpid(), "mock_only": True})
            if offset == 0:
                write_new(root / "mock-arrivals" / (case_id + ".json"),
                          {"task_id": case_id, "pid": os.getpid(), "entered_at": time.monotonic(),
                           "source_files_verified": len(sources)})
                deadline = time.monotonic() + 300
                while len(list((root / "mock-arrivals").glob("*.json"))) < 100:
                    if time.monotonic() >= deadline:
                        raise AssertionError("100 native workers never reached the mocked transport barrier")
                    await asyncio.sleep(.05)
            phase = phases[offset]
            output = [{"type": "function_call", "call_id": f"fixture-{index}-{offset}-{i}",
                       "name": name, "arguments": json.dumps(arguments)}
                      for i, (name, arguments) in enumerate(phase.get("calls", []))]
            if "text" in phase:
                output.append({"type": "message", "content": [{"type": "output_text", "text": phase["text"]}]})
            event = {"type": "response.completed", "response": {"status": "completed", "output": output,
                     "usage": {"input_tokens": 30, "output_tokens": 7,
                               "output_tokens_details": {"reasoning_tokens": 0}}}}
            return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                                  text="event: response.completed\ndata: " + json.dumps(event) + "\n\n")

        return await original_run(case_id, root, _profile=profile,
                                  _test_transport_factory=lambda: httpx.MockTransport(handle))

    backend.run_one_current_multiturn = fixture_run

    def call_api(prompt, options, context):
        return provider.call_api(prompt, options, context)
''')

FIXTURE_ASSERTION = dedent(r'''
    import hashlib
    import json
    from pathlib import Path

    def get_assert(output, context):
        compact = json.loads(output)
        task_id = context["vars"]["task_id"]
        root = Path(context["test"]["metadata"]["campaign_dir"])
        from aster.evaluation.bfcl_multiturn_concurrent import read_task_receipt
        row = read_task_receipt(root, task_id)
        digest = hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        assert compact["backend_result_sha256"] == digest
        assert compact["id"] == row["id"] == task_id
        assert compact["main_score"] is None and compact["result"] == row["result"] == "unsupported"
        assert compact["model_api_calls"] == row["model_api_calls"] == 0
        assert row["verification_result"] == row["protocol_result"] == "pass"
        assert row["scoring_verification"]["valid"] is True and row["http_attempts"] == 2
        return {"pass": True, "score": 1,
                "reason": "Offline structural stress verification only; model capability score remains null"}
''')


class _MemoryStatus(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong), ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong), ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong), ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong)]


def memory_status():
    status = _MemoryStatus()
    status.length = ctypes.sizeof(status)
    assert ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return {"time_monotonic": time.monotonic(), "available_physical_bytes": status.available_physical,
            "total_physical_bytes": status.total_physical, "available_commit_bytes": status.available_page_file}


def _module(name):
    spec = importlib.util.spec_from_file_location("native_development_test_" + name, ADAPTER / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _owned_cli(command, *, cwd, env):
    """Await only the owned native process; monitor host memory without child tools."""
    samples, finished = [memory_status()], threading.Event()

    process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    (cwd / "owned-process.json").write_text(json.dumps({"node_pid": process.pid,
        "parent_pid": os.getpid(), "scope": "This test's retained Popen handle only"}), encoding="utf-8")

    def sample():
        low_commit_samples = 0
        while not finished.wait(2):
            current = memory_status()
            samples.append(current)
            low_commit_samples = low_commit_samples + 1 if current["available_commit_bytes"] < 512 * 1024 ** 2 else 0
            fatal = None
            try:
                fatal = json.loads((cwd.parent / "execution-control.json").read_bytes()).get("fatal")
            except (OSError, ValueError):
                pass
            reason = "campaign_fatal" if fatal else "low_available_commit" if low_commit_samples >= 2 else None
            if reason and process.poll() is None:
                (cwd / "owned-stop.json").write_text(json.dumps({"reason": reason,
                    "fatal_code": fatal, "node_pid": process.pid, "memory": current}), encoding="utf-8")
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=20)
                return

    monitor = threading.Thread(target=sample, daemon=True)
    monitor.start()
    try:
        stdout, stderr = process.communicate(timeout=1200)
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=20)
        process.communicate(timeout=15)
        raise
    finally:
        finished.set()
        monitor.join(timeout=3)
        samples.append(memory_status())
        (cwd / "host-memory-samples.json").write_text(json.dumps(samples), encoding="utf-8")
    return process.returncode, stdout, stderr, samples


@pytest.fixture
def native_root():
    boundary = (PROJECT / "artifacts/bfcl").resolve()
    directory = Path(tempfile.mkdtemp(prefix="n100-", dir=boundary)).resolve()
    assert directory.parent == boundary and directory.name.startswith("n100-")
    try:
        yield directory
    finally:
        assert directory.resolve().parent == boundary and directory.name.startswith("n100-")
        shutil.rmtree(directory)


def test_native_hundred_workers_execute_two_hundred_shared_campaign_tasks(native_root, record_property):
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    evidence_name = f"attempt-{time.time_ns()}"
    attempt = EVIDENCE / evidence_name
    attempt.mkdir(exist_ok=False)
    shutil.copyfile(Path(__file__), attempt / "as-run-test.py")
    started = time.monotonic()
    package = json.loads((PROJECT / ".eval-tools/promptfoo/node_modules/promptfoo/package.json").read_bytes())
    assert package["version"] == "0.125.1" and shutil.which("node")
    launcher = _module("run_development")
    source = source_at(native_root)
    source_before = {path.relative_to(source).as_posix(): path.read_bytes()
                     for path in source.rglob("*") if path.is_file()}
    profile = capture_current_profile(source, model="deepseek-flash", reasoning_effort="none")
    campaign = native_root / "c"
    campaign.mkdir()
    cases = [case_at(index) for index in range(TASKS)]
    for case in cases:
        case["question"], case["expected"] = case["question"][:1], case["expected"][:1]
    initial_bytes = len(json.dumps(cases).encode())
    padding = max(0, math.ceil((MINIMUM_CASE_BYTES - initial_bytes) / TASKS))
    for case in cases:
        case.setdefault("provenance", {})["fixture_unused_padding"] = "p" * padding
    plan = launcher.preflight(WORKERS)
    plan.update(campaign=campaign.name, groups=[{"group": case["id"], "entry_ids": [case["id"]],
        "category": "multi_turn_base", "user_turns": 1} for case in cases],
        known_admission_conflicts=[], reference_admission_issues={})
    (campaign / "campaign-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    source_manifest = launcher._helpers.snapshot_sources(campaign)
    assert len(source_manifest["files"]) >= 277
    control = backend.initialize_campaign(cases, campaign, profile=profile,
        maximum_concurrency=WORKERS, _test_only=True)
    private_bytes = (campaign / "private-cases.json").stat().st_size
    assert private_bytes >= MINIMUM_CASE_BYTES
    prepared = {"configured_workers": WORKERS, "planned_tasks": TASKS,
        "real_backend_payload_bytes": MINIMUM_CASE_BYTES, "fixture_private_cases_bytes": private_bytes,
        "public_source_files_verified_per_task": len(source_manifest["files"]),
        "model_api_calls": 0, "judge_api_calls": 0}
    (attempt / "pre-cli.json").write_text(json.dumps(prepared, indent=2), encoding="utf-8")
    print(json.dumps({"native_pressure_fixture_ready": prepared}), flush=True)
    output = campaign / "promptfoo"
    (output / "dispatches").mkdir(parents=True)
    (campaign / "mock-arrivals").mkdir()
    (campaign / "mock-requests").mkdir()
    (campaign / "lock-timeouts").mkdir()
    fixture = FIXTURE_PROVIDER.replace("__PROJECT__", repr(str(PROJECT))).replace(
        "__DEFAULT_RUNTIME__", repr(str(Path.home() / ".aster")))
    (output / "fixture_provider.py").write_text(fixture, encoding="utf-8")
    (output / "fixture_assertion.py").write_text(FIXTURE_ASSERTION, encoding="utf-8")
    config = launcher.build_config(plan, campaign)
    assert config["providers"][0]["config"]["workers"] == WORKERS
    assert config["evaluateOptions"]["maxConcurrency"] == WORKERS
    assert len(config["tests"]) == TASKS
    config["providers"][0]["id"] = "file://" + (output / "fixture_provider.py").as_posix()
    config["defaultTest"] = {"assert": [{"type": "python", "metric": "offline_structure_only",
        "value": "file://fixture_assertion.py"}]}
    (output / "promptfooconfig.json").write_text(json.dumps(config), encoding="utf-8")
    (output / "empty.env").write_text("", encoding="utf-8")
    env = launcher.child_environment(output, control)
    env["PROMPTFOO_PYTHON_WORKERS"] = str(WORKERS)
    command = launcher.cli_command(output, WORKERS)
    assert command[-2:] == ["--max-concurrency", str(WORKERS)]
    code, stdout, stderr, samples = _owned_cli(command, cwd=output, env=env)
    (attempt / "native.stdout.txt").write_text(stdout, encoding="utf-8")
    (attempt / "native.stderr.txt").write_text(stderr, encoding="utf-8")
    (attempt / "host-memory-samples.json").write_text(json.dumps(samples, indent=2), encoding="utf-8")
    shutil.copyfile(campaign / "source-manifest.json", attempt / "source-manifest.json")
    shutil.copytree(campaign / "as-run-source", attempt / "as-run-source")
    for name in ("owned-process.json", "owned-stop.json"):
        if (output / name).is_file():
            shutil.copyfile(output / name, attempt / name)
    for name in ("results.json",):
        if (output / name).is_file():
            shutil.copyfile(output / name, attempt / name)
    for path in output.glob("*-guard.*.json"):
        shutil.copyfile(path, attempt / path.name)
    shutil.copyfile(campaign / "execution-control.json", attempt / "execution-control.json")
    shutil.copyfile(campaign / "results/http-budget.json", attempt / "http-budget.json")
    shutil.copyfile(campaign / "results/results.json", attempt / "backend-results.json")
    for path in (campaign / "lock-timeouts").glob("*.json"):
        target = attempt / "lock-timeouts" / path.name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(path, target)
    summary = {"native_exit_code": code, "elapsed_seconds": time.monotonic() - started,
        "configured_workers": WORKERS, "planned_tasks": TASKS, "private_cases_bytes": private_bytes,
        "public_source_files_verified_per_task": len(source_manifest["files"]),
        "minimum_available_physical_bytes": min(row["available_physical_bytes"] for row in samples),
        "minimum_available_commit_bytes": min(row["available_commit_bytes"] for row in samples),
        "mock_arrivals": len(list((campaign / "mock-arrivals").glob("*.json"))),
        "mock_requests": len(list((campaign / "mock-requests").glob("*.json"))),
        "completed_receipts": len(list((campaign / "results/receipts").glob("*.json"))),
        "model_api_calls": 0, "judge_api_calls": 0, "capability_score": None}
    (attempt / "attempt-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    assert code == 0, f"Native structural pressure evaluation exited {code}; inspect {attempt}"
    native = json.loads((output / "results.json").read_bytes())["results"]
    assert native["stats"]["errors"] == native["stats"]["failures"] == 0
    assert native["stats"]["successes"] == len(native["results"]) == TASKS
    for row in native["results"]:
        compact = json.loads(row["response"]["output"])
        assert compact["main_score"] is None and compact["model_api_calls"] == 0
        assert compact["result"] == "unsupported" and row["success"] is True
    report = backend.finalize_campaign(campaign)
    shutil.copyfile(campaign / "results/results.json", attempt / "backend-results-final.json")
    shutil.copytree(campaign / "mock-arrivals", attempt / "mock-arrivals")
    shutil.copytree(campaign / "mock-requests", attempt / "mock-requests")
    assert report["http_transport_attempts"] == report["wire_policy_verified_calls"] == TASKS * 2
    assert report["model_api_calls"] == 0 and report["fatal"] is None
    assert len(report["results"]) == TASKS and len({row["session_id"] for row in report["results"]}) == TASKS
    assert all(row["verification_result"] == row["protocol_result"] == "pass" for row in report["results"])
    assert all(row["result"] == "unsupported" and row["http_attempts"] == 2 for row in report["results"])
    assert all(len(row["runs"]) == 1 for row in report["results"])
    assert set(report["concurrent_tasks"]["states"].values()) == {"completed"}
    assert len(list(campaign.rglob("execution-control.json"))) == 1
    assert len(list(campaign.rglob("http-budget.json"))) == 1
    assert summary["mock_requests"] == TASKS * 2
    assert summary["completed_receipts"] == TASKS
    arrivals = [json.loads(path.read_bytes()) for path in (campaign / "mock-arrivals").glob("*.json")]
    assert len(arrivals) == TASKS and len({row["pid"] for row in arrivals}) == WORKERS
    starts = [json.loads(path.read_bytes()) for path in (output / "dispatches").glob("*.started.json")]
    finishes = [json.loads(path.read_bytes()) for path in (output / "dispatches").glob("*.finished.json")]
    assert len(starts) == len(finishes) == TASKS
    events = sorted([(row["started_monotonic"], 1) for row in starts]
                    + [(row["finished_monotonic"], -1) for row in finishes])
    active = peak = 0
    for _, change in events:
        active += change
        peak = max(peak, active)
    assert peak == WORKERS
    guards = [json.loads(path.read_bytes()) for path in output.glob("*-guard.*.json")]
    mocks = [json.loads(path.read_bytes()) for path in output.glob("mock-guard.*.json")]
    assert len(mocks) == WORKERS and all(row["offline_guards_passed"] for row in mocks)
    assert all(row.get("blocked_attempts", row.get("external_attempts", 0)) == 0 for row in guards)
    assert any(path.name.startswith("node-guard.") for path in output.glob("*-guard.*.json"))
    assert {"persistent_wrapper.py", "wrapper.py"} <= {row.get("entry_script") for row in guards}
    assert source_before == {path.relative_to(source).as_posix(): path.read_bytes()
                             for path in source.rglob("*") if path.is_file()}
    evidence = {**summary, "passed": True, "native_promptfoo_version": package["version"],
        "same_campaign": True, "provider_pids": sorted({row["pid"] for row in starts}),
        "peak_overlapping_provider_tasks": peak, "mock_transport_barrier_first_wave": WORKERS,
        "independent_sessions": TASKS, "single_control_file": True, "single_http_ledger": True,
        "task_intents": len(list((campaign / "results/intents").glob("*.json"))),
        "task_receipts": TASKS, "all_official_fixture_checks_valid": True,
        "source_runtime_unchanged": True, "mock_worker_guards": mocks,
        "native_guard_count": len(guards), "native_guard_blocked_attempts": 0,
        "scope": "Native CLI and production provider/new backend, synthetic one-turn tasks only; no model score"}
    (attempt / "report.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    record_property("native_offline_evidence", json.dumps(evidence, sort_keys=True))


@pytest.fixture(scope="module", autouse=True)
def _native_prerequisites():
    require_native(promptfoo=True, frozen=False)
