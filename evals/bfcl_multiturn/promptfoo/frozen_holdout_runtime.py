"""Materialize verified historical Aster sources without changing the checkout.

Only two evaluator infrastructure modules are shared across the three frozen
policies. Engine, coordinator and protocol execution source bytes stay frozen.
No model, Runtime, credential or third-party dependency is imported here.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[3]
VERSIONS = {
    "A0": {
        "source_commit": "453f4bd0293dba764eca609915c633ab382bcc50",
        "campaign": "artifacts/bfcl/compact-dev40-c20-20261030",
        "source_manifest_sha256": "74e8895ca84446789edd1705057da726695444ac9a3c207333ee5788d1a1e137",
        "source_files": 293,
    },
    "A3": {
        "source_commit": "04d98823bec6d0222047ef1c04f27b49975fd74e",
        "campaign": "artifacts/bfcl/compact-dev40-a3-c20-20261030",
        "source_manifest_sha256": "b6dc5707883ce2ac6a6fb582b60d2fffb5b24f0baa981ad566a51d95715fe3b4",
        "source_files": 295,
    },
    "MF-Gate-v1": {
        "source_commit": "2beefb554487c03ce9f6f9bf59672b6c3ecabb39",
        "campaign": "artifacts/bfcl/compact-dev40-mf-gate-c20-20261030",
        "source_manifest_sha256": "50ce2a235bb71519376a5f75b500d4a4b32dc24928e817272b8030d8f7570e66",
        "source_files": 299,
    },
}
SHARED_BACKENDS = frozenset({
    "src/aster/evaluation/bfcl_multiturn_concurrent.py",
    "src/aster/evaluation/bfcl_multiturn_unmetered.py",
})
_IGNORED_SUFFIXES = {".pyc", ".pyo"}


def _sha(payload):
    return hashlib.sha256(payload).hexdigest()


def _relative(value):
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or "\\" in str(value):
        raise ValueError("Materialized source path must remain project-relative")
    return path


def _source(path, root):
    path = Path(path)
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or path.is_symlink():
        raise ValueError("Materialized source cannot escape the project or follow a symlink")
    return path.read_bytes()


def _historical(project, label):
    if label not in VERSIONS:
        raise ValueError("Expected one of the three explicitly frozen configurations")
    version = VERSIONS[label]
    campaign = project / version["campaign"]
    manifest_bytes = _source(campaign / "source-manifest.json", project)
    if _sha(manifest_bytes) != version["source_manifest_sha256"]:
        raise ValueError("Historical frozen source manifest changed")
    manifest = json.loads(manifest_bytes)
    if len(manifest["files"]) != version["source_files"]:
        raise ValueError("Historical source file count changed")
    files = {}
    for record in manifest["files"]:
        relative = _relative(record["file"])
        if relative.as_posix() in files:
            raise ValueError("Historical source manifest contains duplicate paths")
        payload = _source(campaign / "as-run-source" / relative, project)
        if _sha(payload) != record["sha256"] or len(payload) != record["bytes"]:
            raise ValueError("Historical as-run source changed")
        files[relative.as_posix()] = payload
    return manifest_bytes, files


def _data_paths(project):
    """Copy pinned local evidence needed by the unchanged corpus verifier."""
    paths = set()
    for split in ("compact100-v2", "frozen"):
        directory = project / "evals/bfcl_multiturn/splits" / split
        paths.update(path for path in directory.iterdir() if path.is_file())
        manifest = json.loads((directory / "split-manifest.json").read_bytes())
        paths.update(project / _relative(name) for name in manifest["sources"])
    corpus = project / "artifacts/bfcl/v4-coverage-20261029"
    paths.update(path for path in corpus.rglob("*")
                 if path.is_file() and "__pycache__" not in path.parts and path.suffix not in _IGNORED_SUFFIXES)
    return sorted(paths)


def materialize_runtime(label, destination, *, project=PROJECT, harness_files=None):
    """Create one new immutable execution workspace and return its manifest.

    Existing destinations are rejected. Call validate_runtime explicitly when
    using an already prepared, unchanged workspace for a later live invocation.
    """
    project, destination = Path(project).resolve(), Path(destination).resolve()
    if destination.exists() or destination == project or project.is_relative_to(destination):
        raise ValueError("Materialized runtime destination must be new and separate")
    origin_bytes, historical = _historical(project, label)
    shared_bytes, shared = _historical(project, "MF-Gate-v1")
    # Validate all inputs before creating output; no partial source is executable.
    catalog = {name: {"payload": payload, "role": "frozen_runtime" if name.startswith("src/aster/")
                     else "frozen_support", "origin": VERSIONS[label]["source_commit"]}
               for name, payload in historical.items()}
    for name in SHARED_BACKENDS:
        catalog[name] = {"payload": shared[name], "role": "shared_evaluation_infrastructure",
                         "origin": VERSIONS["MF-Gate-v1"]["source_commit"]}
    harness_root = project / "evals/bfcl_multiturn/promptfoo"
    if harness_files is None:
        harness_files = sorted(path for path in harness_root.rglob("*")
                               if path.is_file() and "__pycache__" not in path.parts
                               and path.suffix not in _IGNORED_SUFFIXES)
    for value in harness_files:
        path = Path(value)
        if not path.is_absolute():
            path = project / _relative(value)
        if not path.resolve().is_relative_to(harness_root.resolve()):
            raise ValueError("Only promptfoo adapter files may overlay the frozen harness")
        relative = path.relative_to(project).as_posix()
        catalog[relative] = {"payload": _source(path, project), "role": "holdout_evaluation_harness",
                             "origin": "current_local_harness"}
    for path in _data_paths(project):
        relative = path.relative_to(project).as_posix()
        payload = _source(path, project)
        if relative in catalog and catalog[relative]["payload"] != payload:
            raise ValueError("Pinned split/corpus support differs from historical bytes")
        catalog.setdefault(relative, {"payload": payload, "role": "pinned_dataset_support",
                                      "origin": "local_pinned_corpus_and_frozen_split"})
    catalog["frozen-origin-source-manifest.json"] = {
        "payload": origin_bytes, "role": "historical_manifest", "origin": VERSIONS[label]["source_commit"]}
    catalog["shared-infrastructure-source-manifest.json"] = {
        "payload": shared_bytes, "role": "shared_infrastructure_manifest",
        "origin": VERSIONS["MF-Gate-v1"]["source_commit"]}
    destination.mkdir(parents=True)
    records = []
    for name, item in sorted(catalog.items()):
        target = destination / _relative(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(item["payload"])
        records.append({"file": name, "sha256": _sha(item["payload"]), "bytes": len(item["payload"]),
                        "role": item["role"], "origin": item["origin"]})
    manifest = {
        "schema_version": 1, "configuration_label": label, "source_commit": VERSIONS[label]["source_commit"],
        "historical_campaign": VERSIONS[label]["campaign"],
        "historical_source_manifest_sha256": VERSIONS[label]["source_manifest_sha256"],
        "historical_source_file_count": VERSIONS[label]["source_files"], "project_root": str(destination),
        "shared_infrastructure_source_commit": VERSIONS["MF-Gate-v1"]["source_commit"],
        "shared_infrastructure_source_manifest_sha256": VERSIONS["MF-Gate-v1"]["source_manifest_sha256"],
        "frozen_files": [item for item in records if item["role"].startswith("frozen_")],
        "overlays": [item for item in records if item["role"] in {
            "shared_evaluation_infrastructure", "holdout_evaluation_harness"}],
        "support_files": [item for item in records if item["role"] not in {
            "frozen_runtime", "frozen_support", "shared_evaluation_infrastructure", "holdout_evaluation_harness"}],
        "new_model_api_calls": 0, "new_judge_api_calls": 0,
        "source_policy": "Frozen Agent/engine/coordinator/protocol; shared two-file evaluator infrastructure only",
    }
    with (destination / "frozen-runtime-manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    validate_runtime(destination)
    return manifest


def validate_runtime(root):
    """Reject changed bytes, added runtime source, or an expanded overlay."""
    root = Path(root).resolve()
    manifest = json.loads((root / "frozen-runtime-manifest.json").read_bytes())
    label = manifest.get("configuration_label")
    if (manifest.get("schema_version") != 1 or label not in VERSIONS
            or manifest.get("project_root") != str(root)
            or manifest.get("source_commit") != VERSIONS[label]["source_commit"]
            or manifest.get("historical_source_manifest_sha256") != VERSIONS[label]["source_manifest_sha256"]
            or manifest.get("historical_source_file_count") != VERSIONS[label]["source_files"]
            or manifest.get("shared_infrastructure_source_commit") != VERSIONS["MF-Gate-v1"]["source_commit"]
            or manifest.get("shared_infrastructure_source_manifest_sha256")
                != VERSIONS["MF-Gate-v1"]["source_manifest_sha256"]):
        raise ValueError("Materialized runtime identity differs")
    if (_sha((root / "frozen-origin-source-manifest.json").read_bytes())
            != VERSIONS[label]["source_manifest_sha256"]
            or _sha((root / "shared-infrastructure-source-manifest.json").read_bytes())
            != VERSIONS["MF-Gate-v1"]["source_manifest_sha256"]):
        raise ValueError("Materialized historical authority changed")
    historical = {item["file"]: item for item in json.loads(
        (root / "frozen-origin-source-manifest.json").read_bytes())["files"]}
    shared = {item["file"]: item for item in json.loads(
        (root / "shared-infrastructure-source-manifest.json").read_bytes())["files"]}
    records = manifest["frozen_files"] + manifest["overlays"] + manifest["support_files"]
    by_name = {item["file"]: item for item in records}
    if len(by_name) != len(records):
        raise ValueError("Materialized runtime manifest has duplicate paths")
    expected_src = {name for name in historical if name.startswith("src/aster/")} | SHARED_BACKENDS
    actual_src = {path.relative_to(root).as_posix() for path in (root / "src/aster").rglob("*")
                  if path.is_file() and "__pycache__" not in path.parts and path.suffix not in _IGNORED_SUFFIXES}
    if actual_src != expected_src or {name for name in by_name if name.startswith("src/aster/")} != expected_src:
        raise ValueError("Materialized runtime source membership changed")
    for name, item in by_name.items():
        payload = _source(root / _relative(name), root)
        if _sha(payload) != item["sha256"] or len(payload) != item["bytes"]:
            raise ValueError("Materialized source bytes changed")
        if name.startswith("src/aster/"):
            authority = shared if name in SHARED_BACKENDS else historical
            if name not in authority or item["sha256"] != authority[name]["sha256"]:
                raise ValueError("Frozen Agent source was replaced")
            expected_role = "shared_evaluation_infrastructure" if name in SHARED_BACKENDS else "frozen_runtime"
            if item["role"] != expected_role:
                raise ValueError("Agent source overlay role changed")
        elif item["role"] == "holdout_evaluation_harness" and not name.startswith("evals/bfcl_multiturn/promptfoo/"):
            raise ValueError("Holdout harness overlay escapes its scope")
    if {item["file"] for item in manifest["overlays"] if item["file"].startswith("src/")} != SHARED_BACKENDS:
        raise ValueError("Only the two frozen shared evaluator backend overlays are admitted")
    return manifest


def capture_import_origins(root):
    """Verify every currently imported Aster module belongs to this workspace."""
    root = Path(root).resolve()
    manifest = validate_runtime(root)
    records = {item["file"]: item for item in manifest["frozen_files"] + manifest["overlays"]}
    imports = []
    for name, module in sorted(sys.modules.items()):
        if name != "aster" and not name.startswith("aster."):
            continue
        source = getattr(module, "__file__", None)
        if not isinstance(source, str):
            raise TypeError("Imported Aster module has no captured source origin")
        path = Path(source).resolve()
        if not path.is_relative_to(root / "src/aster"):
            raise ValueError("Imported Aster module escaped its frozen runtime")
        relative = path.relative_to(root).as_posix()
        item = records.get(relative)
        if item is None or _sha(path.read_bytes()) != item["sha256"]:
            raise ValueError("Imported Aster source differs from frozen runtime authority")
        imports.append({"module": name, "origin": str(path), "file": relative,
                        "sha256": item["sha256"], "role": item["role"]})
    return {"schema_version": 1, "configuration_label": manifest["configuration_label"],
            "project_root": str(root), "source_commit": manifest["source_commit"], "modules": imports,
            "new_model_api_calls": 0, "new_judge_api_calls": 0}
