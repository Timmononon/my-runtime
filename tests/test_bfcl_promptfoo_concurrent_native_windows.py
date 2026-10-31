"""Native promptfoo workers exercise one shared campaign with mocked HTTP only."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
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
pytestmark = [pytest.mark.bfcl_native, pytest.mark.windows, pytest.mark.skipif(os.name != "nt", reason="Native Windows workers")]


# This fixture is generated only in pytest's temporary directory. Production
# providers have no configuration flag or environment switch for mock dispatch.
FIXTURE_PROVIDER = dedent(r'''
    import asyncio
    import atexit
    import importlib.util
    import json
    import os
    import sys
    import time
    import types
    from pathlib import Path

    import httpx

    PROJECT = Path(__PROJECT__)
    CAMPAIGN = Path(__file__).resolve().parents[1]
    sys.dont_write_bytecode = True
    guard_path = PROJECT / "artifacts/bfcl/v4-integration-20261029/offline_gate.py"
    guard_module = types.ModuleType("native_fixture_offline_guard")
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
    original_run = backend.run_one_current_multiturn

    async def fixture_run(case_id, campaign_dir):
        root = Path(campaign_dir).resolve()
        state = json.loads((root / "execution-control.json").read_bytes())
        assert root == CAMPAIGN and state["test_only"] is True
        assert state["concurrent_tasks"]["maximum"] == 5
        profile = capture_current_profile(root.parent / "source", model="deepseek-flash", reasoning_effort="none")
        index = int(case_id.rsplit("_", 1)[1])
        phases = [
            {"calls": [("touch", {"file_name": "memo.txt"}),
                       ("echo", {"content": "alpha-" + str(index), "file_name": "memo.txt"})]},
            {"text": "Created."}, {"calls": [("cat", {"file_name": "memo.txt"})]},
            {"text": "alpha-" + str(index)},
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
                          {"task_id": case_id, "pid": os.getpid(), "entered_at": time.monotonic()})
                deadline = time.monotonic() + 35
                while len(list((root / "mock-arrivals").glob("*.json"))) != 5:
                    if time.monotonic() >= deadline:
                        raise AssertionError("Five native workers never reached the mocked transport barrier")
                    await asyncio.sleep(.01)
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
        assert row["scoring_verification"]["valid"] is True and row["http_attempts"] == 4
        return {"pass": True, "score": 1,
                "reason": "Offline structural verification only; model capability score remains null"}
''')


def _module(name):
    spec = importlib.util.spec_from_file_location("native_test_" + name, ADAPTER / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _owned_cli(command, *, cwd, env):
    """On timeout terminate only the still-running process tree we created."""
    process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        stdout, stderr = process.communicate(timeout=150)
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=15)
        process.communicate(timeout=10)
        raise
    return process.returncode, stdout, stderr


@pytest.fixture
def native_root():
    # Standard pytest's deeply nested Windows temp path plus Runtime/session/run
    # names can exceed legacy MAX_PATH. Use a short, owned temporary directory.
    boundary = (PROJECT / "artifacts/bfcl").resolve()
    directory = Path(tempfile.mkdtemp(prefix="n5-", dir=boundary)).resolve()
    assert directory.parent == boundary and directory.name.startswith("n5-")
    try:
        yield directory
    finally:
        assert directory.resolve().parent == boundary and directory.name.startswith("n5-")
        shutil.rmtree(directory)


def test_native_five_workers_share_campaign_and_use_only_mock_http(native_root, record_property):
    tmp_path = native_root
    assert shutil.which("node"), "The pinned local promptfoo Node runtime is required"
    package = json.loads((PROJECT / ".eval-tools/promptfoo/node_modules/promptfoo/package.json").read_bytes())
    assert package["version"] == "0.125.1"
    launcher = _module("run_concurrent")
    source = source_at(tmp_path)
    source_before = {path.relative_to(source).as_posix(): path.read_bytes()
                     for path in source.rglob("*") if path.is_file()}
    profile = capture_current_profile(source, model="deepseek-flash", reasoning_effort="none")
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    cases = [case_at(index) for index in range(5)]
    plan = launcher.preflight(5)
    plan.update(campaign=campaign.name, groups=[{"group": case["id"], "entry_ids": [case["id"]],
        "category": "multi_turn_base", "user_turns": len(case["question"])} for case in cases])
    (campaign / "campaign-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    control = backend.initialize_campaign(cases, campaign, profile=profile, maximum_concurrency=5, _test_only=True)
    output = campaign / "promptfoo"
    (output / "dispatches").mkdir(parents=True)
    (campaign / "mock-arrivals").mkdir()
    (campaign / "mock-requests").mkdir()
    fixture = FIXTURE_PROVIDER.replace("__PROJECT__", repr(str(PROJECT))).replace(
        "__DEFAULT_RUNTIME__", repr(str(Path.home() / ".aster")))
    (output / "fixture_provider.py").write_text(fixture, encoding="utf-8")
    (output / "fixture_assertion.py").write_text(FIXTURE_ASSERTION, encoding="utf-8")
    config = launcher.build_config(plan, campaign)
    assert config["providers"][0]["config"]["workers"] == 5
    assert config["evaluateOptions"]["maxConcurrency"] == 5
    config["providers"][0]["id"] = "file://" + (output / "fixture_provider.py").as_posix()
    config["defaultTest"] = {"assert": [{"type": "python", "metric": "offline_structure_only",
        "value": "file://fixture_assertion.py"}]}
    (output / "promptfooconfig.json").write_text(json.dumps(config), encoding="utf-8")
    (output / "empty.env").write_text("", encoding="utf-8")
    env = launcher.child_environment(output, control)
    env["PROMPTFOO_PYTHON_WORKERS"] = "5"
    command = launcher.cli_command(output, 5)
    assert command[-2:] == ["--max-concurrency", "5"]
    code, stdout, stderr = _owned_cli(command, cwd=output, env=env)
    (output / "native.stdout.txt").write_text(stdout, encoding="utf-8")
    (output / "native.stderr.txt").write_text(stderr, encoding="utf-8")
    assert code == 0, f"Native structural evaluation exited with code {code}; inspect {output}"
    native = json.loads((output / "results.json").read_bytes())["results"]
    assert native["stats"]["errors"] == native["stats"]["failures"] == 0
    assert native["stats"]["successes"] == len(native["results"]) == 5
    for row in native["results"]:
        compact = json.loads(row["response"]["output"])
        assert compact["main_score"] is None and compact["model_api_calls"] == 0
        assert compact["result"] == "unsupported" and row["success"] is True
    report = backend.finalize_campaign(campaign)
    assert report["http_transport_attempts"] == report["wire_policy_verified_calls"] == 20
    assert report["model_api_calls"] == 0 and report["fatal"] is None
    assert len(report["results"]) == 5 and len({row["session_id"] for row in report["results"]}) == 5
    assert all(row["verification_result"] == row["protocol_result"] == "pass" for row in report["results"])
    assert all(row["result"] == "unsupported" and row["http_attempts"] == 4 for row in report["results"])
    assert all(len(row["runs"]) == 2 for row in report["results"])
    assert set(report["concurrent_tasks"]["states"].values()) == {"completed"}
    assert len(list(campaign.rglob("execution-control.json"))) == 1
    assert len(list(campaign.rglob("http-budget.json"))) == 1
    assert len(list((campaign / "results/intents").glob("*.json"))) == 5
    assert len(list((campaign / "results/receipts").glob("*.json"))) == 5
    assert len(list((campaign / "mock-requests").glob("*.json"))) == 20
    arrivals = [json.loads(path.read_bytes()) for path in (campaign / "mock-arrivals").glob("*.json")]
    assert len(arrivals) == len({row["pid"] for row in arrivals}) == 5
    starts = [json.loads(path.read_bytes()) for path in (output / "dispatches").glob("*.started.json")]
    finishes = [json.loads(path.read_bytes()) for path in (output / "dispatches").glob("*.finished.json")]
    assert len(starts) == len(finishes) == len({row["pid"] for row in starts}) == 5
    events = sorted([(row["started_monotonic"], 1) for row in starts]
                    + [(row["finished_monotonic"], -1) for row in finishes])
    active = peak = 0
    for _, change in events:
        active += change
        peak = max(peak, active)
    assert peak == 5
    guards = [json.loads(path.read_bytes()) for path in output.glob("*-guard.*.json")]
    mocks = [json.loads(path.read_bytes()) for path in output.glob("mock-guard.*.json")]
    assert len(mocks) == 5 and all(row["offline_guards_passed"] for row in mocks)
    assert all(row.get("blocked_attempts", row.get("external_attempts", 0)) == 0 for row in guards)
    assert any(path.name.startswith("node-guard.") for path in output.glob("*-guard.*.json"))
    assert {"persistent_wrapper.py", "wrapper.py"} <= {row.get("entry_script") for row in guards}
    assert source_before == {path.relative_to(source).as_posix(): path.read_bytes()
                             for path in source.rglob("*") if path.is_file()}
    evidence = {"native_promptfoo_version": package["version"], "native_exit_code": code,
        "same_campaign": True, "python_workers": 5, "provider_pids": sorted({row["pid"] for row in starts}),
        "peak_overlapping_provider_tasks": peak, "mock_transport_barrier_participants": len(arrivals),
        "mock_transport_requests": report["http_transport_attempts"], "independent_sessions": 5,
        "single_control_file": True, "single_http_ledger": True, "task_intents": 5, "task_receipts": 5,
        "all_official_fixture_checks_valid": True, "model_api_calls": 0, "judge_api_calls": 0,
        "capability_score": None, "source_runtime_unchanged": True,
        "mock_worker_guards": mocks, "native_guard_count": len(guards),
        "native_guard_blocked_attempts": 0,
        "scope": "Native CLI plus production provider/new backend; synthetic fixtures only, not model capability"}
    record_property("native_offline_evidence", json.dumps(evidence, sort_keys=True))


@pytest.fixture(scope="module", autouse=True)
def _native_prerequisites():
    require_native(promptfoo=True, frozen=False)
