"""Offline actual Runtime/MockTransport to saved DeepEval-contract bridge.

Run under Aster's offline gate, which allows Windows event-loop local IPC.
The stricter DeepEval evaluation worker continues to deny all networking.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from test_bfcl_deepeval_comparison import PROJECT, data


async def _gate_projection_actual_protocol(tmp_path, status):
    import httpx
    from test_bfcl_capability_gate_policy import missing_case, ready, setup
    from test_bfcl_multiturn_concurrent import case_at, phases
    from test_bfcl_v4_live import response

    from aster.engine.capability_gate import policy_identity
    from aster.evaluation import bfcl_multiturn_concurrent as backend

    case = missing_case() if status == "missing_capability" else case_at(0)
    root, profile, _control = setup(tmp_path, case)
    native = phases(0)
    if status == "missing_capability":
        scripted = [{"text": json.dumps({"status": status, "reply": "File creation is not available."})},
                    ready(), *native[:2], ready(), *native[2:]]
    else:
        first = ready() if status == "ready" else {"text": "Malformed capability verdict."}
        scripted = [first, *native[:2], ready(), *native[2:]]
    calls = []

    def handle(request):
        index = len(calls)
        calls.append(request)
        return response(request, index=index, **scripted[index])

    row = await backend.run_one_current_multiturn(case["id"], root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(handle))
    assert row["verification_result"] == "pass" and row["result"] == "unsupported"
    manifest = {"files": []}
    for name in (data.PROMPT_SOURCE, data.GATE_SOURCE):
        payload = (PROJECT / name).read_bytes()
        destination = root / "as-run-source" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        manifest["files"].append({"file": name, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
    (root / "source-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    envelope = {"agent_policy": data.GATE_POLICY, "agent_policy_identity": policy_identity()}
    assert data.validate_gate_campaign(root, envelope, envelope, envelope, [row]) == policy_identity()
    assert row["runs"][0]["capability_gate"]["checks"][0]["status"] == status
    projected = data._gate_projection(row["runs"][0])
    assert projected["model_requests"][0]["purpose"] == "capability_check"
    assert all(set(item["recorded_usage"]) == set(data.TOKEN_FIELDS) for item in projected["model_requests"])
    assert all(type(item["recorded_usage"]["input_tokens"]) is int for item in projected["model_requests"])


@pytest.mark.parametrize("status", ["ready", "missing_capability", "fallback_to_native"])
def test_gate_projection_accepts_actual_runtime_and_mocked_native_protocol(tmp_path, status):
    asyncio.run(_gate_projection_actual_protocol(tmp_path, status))


