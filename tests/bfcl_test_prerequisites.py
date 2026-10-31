"""Admission for optional local BFCL evidence, never a scoring fallback."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
CORPUS = "artifacts/bfcl/v4-coverage-20261029"
HISTORICAL_CAMPAIGNS = (
    "compact-dev40-c20-20261030",
    "compact-dev40-a3-c20-20261030",
    "compact-dev40-mf-gate-c20-20261030",
)


def require_evidence(*relative_files):
    """Skip absent fixed repository inputs; present invalid inputs still fail."""
    missing = []
    for name in relative_files:
        relative = Path(name)
        if relative.is_absolute() or relative.drive or ".." in relative.parts:
            raise ValueError("BFCL test prerequisites must be fixed project-relative files")
        if not (PROJECT / relative).is_file():
            missing.append(relative.as_posix())
    if missing:
        pytest.skip("Optional local BFCL evidence is absent: " + ", ".join(missing))


def require_corpus():
    require_evidence(CORPUS + "/provenance.json", CORPUS + "/sample-manifest.json")


def require_campaign(name):
    require_evidence("artifacts/bfcl/" + name + "/source-manifest.json")


def require_frozen_runtimes():
    for campaign in HISTORICAL_CAMPAIGNS:
        require_campaign(campaign)
        require_evidence("artifacts/bfcl/" + campaign + "/as-run-source/src/aster/engine/loop.py")
    require_corpus()


def require_native(*, promptfoo=True, frozen=False):
    if os.environ.get("ASTER_TEST_BFCL_NATIVE") != "1":
        pytest.skip("Set ASTER_TEST_BFCL_NATIVE=1 for optional local BFCL native/stress integration")
    require_evidence("artifacts/bfcl/v4-integration-20261029/offline_gate.py")
    if promptfoo:
        if shutil.which("node") is None:
            pytest.skip("Optional BFCL native integration requires Node.js")
        require_evidence(".eval-tools/promptfoo/node_modules/promptfoo/package.json",
                         ".eval-tools/promptfoo/node_modules/promptfoo/dist/src/entrypoint.js")
    if frozen:
        require_frozen_runtimes()


def require_bm25():
    pytest.importorskip("rank_bm25", reason="This exact BFCL KV simulator requires pip install '.[bfcl]'")
