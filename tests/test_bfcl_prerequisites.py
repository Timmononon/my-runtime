from pathlib import Path

import bfcl_test_prerequisites as prerequisites
import pytest


def test_absent_local_evidence_has_an_explicit_skip_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(prerequisites, "PROJECT", tmp_path)
    with pytest.raises(pytest.skip.Exception, match="Optional local BFCL evidence is absent"):
        prerequisites.require_evidence("artifacts/bfcl/local/source-manifest.json")


def test_present_invalid_evidence_is_left_for_the_real_validator(tmp_path, monkeypatch):
    monkeypatch.setattr(prerequisites, "PROJECT", tmp_path)
    path = tmp_path / "artifacts/bfcl/local/source-manifest.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"invalid json is not an environment skip")
    prerequisites.require_evidence(path.relative_to(tmp_path))
    assert path.read_bytes() == b"invalid json is not an environment skip"


def test_native_integration_needs_explicit_admission_before_resource_checks(monkeypatch):
    monkeypatch.delenv("ASTER_TEST_BFCL_NATIVE", raising=False)
    monkeypatch.setattr(prerequisites, "require_evidence", lambda *_: pytest.fail("No local inputs before opt-in"))
    with pytest.raises(pytest.skip.Exception, match="ASTER_TEST_BFCL_NATIVE=1"):
        prerequisites.require_native()


def test_prerequisite_paths_cannot_escape_the_fixed_project(tmp_path, monkeypatch):
    monkeypatch.setattr(prerequisites, "PROJECT", tmp_path)
    for path in (Path("..") / "outside", tmp_path / "absolute"):
        with pytest.raises(ValueError, match="project-relative"):
            prerequisites.require_evidence(path)
