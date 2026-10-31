"""Three genuine isolated frozen packages execute native twenty-worker mocks."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from bfcl_test_prerequisites import require_native

PROJECT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.bfcl_native, pytest.mark.windows, pytest.mark.skipif(os.name != "nt", reason="Native Windows orchestration")]


@pytest.mark.parametrize("label", ["A0", "A3", "MF-Gate-v1"])
def test_frozen_native_twenty_workers(label, record_property):
    boundary = (PROJECT / "artifacts/bfcl").resolve()
    # Match the real campaign prefix length, rather than hiding long paths with
    # an extremely short source root. Temporary evidence remains independently owned.
    with tempfile.TemporaryDirectory(prefix="compact-holdout60-native-", dir=boundary) as temporary:
        root = Path(temporary).resolve()
        assert root.parent == boundary and root.name.startswith("compact-holdout60-native-")
        spec = importlib.util.spec_from_file_location("native_holdout_builder", PROJECT /
            "evals/bfcl_multiturn/promptfoo/frozen_holdout_runtime.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        workspace = root / "runtime-workspace"
        builder.materialize_runtime(label, workspace)
        env = dict(os.environ)
        env.update(PYTHONPATH=str(workspace / "src"), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
        command = [sys.executable, "-u",
            str(PROJECT / "tests/bfcl_holdout_native_worker.py"), "--main-project", str(PROJECT),
            "--workspace", str(workspace), "--root", str(root), "--configuration", label]
        completed = subprocess.run(command, cwd=workspace, env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=270, check=False)
        # Failure output contains local source/test traces, never credentials.
        assert completed.returncode == 0, completed.stdout[-1500:] + completed.stderr[-3000:]
        evidence = json.loads((root / "native-evidence.json").read_bytes())
        assert evidence["status"] == "passed" and evidence["configuration_label"] == label
        assert evidence["model_api_calls"] == evidence["judge_api_calls"] == evidence["new_holdout_inference_tasks"] == 0
        assert evidence["all_callback_import_proofs_match"] is True
        assert evidence["actual_frozen_runtime_imports"]["source_commit"] == builder.VERSIONS[label]["source_commit"]
        receipt = PROJECT / "artifacts/bfcl/holdout60-promptfoo-validation-20261030" / ("native-" + label + ".json")
        receipt.parent.mkdir(parents=True, exist_ok=True)
        with receipt.open("x", encoding="utf-8") as stream:
            json.dump(evidence, stream, sort_keys=True, ensure_ascii=False, indent=2)
            stream.write("\n")
        assert hashlib.sha256(receipt.read_bytes()).hexdigest()
        record_property("native_offline_evidence", json.dumps(evidence, sort_keys=True))


@pytest.fixture(scope="module", autouse=True)
def _native_prerequisites():
    require_native(promptfoo=True, frozen=True)
