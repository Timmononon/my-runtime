"""Read-only model identity and I/O-stage witnesses over offline transports."""
from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from test_bfcl_current import source_at
from test_bfcl_v4_live import response

from aster.domain import Message, ModelTurn, ToolCall
from aster.evaluation.bfcl_current import capture_current_profile
from aster.evaluation.bfcl_v4_live import V4HTTPCallBudget, _CurrentModelDriver
from aster.providers.deepseek import ProviderError


def driver_at(tmp_path, handle):
    profile = capture_current_profile(source_at(tmp_path))
    budget = V4HTTPCallBudget(tmp_path / "http-ledger.json", maximum=5, binding="diagnostics")
    protocol = SimpleNamespace(path=tmp_path / "protocol-budget.json", reserve=lambda _kind: True)
    driver = _CurrentModelDriver(profile, budget, "task", protocol_budget=protocol, max_output_tokens=8192,
        test_transport_factory=lambda: httpx.MockTransport(handle))
    return driver, budget, protocol


def permission(target, other):
    error = PermissionError(13, "PRIVATE_ERROR_TEXT_MUST_NOT_BE_RECORDED", str(other))
    error.winerror, error.filename2 = 5, str(target)
    return error


@pytest.mark.asyncio
async def test_returned_tool_identity_witness_is_complete_bounded_and_never_changes_turn(tmp_path, monkeypatch):
    driver, budget, _ = driver_at(tmp_path, lambda _request: (_ for _ in ()).throw(AssertionError("no HTTP")))
    messages = [Message("assistant", tool_calls=[ToolCall("reused-message-id", "touch", {})]),
        {"role": "assistant", "tool_calls": [], "provider_items": [
            {"type": "function_call", "call_id": "reused-provider-id"}]}]
    before_messages = copy.deepcopy(messages)
    ids = [f"new-{index}" for index in range(65)]
    ids += ["reused-message-id", "reused-message-id", "reused-provider-id", "", None]
    turn = ModelTurn(text="PRIVATE_RETURNED_TEXT_NOT_FOR_WITNESS",
                     tool_calls=[ToolCall(identifier, "touch", {}) for identifier in ids])
    before_turn = copy.deepcopy(turn)

    async def generated(*_args, **_kwargs):
        return turn

    monkeypatch.setattr(driver.inner, "generate", generated)
    try:
        returned = await driver.generate(messages, [], system="fixture")
        witness = driver.observations[0]["tool_call_id_witness"]
        assert returned is turn and turn == before_turn and messages == before_messages
        assert witness["returned_tool_call_count"] == 70 and witness["missing_or_nonstring_id_count"] == 2
        assert witness["duplicate_id_count"] == witness["duplicate_unique_id_count"] == 1
        assert witness["reused_tool_call_id_count"] == 2 and witness["reused_provider_call_id_count"] == 1
        assert witness["hash_lists_truncated"] is True and len(witness["returned_id_sha256"]) == 64
        assert witness["reused_tool_call_id_sha256"] == [hashlib.sha256(b"reused-message-id").hexdigest()]
        assert all(value not in json.dumps(witness) for value in
                   ("PRIVATE_RETURNED_TEXT_NOT_FOR_WITNESS", "reused-message-id", "reused-provider-id"))
        assert driver.observations[0]["driver_stage"] == "model_turn_received"
        assert budget.snapshot()["calls"] == []
    finally:
        await driver.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,stage,role,http_count", [
    ("before", "request_validation", "http_ledger", 0),
    ("after", "headers_received", "http_ledger", 1),
    ("protocol", "protocol_budget_reservation", "protocol_budget", 0),
])
async def test_permission_witness_preserves_hook_failure_stage_without_paths_or_prose(
        tmp_path, monkeypatch, phase, stage, role, http_count):
    received = []

    def handle(request):
        received.append(request)
        return response(request, text="Done")

    driver, budget, protocol = driver_at(tmp_path, handle)
    target = protocol.path if phase == "protocol" else budget.path
    failure = permission(target, tmp_path / "PRIVATE_OTHER_FILENAME.tmp")

    async def failed_hook(*_args, **_kwargs):
        raise failure

    if phase == "protocol":
        def failed_reserve(_kind):
            raise failure

        protocol.reserve = failed_reserve
    else:
        monkeypatch.setattr(budget, "before_request" if phase == "before" else "after_response", failed_hook)
    try:
        with pytest.raises(ProviderError, match="Current model generation failed"):
            await driver.generate([Message("user", "fixture")], [], system="fixture")
        observed = driver.failures[0]
        assert observed["type"] == "PermissionError" and observed["category"] == "unknown"
        assert observed["diagnostic_stage"] == stage and observed["failure_target_role"] == role
        assert observed["errno"] == 13 and observed["winerror"] == 5
        assert observed["diagnostic_http_status_seen"] == (200 if phase == "after" else None)
        assert len(received) == http_count
        assert all(value not in json.dumps(observed) for value in
                   (str(tmp_path), "PRIVATE_ERROR_TEXT", "PRIVATE_OTHER_FILENAME", "fake-current-key"))
        if phase == "after":
            assert budget.snapshot()["calls"][0]["status"] == "outcome_unknown"
    finally:
        await driver.aclose()


@pytest.mark.asyncio
async def test_new_generation_resets_prior_headers_and_model_turn_stage(tmp_path, monkeypatch):
    received = []

    def handle(request):
        received.append(request)
        return response(request, text="Done")

    driver, budget, _ = driver_at(tmp_path, handle)
    try:
        await driver.generate([Message("user", "first")], [], system="fixture")
        assert driver.observations[0]["driver_stage"] == "model_turn_received"

        async def failed_before(*_args, **_kwargs):
            raise permission(budget.path, tmp_path / "unknown.tmp")

        monkeypatch.setattr(budget, "before_request", failed_before)
        with pytest.raises(ProviderError):
            await driver.generate([Message("user", "second")], [], system="fixture")
        assert driver.failures[0]["diagnostic_stage"] == "request_validation"
        assert driver.failures[0]["diagnostic_http_status_seen"] is None
        assert driver.observations[1]["driver_stage"] == "request_validation"
        assert "tool_call_id_witness" not in driver.observations[1]
        assert len(received) == 1 and len(budget.snapshot()["calls"]) == 1
    finally:
        await driver.aclose()


@pytest.mark.asyncio
async def test_unknown_transport_outcome_is_not_retried_or_reclassified_by_diagnostics(tmp_path):
    received = []

    def broken(request):
        received.append(request)
        raise httpx.ReadError("PRIVATE_NETWORK_PROSE_OR_SECRET", request=request)

    driver, budget, _ = driver_at(tmp_path, broken)
    try:
        with pytest.raises(ProviderError):
            await driver.generate([Message("user", "fixture")], [], system="fixture")
        assert len(received) == 1 and budget.snapshot()["calls"][0]["status"] == "outcome_unknown"
        failure = driver.failures[0]
        assert failure["category"] == "transport" and failure["diagnostic_stage"] == "request_reserved"
        assert failure["failure_target_role"] == "unknown" and failure["errno"] is None
        assert "PRIVATE_NETWORK_PROSE_OR_SECRET" not in json.dumps(driver.failures)
        assert "fake-current-key" not in json.dumps(driver.failures)
    finally:
        await driver.aclose()
