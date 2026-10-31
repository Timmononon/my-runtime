"""Explicit A6 no-thinking policy over the existing task-locked BFCL backend.

This adapter does not select a provider, read credentials, or make requests on
import. The caller captures the configured A6 profile and admits a fresh plan.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from aster.evaluation import bfcl_multiturn_concurrent as backend
from aster.evaluation import bfcl_multiturn_unmetered as serial

WIRE_POLICY = serial.A6_WIRE_POLICY
GATEWAY_FAILURE_POLICY = serial.A6_GATEWAY_FAILURE_POLICY
PROVIDER_ID = serial.A6_PROVIDER_ID
BASE_URL = "https://api.a6api.com/v1"
PROTOCOL = "openai_responses"
MODEL = "deepseek-v4-flash"
MODE, PERFORMANCE_POLICY = backend.MODE, backend.PERFORMANCE_POLICY


def _require_a6(campaign_dir):
    root = Path(campaign_dir).resolve()
    # The delegated backend performs complete immutable validation. This short
    # locked discriminator avoids another full 200-case parse in each worker.
    with backend._batch_lock(root):
        control = serial._read(root / "execution-control.json")
    if control.get("wire_policy") != WIRE_POLICY:
        raise ValueError("An A6-admitted campaign is required")
    return root


def initialize_campaign(cases, campaign_dir, *, profile, maximum_concurrency=100, **kwargs):
    """Bind the configured A6 endpoint/model/effort without changing defaults."""
    if "wire_policy" in kwargs:
        raise ValueError("The A6 adapter owns its fixed wire policy")
    root = Path(campaign_dir).resolve()
    if not kwargs.get("_test_only", False):
        workspace = Path(__file__).resolve().parents[3]
        manifest = serial._read(root / "source-manifest.json")
        path = Path(__file__).resolve()
        if not any(entry.get("file") == path.relative_to(workspace).as_posix()
                   and entry.get("sha256") == hashlib.sha256(path.read_bytes()).hexdigest()
                   for entry in manifest.get("files", [])):
            raise ValueError("The A6 adapter must be pinned in the source snapshot")
    return backend.initialize_campaign(cases, root, profile=profile, maximum_concurrency=maximum_concurrency,
                                       wire_policy=WIRE_POLICY, **kwargs)


async def run_one_current_multiturn(case_id, campaign_dir, *, _profile=None, _test_transport_factory=None):
    root = _require_a6(campaign_dir)
    return await backend.run_one_current_multiturn(case_id, root, _profile=_profile,
                                                  _test_transport_factory=_test_transport_factory)


def read_task_receipt(campaign_dir, case_id):
    return backend.read_task_receipt(_require_a6(campaign_dir), case_id)


def finalize_campaign(campaign_dir):
    return backend.finalize_campaign(_require_a6(campaign_dir))
