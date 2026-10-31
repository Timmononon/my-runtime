"""Offline A6 policy, real admission, and shared concurrent Runtime contracts."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from test_bfcl_current import source_at
from test_bfcl_multiturn_concurrent import case_at, phases
from test_bfcl_v4_live import response

from aster.evaluation import bfcl_multiturn_a6 as a6
from aster.evaluation import bfcl_multiturn_concurrent as concurrent
from aster.evaluation import bfcl_multiturn_unmetered as serial
from aster.evaluation.bfcl_current import capture_current_profile
from aster.providers.deepseek import ProviderError

PROJECT = Path(__file__).resolve().parents[1]


def a6_source(tmp_path):
    source = source_at(tmp_path)
    database = sqlite3.connect(source / "runtime.db")
    metadata = {"name": "a6", "base_url": a6.BASE_URL, "protocol": a6.PROTOCOL,
        "identity_revision": 2, "models": [{"id": a6.MODEL, "context_window": 262144,
        "max_output_tokens": None, "input_modalities": ["text"], "effort_levels": ["none", "high"]}]}
    database.execute("UPDATE provider_profiles SET id=?,document=?", (a6.PROVIDER_ID, json.dumps(metadata)))
    database.commit()
    database.close()
    return source


def captured(tmp_path):
    return capture_current_profile(a6_source(tmp_path), provider_id=a6.PROVIDER_ID,
                                   model=a6.MODEL, reasoning_effort="none")


def setup(tmp_path, count=1, maximum=100, dns_fallback="off", failure_policy=None):
    profile, root = captured(tmp_path), tmp_path / "campaign"
    control = a6.initialize_campaign([case_at(i) for i in range(count)], root, profile=profile,
                                    maximum_concurrency=maximum, _test_only=True,
                                    http_dns_fallback=dns_fallback, provider_failure_policy=failure_policy)
    return root, profile, control


def request_for(profile, **changes):
    body = {"model": a6.MODEL, "reasoning": {"effort": "none"}, "max_output_tokens": 8192,
            "store": False, "input": "PRIVATE_BODY_NOT_FOR_LEDGER"}
    body.update(changes)
    return httpx.Request("POST", profile.base_url + "/responses", json=body,
                         headers={"Authorization": "Bearer NEVER_LOG_THIS_HEADER"})


def source_hashes(source):
    return {path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in source.rglob("*") if path.is_file()}


@pytest.mark.asyncio
async def test_actual_a6_wire_policy_is_explicit_and_no_private_body_is_logged(tmp_path):
    profile = captured(tmp_path)
    path = tmp_path / "http.json"
    options = {"binding": "a6-bound", "case_step_bounds": {"task": 20}, "wire_policy": a6.WIRE_POLICY}
    budget = serial.UnmeteredMultiTurnHTTPBudget(path, create=True, **options)
    request = request_for(profile)
    await budget.before_request(request, profile=profile, case_id="task")
    await budget.after_response(httpx.Response(200, request=request))
    document = serial.UnmeteredMultiTurnHTTPBudget(path, **options).snapshot()
    assert document["maximum"] is None and document["wire_policy"] == a6.WIRE_POLICY
    call = document["calls"][0]
    assert call["wire_model"] == a6.MODEL and call["wire_protocol"] == a6.PROTOCOL
    assert call["wire_reasoning_effort"] == "none" and call["wire_max_output_tokens"] == 8192
    assert call["wire_store"] is False and call["thinking_disabled_verified"] is True
    assert call["status_code"] == 200 and call["path"] == "/v1/responses"
    assert all(value not in path.read_text(encoding="utf-8") for value in
               ("PRIVATE_BODY_NOT_FOR_LEDGER", "NEVER_LOG_THIS_HEADER", "fake-current-key"))
    with pytest.raises(ValueError, match="identity changed"):
        serial.UnmeteredMultiTurnHTTPBudget(path, binding="a6-bound", case_step_bounds={"task": 20})


@pytest.mark.asyncio
async def test_thinking_storage_model_and_endpoint_mismatches_never_reserve_http(tmp_path):
    profile = captured(tmp_path)
    budget = serial.UnmeteredMultiTurnHTTPBudget(tmp_path / "http.json", binding="a6-bound",
        case_step_bounds={"task": 20}, wire_policy=a6.WIRE_POLICY, create=True)
    for changes in ({"model": "deepseek-flash"}, {"reasoning": {"effort": "high"}}, {"reasoning": None},
                    {"store": True}, {"store": None}, {"max_output_tokens": True}, {"max_output_tokens": 2048}):
        with pytest.raises(ProviderError):
            await budget.before_request(request_for(profile, **changes), profile=profile, case_id="task")
    for request in (httpx.Request("GET", profile.base_url + "/responses"),
                    httpx.Request("POST", "https://other.invalid/v1/responses")):
        with pytest.raises(ProviderError):
            await budget.before_request(request, profile=profile, case_id="task")
    assert budget.snapshot()["calls"] == []


def test_wrong_captured_identity_or_policy_rejects_before_creating_evidence(tmp_path):
    profile = captured(tmp_path)
    for changes in ({"provider_id": "different"}, {"base_url": "https://other.invalid/v1"},
                    {"protocol": "openai_chat"}, {"model": "deepseek-flash"}, {"reasoning_effort": "high"}):
        with pytest.raises(ValueError, match="wire policy"):
            a6.initialize_campaign([case_at(0)], tmp_path / "rejected", profile=replace(profile, **changes),
                                   _test_only=True)
        assert not (tmp_path / "rejected/execution-control.json").exists()
    with pytest.raises(ValueError, match="owns its fixed"):
        a6.initialize_campaign([case_at(0)], tmp_path / "rejected", profile=profile,
                               wire_policy=None, _test_only=True)
    with pytest.raises(ValueError, match="Unknown"):
        serial.initialize_campaign([case_at(0)], tmp_path / "rejected", profile=profile,
                                   wire_policy="arbitrary-policy", _test_only=True)
    with pytest.raises(ValueError, match="DNS policy"):
        a6.initialize_campaign([case_at(0)], tmp_path / "rejected", profile=profile,
                               http_dns_fallback="unverified-private", _test_only=True)


def paid_files(root, cases, *, policy=a6.WIRE_POLICY, adapter_source=True, dns_fallback="off", failure_policy=None):
    root.mkdir()
    plan = {"backend_mode": a6.MODE, "maximum_concurrency": 100,
        "concurrency_performance_policy": a6.PERFORMANCE_POLICY,
        "groups": [{"entry_ids": [case["id"]]} for case in cases], "authorized_unlimited_api": True,
        "api_unlimited": True, "authorized_api_call_limit": None, "max_output_tokens": 8192,
        "inherited_runtime_safety": serial.INHERITED_RUNTIME_SAFETY,
        "case_deadline_seconds": serial.CASE_DEADLINE_SECONDS,
        "case_tool_safety_limit": serial.CASE_TOOL_SAFETY_LIMIT, "wire_policy": policy,
        "http_dns_fallback": dns_fallback}
    if failure_policy is not None:
        plan["provider_failure_policy"] = failure_policy
    (root / "campaign-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    paths = [Path(concurrent.__file__), *([Path(a6.__file__)] if adapter_source else [])]
    manifest = {"files": [{"file": path.relative_to(PROJECT).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in paths]}
    (root / "source-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_paid_admission_uses_captured_identity_without_constructing_client_or_mutating_runtime(tmp_path, monkeypatch):
    profile, cases = captured(tmp_path), [case_at(0), case_at(1)]
    before = source_hashes(profile.source)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Local paid admission must not construct a model client")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    root = tmp_path / "paid"
    paid_files(root, cases)
    control = a6.initialize_campaign(cases, root, profile=profile, maximum_concurrency=100)
    assert control["test_only"] is False and control["wire_policy"] == a6.WIRE_POLICY
    assert control["profile"]["provider_id"] == a6.PROVIDER_ID and control["profile"]["model"] == a6.MODEL
    assert control["concurrent_tasks"]["maximum"] == 100
    assert json.loads((root / "results/http-budget.json").read_bytes())["calls"] == []
    assert source_hashes(profile.source) == before
    for label, kwargs in (("wrong-plan", {"policy": None}), ("missing-source", {"adapter_source": False}),
                          ("dns-plan-mismatch", {"dns_fallback": "google"})):
        rejected = tmp_path / label
        paid_files(rejected, cases, **kwargs)
        with pytest.raises(ValueError):
            a6.initialize_campaign(cases, rejected, profile=profile, maximum_concurrency=100)
        assert not (rejected / "execution-control.json").exists()


@pytest.mark.asyncio
async def test_five_concurrent_a6_tasks_keep_cross_turn_state_and_counters_separate(tmp_path):
    root, profile, _control = setup(tmp_path, count=5)
    arrived, release = set(), asyncio.Event()

    def transport(index):
        script, count = phases(index), 0

        async def handle(request):
            nonlocal count
            offset, count = count, count + 1
            body = json.loads(request.content)
            assert body["model"] == a6.MODEL and body["reasoning"] == {"effort": "none"}
            assert body["store"] is False and body["max_output_tokens"] == 8192
            if offset == 0:
                arrived.add(index)
                if len(arrived) == 5:
                    release.set()
                await asyncio.wait_for(release.wait(), 15)
            return response(request, index=offset, **script[offset])

        return lambda: httpx.MockTransport(handle)

    rows = await asyncio.gather(*(a6.run_one_current_multiturn(f"multi_turn_base_{i}", root,
        _profile=profile, _test_transport_factory=transport(i)) for i in (4, 2, 0, 3, 1)))
    assert arrived == set(range(5)) and len({row["session_id"] for row in rows}) == 5
    assert all(row["verification_result"] == "pass" and row["result"] == "unsupported"
               and len(row["runs"]) == 2 and row["http_attempts"] == 4 for row in rows)
    report = a6.finalize_campaign(root)
    assert report["http_transport_attempts"] == 20 and report["model_api_calls"] == 0
    assert report["wire_policy"] == a6.WIRE_POLICY and report["fatal"] is None
    assert a6.read_task_receipt(root, rows[0]["id"]) == rows[0]


@pytest.mark.asyncio
async def test_a6_unknown_http_outcome_is_not_retried_and_blocks_later_task(tmp_path):
    root, profile, _ = setup(tmp_path, count=2)
    calls = []

    def broken(request):
        calls.append(request)
        raise httpx.ReadError("PRIVATE_EXCEPTION_NOT_FOR_REPORT", request=request)

    factory = lambda: httpx.MockTransport(broken)
    first = await a6.run_one_current_multiturn("multi_turn_base_0", root, _profile=profile,
                                            _test_transport_factory=factory)
    later = await a6.run_one_current_multiturn("multi_turn_base_1", root, _profile=profile,
                                            _test_transport_factory=factory)
    assert first["status"] == "case_error" and first["http_attempts"] == 1
    assert later["status"] == "not_run" and len(calls) == 1
    report = a6.finalize_campaign(root)
    assert report["fatal"] == "unknown_provider_request_outcome"
    assert "PRIVATE_EXCEPTION_NOT_FOR_REPORT" not in json.dumps(report)
    repeated = await a6.run_one_current_multiturn("multi_turn_base_0", root, _profile=profile,
                                               _test_transport_factory=factory)
    assert repeated == first and len(calls) == 1


@pytest.mark.asyncio
async def test_native_campaign_cannot_be_dispatched_through_a6_adapter(tmp_path):
    profile = capture_current_profile(source_at(tmp_path), model="deepseek-flash", reasoning_effort="none")
    root = tmp_path / "native"
    control = concurrent.initialize_campaign([case_at(0)], root, profile=profile, _test_only=True)
    assert "wire_policy" not in control
    ledger = json.loads((root / "results/http-budget.json").read_bytes())
    assert "wire_policy" not in ledger
    with pytest.raises(ValueError, match="A6-admitted"):
        await a6.run_one_current_multiturn("multi_turn_base_0", root, _profile=profile,
            _test_transport_factory=lambda: (_ for _ in ()).throw(AssertionError("no model client")))
    assert not (root / "results/task-locks").exists()


@pytest.mark.asyncio
async def test_explicit_dns_snapshot_reaches_existing_compatible_transport(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from aster.evaluation import bfcl_v4_live as live

    profile, seen = captured(tmp_path), []

    async def after(_response):
        pass

    def transport(base_url, *, dns_fallback):
        seen.append((base_url, dns_fallback))

        def forbidden(_request):
            raise AssertionError("Constructing a client must not dispatch")

        return httpx.MockTransport(forbidden)

    monkeypatch.setattr(live, "PublicProviderTransport", transport)
    for fallback in ("off", "google"):
        driver = live._CurrentModelDriver(profile, SimpleNamespace(after_response=after), "task",
            protocol_budget=None, max_output_tokens=8192, dns_fallback=fallback)
        await driver.aclose()
    assert seen == [(a6.BASE_URL, "off"), (a6.BASE_URL, "google")]


@pytest.mark.asyncio
async def test_a6_campaign_dns_policy_is_bound_and_reaches_each_task_driver(tmp_path, monkeypatch):
    root, profile, control = setup(tmp_path, dns_fallback="google")
    assert control["http_dns_fallback"] == "google"
    real_driver, seen, script = concurrent._CurrentModelDriver, [], phases(0)

    def recording_driver(*args, **kwargs):
        seen.append(kwargs["dns_fallback"])
        return real_driver(*args, **kwargs)

    requests = []

    def handle(request):
        index = len(requests)
        requests.append(request)
        return response(request, index=index, **script[index])

    monkeypatch.setattr(concurrent, "_CurrentModelDriver", recording_driver)
    row = await a6.run_one_current_multiturn("multi_turn_base_0", root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(handle))
    assert seen == ["google"] and row["verification_result"] == "pass" and len(requests) == 4
    control["http_dns_fallback"] = "unverified"
    (root / "execution-control.json").write_text(json.dumps(control), encoding="utf-8")
    with pytest.raises(ValueError, match="policy changed"):
        a6.read_task_receipt(root, row["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [502, 503, 504])
async def test_explicit_gateway_policy_isolates_only_failed_task_without_retry(tmp_path, status):
    root, profile, _ = setup(tmp_path, count=2, failure_policy=a6.GATEWAY_FAILURE_POLICY)
    requests, script = [], phases(1)

    def gateway(request):
        requests.append(request)
        return httpx.Response(status, request=request, text="PRIVATE_GATEWAY_BODY_MUST_NOT_APPEAR")

    row = await a6.run_one_current_multiturn("multi_turn_base_0", root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(gateway))
    assert row["result"] == "case_error" and row["task_local_gateway_error"] is True
    assert row["provider_failure_policy"] == a6.GATEWAY_FAILURE_POLICY
    assert row["http_attempts"] == 1 and len(requests) == 1
    assert all(item.get("input_tokens") is None and item.get("output_tokens") is None
               for item in row.get("usage", []))

    def successful(request):
        index = len(requests) - 1
        requests.append(request)
        return response(request, index=index, **script[index])

    second = await a6.run_one_current_multiturn("multi_turn_base_1", root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(successful))
    assert second["verification_result"] == "pass" and second["http_attempts"] == 4
    report = a6.finalize_campaign(root)
    assert report["fatal"] is None and report["http_transport_attempts"] == 5
    assert report["provider_failure_policy"] == a6.GATEWAY_FAILURE_POLICY
    assert "PRIVATE_GATEWAY_BODY_MUST_NOT_APPEAR" not in json.dumps(report)
    repeated = await a6.run_one_current_multiturn("multi_turn_base_0", root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(gateway))
    assert repeated == row and len(requests) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["authentication", "other_server", "protocol", "timeout", "unknown"])
async def test_gateway_policy_does_not_isolate_auth_protocol_timeout_or_unknown(tmp_path, kind):
    root, profile, _ = setup(tmp_path, count=2, failure_policy=a6.GATEWAY_FAILURE_POLICY)
    requests = []

    def broken(request):
        requests.append(request)
        if kind == "authentication":
            return httpx.Response(401, request=request)
        if kind == "other_server":
            return httpx.Response(500, request=request)
        if kind == "protocol":
            return httpx.Response(200, request=request, headers={"content-type": "application/json"}, text="{}")
        if kind == "timeout":
            raise TimeoutError("PRIVATE_TIMEOUT_MUST_NOT_APPEAR")
        raise httpx.ReadError("PRIVATE_UNKNOWN_MUST_NOT_APPEAR", request=request)

    factory = lambda: httpx.MockTransport(broken)
    row = await a6.run_one_current_multiturn("multi_turn_base_0", root, _profile=profile,
                                           _test_transport_factory=factory)
    assert row["result"] == "case_error" and not row.get("task_local_gateway_error")
    second = await a6.run_one_current_multiturn("multi_turn_base_1", root, _profile=profile,
                                              _test_transport_factory=factory)
    assert second["result"] == "not_run" and len(requests) == 1
    report = a6.finalize_campaign(root)
    expected = "unknown_provider_request_outcome" if kind in {"timeout", "unknown"} else "provider_infrastructure_error"
    assert report["fatal"] == expected
    assert "PRIVATE_" not in json.dumps(report)


@pytest.mark.asyncio
async def test_late_gateway_receipt_never_clears_another_workers_unknown_stop(tmp_path):
    root, profile, _ = setup(tmp_path, count=3, failure_policy=a6.GATEWAY_FAILURE_POLICY)
    peer_entered, release_peer = asyncio.Event(), asyncio.Event()

    async def unknown(request):
        await peer_entered.wait()
        raise httpx.ReadError("unknown", request=request)

    async def gateway(request):
        peer_entered.set()
        await release_peer.wait()
        return httpx.Response(503, request=request)

    first = asyncio.create_task(a6.run_one_current_multiturn("multi_turn_base_0", root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(unknown)))
    peer = asyncio.create_task(a6.run_one_current_multiturn("multi_turn_base_1", root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(gateway)))
    try:
        await asyncio.wait_for(first, 15)
    finally:
        release_peer.set()
    row = await asyncio.wait_for(peer, 15)
    assert row["task_local_gateway_error"] is True
    third = await a6.run_one_current_multiturn("multi_turn_base_2", root, _profile=profile,
        _test_transport_factory=lambda: (_ for _ in ()).throw(AssertionError("stopped before model")))
    assert third["result"] == "not_run"
    assert a6.finalize_campaign(root)["fatal"] == "unknown_provider_request_outcome"


def test_gateway_policy_requires_explicit_paid_plan_and_is_bound_against_later_changes(tmp_path):
    profile, cases = captured(tmp_path), [case_at(0)]
    root = tmp_path / "paid-gateway"
    paid_files(root, cases, failure_policy=a6.GATEWAY_FAILURE_POLICY)
    with pytest.raises(ValueError, match="approved no-quota plan"):
        a6.initialize_campaign(cases, root, profile=profile, maximum_concurrency=100)
    control = a6.initialize_campaign(cases, root, profile=profile, maximum_concurrency=100,
        provider_failure_policy=a6.GATEWAY_FAILURE_POLICY)
    assert control["provider_failure_policy"] == a6.GATEWAY_FAILURE_POLICY
    assert json.loads((root / "results/http-budget.json").read_bytes())["calls"] == []
    control.pop("provider_failure_policy")
    (root / "execution-control.json").write_text(json.dumps(control), encoding="utf-8")
    with pytest.raises(ValueError, match="provider policy differs"):
        a6.read_task_receipt(root, cases[0]["id"])


def test_gateway_policy_is_unavailable_to_legacy_native_campaigns(tmp_path):
    profile = capture_current_profile(source_at(tmp_path), model="deepseek-flash", reasoning_effort="none")
    with pytest.raises(ValueError, match="provider failure policy"):
        concurrent.initialize_campaign([case_at(0)], tmp_path / "native-policy", profile=profile,
            provider_failure_policy=a6.GATEWAY_FAILURE_POLICY, _test_only=True)
