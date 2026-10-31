"""Real Runtime/MockTransport to isolated DeepEval evidence contract.

Run in Aster's offline gate: Windows event-loop IPC remains local while external
networking, child processes and user Runtime access stay denied. The actual
DeepEval worker separately retains its stricter no-network guard.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from test_bfcl_deepeval_comparison import PROJECT, data


async def _bridge(tmp_path, status):
    import httpx
    from test_bfcl_multiturn_concurrent import case_at, phases
    from test_bfcl_readiness_gate_v2_policy import clarification_case, missing_case, ready, setup
    from test_bfcl_v4_live import response

    from aster.engine.readiness_gate_v2 import policy_identity
    from aster.evaluation import bfcl_multiturn_concurrent as backend

    case = (clarification_case() if status == "needs_clarification" else
            missing_case() if status == "missing_capability" else case_at(0))
    root, profile, _control = setup(tmp_path, case)
    native = phases(0)
    if status in {"missing_capability", "needs_clarification"}:
        reply = "Which filename should I use?" if status == "needs_clarification" else "Creation is unavailable."
        scripted = [{"text": json.dumps({"status": status, "reply": reply})},
                    ready(), *native[:2], ready(), *native[2:]]
    else:
        first = ready() if status == "ready" else {"text": "Malformed readiness verdict."}
        scripted = [first, *native[:2], ready(), *native[2:]]
    requests = []

    def handle(request):
        index = len(requests)
        requests.append(request)
        return response(request, index=index, **scripted[index])

    row = await backend.run_one_current_multiturn(case["id"], root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(handle))
    assert row["verification_result"] == "pass" and row["result"] == "unsupported"
    manifest = {"files": []}
    for name in (data.PROMPT_SOURCE, data.CLARIFY_SOURCE):
        payload = (PROJECT / name).read_bytes()
        target = root / "as-run-source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        manifest["files"].append({"file": name, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
    (root / "source-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    identity = policy_identity()
    envelope = {"agent_policy": data.CLARIFY_POLICY, "agent_policy_identity": identity}
    assert data.validate_gate_campaign(root, envelope, envelope, envelope, [row]) == identity
    first = row["runs"][0]
    assert first["capability_gate"]["checks"][0]["status"] == status
    if status in {"missing_capability", "needs_clarification"}:
        assert first["model_request_purposes"] == {"capability_check": 1}
        assert not any(item.get("run_id") == first["run_id"] for item in row["tool_ledger"])
    projected = data._gate_projection(first)
    assert projected["checks"][0]["status"] == status
    assert projected["model_requests"][0]["purpose"] == "capability_check"
    assert all(set(item["recorded_usage"]) == set(data.TOKEN_FIELDS) for item in projected["model_requests"])
    assert "reply" not in projected["checks"][0]


@pytest.mark.parametrize("status", ["ready", "missing_capability", "needs_clarification", "fallback_to_native"])
def test_readiness_projection_matches_actual_runtime_protocol(tmp_path, status):
    asyncio.run(_bridge(tmp_path, status))
