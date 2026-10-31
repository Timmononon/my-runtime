"""Verify actual import origins in one immutable frozen-policy worker process."""
from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

COMMITS = {
    "A0": "453f4bd0293dba764eca609915c633ab382bcc50",
    "A3": "04d98823bec6d0222047ef1c04f27b49975fd74e",
    "MF-Gate-v1": "2beefb554487c03ce9f6f9bf59672b6c3ecabb39",
}
MODULES = (
    "aster.engine.loop", "aster.service.coordinator", "aster.evaluation.bfcl_v4_runner",
    "aster.evaluation.bfcl_multiturn_concurrent", "aster.evaluation.bfcl_multiturn_unmetered",
)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def capture_runtime_identity(configuration, project_root, *, campaign=None):
    if configuration not in COMMITS:
        raise ValueError("Unsupported frozen configuration")
    root = Path(project_root).resolve()
    source_root = root / "src"
    if not (root / "frozen-runtime-manifest.json").is_file():
        raise ValueError("A materialized frozen runtime is required")
    manifest = json.loads((root / "frozen-runtime-manifest.json").read_bytes())
    if (manifest.get("configuration_label") != configuration
            or manifest.get("source_commit") != COMMITS[configuration]):
        raise ValueError("The selected label differs from the materialized frozen runtime")
    modules = list(MODULES)
    if configuration == "A3":
        modules.append("aster.engine.task_control")
    elif configuration == "MF-Gate-v1":
        modules.extend(("aster.engine.capability_gate", "aster.engine.readiness_prompt"))
    expected = None
    if campaign is not None:
        expected = {item["file"]: item for item in json.loads(
            (Path(campaign) / "source-manifest.json").read_bytes())["files"]}
    records = []
    for name in modules:
        module = importlib.import_module(name)
        origin = Path(module.__file__).resolve()
        if not origin.is_relative_to(source_root) or origin.is_symlink():
            raise ValueError("An Agent module escaped the frozen source workspace")
        relative = origin.relative_to(root).as_posix()
        observed_hash = digest(origin)
        if expected is not None and (
                relative not in expected or expected[relative]["sha256"] != observed_hash):
            raise ValueError("An imported Agent module differs from its as-run identity")
        records.append({"module": name, "origin": str(origin), "relative_file": relative,
                        "sha256": observed_hash})
    return {"schema_version": 1, "configuration_label": configuration,
            "source_commit": COMMITS[configuration], "project_root": str(root), "modules": records}


def verify_callback_identity(campaign, configuration):
    root = Path(campaign).resolve()
    path = root / "runtime-import-identity.json"
    expected = json.loads(path.read_bytes())
    actual = capture_runtime_identity(configuration, expected["project_root"], campaign=root)
    if actual != expected:
        raise ValueError("This provider process imported a different frozen Agent runtime")
    return actual, digest(path)
