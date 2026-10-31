"""Pinned BFCL bytes must survive a real Windows-style Git checkout."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path


def _manifest_hashes(manifest):
    hashes = {}
    for entry in manifest["files"]:
        hashes[entry["file"]] = entry["vendored_sha256"]
        hashes[entry["source"]] = entry["source_sha256"]
    for entry in [*manifest["adapters"], *manifest["resources"]]:
        hashes[entry["file"]] = entry["sha256"]
    return hashes


def test_windows_git_checkout_preserves_bfcl_sources_splits_and_locks(tmp_path):
    root = Path(__file__).resolve().parents[1]
    vendor = Path("src/aster/evaluation/_vendor/bfcl_v4")
    manifest_path = vendor / "SOURCE_MANIFEST.json"
    manifest_bytes = (root / manifest_path).read_bytes()
    expected_hashes = _manifest_hashes(json.loads(manifest_bytes))
    assert len(expected_hashes) == 63
    source_bytes = {vendor / name: (root / vendor / name).read_bytes() for name in expected_hashes}
    for name, expected in expected_hashes.items():
        assert hashlib.sha256(source_bytes[vendor / name]).hexdigest() == expected, name
    source_bytes[manifest_path] = manifest_bytes

    splits = Path("evals/bfcl_multiturn/splits")
    split_files = [path.relative_to(root) for path in sorted((root / splits).rglob("*"))
                   if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"]
    assert all(splits / group / "split-manifest.json" in split_files
               for group in ("frozen", "compact100-v1", "compact100-v2"))
    locks = [Path("evals/bfcl_multiturn/promptfoo/package-lock.json"),
             Path("evals/bfcl_multiturn/deepeval/requirements.lock.txt")]
    resource_bytes = {name: (root / name).read_bytes() for name in [*split_files, *locks]}

    repository = tmp_path / "source"
    repository.mkdir()
    for name, content in {**source_bytes, **resource_bytes}.items():
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    shutil.copy2(root / ".gitattributes", repository / ".gitattributes")
    (repository / "ordinary.txt").write_bytes(b"LF checkout control\n")
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

    def git(*arguments, cwd=repository):
        subprocess.run(["git", "-c", "core.autocrlf=false", *arguments], cwd=cwd, env=env,
                       capture_output=True, text=True, check=True)

    git("init", "-b", "main")
    git("add", "--all")
    git("-c", "user.name=offline-test", "-c", "user.email=offline@example.invalid",
        "commit", "-qm", "Record pinned BFCL bytes")
    checkout = tmp_path / "checkout"
    git("clone", "--no-checkout", str(repository), str(checkout), cwd=tmp_path)
    git("-c", "core.autocrlf=true", "checkout", "-q", "main", cwd=checkout)

    # A genuine conversion is required, so disabling autocrlf cannot mask a regression.
    assert (checkout / "ordinary.txt").read_bytes() == b"LF checkout control\r\n"
    for name, expected in {**source_bytes, **resource_bytes}.items():
        assert (checkout / name).read_bytes() == expected, name.as_posix()
    checked_manifest = json.loads((checkout / manifest_path).read_bytes())
    checked_hashes = _manifest_hashes(checked_manifest)
    assert checked_hashes == expected_hashes
    for name, expected in checked_hashes.items():
        assert hashlib.sha256((checkout / vendor / name).read_bytes()).hexdigest() == expected, name
