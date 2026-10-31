import asyncio
import hashlib
import json
import sqlite3

import httpx
import pytest

from aster.customization.credentials import CredentialStore
from aster.evaluation.bfcl_current import HTTPCallBudget, capture_current_profile, run_current_bfcl
from aster.providers.deepseek import ProviderError


def source_at(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text("model: wrong-global\nreasoning_effort: high\n", encoding="utf-8")
    agent = source / "agents/default"
    agent.mkdir(parents=True)
    (agent / "config.yaml").write_text("provider_id: deepseek\nmodel: deepseek-flash\nreasoning_effort: low\n", encoding="utf-8")
    metadata = {"name": "DeepSeek", "base_url": "https://api.deepseek.com", "protocol": "deepseek_responses",
                "identity_revision": 3, "models": [{"id": "deepseek-flash", "context_window": 100000,
                "max_output_tokens": 8192, "input_modalities": ["text"], "effort_levels": ["none", "low", "high"]}]}
    db = sqlite3.connect(source / "runtime.db")
    db.execute("CREATE TABLE provider_profiles(id TEXT,document TEXT,credential_ref TEXT,deleted INTEGER)")
    db.execute("INSERT INTO provider_profiles VALUES(?,?,?,0)", ("deepseek", json.dumps(metadata), "provider-fixture"))
    db.commit()
    db.close()
    CredentialStore(data_dir=source).set("provider-fixture", "fake-current-key")
    return source


def corpus(tmp_path, count=2):
    data, answers = tmp_path / "questions.jsonl", tmp_path / "answers.jsonl"
    function = {"name": "calculate", "description": "Calculate the area.", "parameters": {"type": "dict",
                "properties": {"base": {"type": "integer"}}, "required": ["base"]}}
    data.write_text("\n".join(json.dumps({"id": f"simple_python_{i}", "question": [[{"role": "user", "content": "Use base 10."}]],
                                         "function": [function]}) for i in range(count)), encoding="utf-8")
    answers.write_text("\n".join(json.dumps({"id": f"simple_python_{i}", "ground_truth": [{"calculate": {"base": [10]}}]})
                                  for i in range(count)), encoding="utf-8")
    return data, answers


def success(request):
    assert request.url.path == "/responses" and request.method == "POST"
    body = json.loads(request.content)
    assert body["model"] == "deepseek-flash" and body["reasoning"] == {"effort": "low"}
    assert "ground_truth" not in request.content.decode()
    event = {"type": "response.completed", "response": {"status": "completed", "output": [
             {"type": "function_call", "call_id": "test-call", "name": "calculate", "arguments": '{"base":10}'}],
             "usage": {"input_tokens": 20, "output_tokens": 5}}}
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          text="event: response.completed\ndata: " + json.dumps(event) + "\n\n")


def test_capture_uses_agent_selection_and_never_exports_key_or_mutates_source(tmp_path):
    source = source_at(tmp_path)
    before = {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob("*") if p.is_file()}
    captured = capture_current_profile(source)
    assert captured.model == "deepseek-flash" and captured.reasoning_effort == "low"
    assert captured.identity_revision == 3 and captured.key() == "fake-current-key"
    assert "fake-current-key" not in repr(captured) + json.dumps(captured.public_summary())
    after = {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob("*") if p.is_file()}
    assert before == after


def test_rotated_key_or_changed_origin_rejects_captured_identity(tmp_path):
    source = source_at(tmp_path)
    captured = capture_current_profile(source)
    CredentialStore(data_dir=source).set("provider-fixture", "replacement-fixture-key")
    with pytest.raises(ProviderError):
        captured.key()
    CredentialStore(data_dir=source).set("provider-fixture", "fake-current-key")
    db = sqlite3.connect(source / "runtime.db")
    row = json.loads(db.execute("SELECT document FROM provider_profiles").fetchone()[0])
    row["base_url"] = "https://other.example"
    db.execute("UPDATE provider_profiles SET document=?", (json.dumps(row),))
    db.commit()
    db.close()
    with pytest.raises(ProviderError):
        captured.key()


@pytest.mark.asyncio
@pytest.mark.parametrize("maximum", [2, 40])
async def test_http_budget_caps_concurrent_attempts_and_forbids_replay(tmp_path, maximum):
    source = source_at(tmp_path)
    captured = capture_current_profile(source)
    budget = HTTPCallBudget(tmp_path / "budget.json", maximum=maximum, binding="fixture")
    requests = [httpx.Request("POST", "https://api.deepseek.com/responses") for _ in range(maximum + 1)]
    results = await asyncio.gather(*(budget.before_request(request, profile=captured, case_id=str(index))
                                     for index, request in enumerate(requests)), return_exceptions=True)
    assert sum(isinstance(result, ProviderError) for result in results) == 1
    assert len(budget.snapshot()["calls"]) == maximum
    with pytest.raises(ValueError):
        HTTPCallBudget(tmp_path / "budget.json", maximum=maximum, binding="fixture")
    for invalid in (0, 41, True, -1, None, 2.5):
        with pytest.raises(ValueError):
            HTTPCallBudget(tmp_path / "unused.json", maximum=invalid, binding="fixture")


@pytest.mark.asyncio
async def test_existing_ten_request_budget_cannot_be_upgraded_reset_or_rebound(tmp_path):
    captured = capture_current_profile(source_at(tmp_path))
    path = tmp_path / "pilot-budget.json"
    budget = HTTPCallBudget(path, maximum=10, binding="original-pilot")
    initial = path.read_bytes()
    for maximum, binding in ((40, "original-pilot"), (10, "new-campaign")):
        with pytest.raises(ValueError):
            HTTPCallBudget(path, maximum=maximum, binding=binding)
        assert path.read_bytes() == initial
    for index in range(10):
        await budget.before_request(httpx.Request("POST", "https://api.deepseek.com/responses"),
                                    profile=captured, case_id=str(index))
    with pytest.raises(ProviderError, match="budget exhausted"):
        await budget.before_request(httpx.Request("POST", "https://api.deepseek.com/responses"),
                                    profile=captured, case_id="extra")
    used = path.read_bytes()
    for maximum in (10, 40):
        with pytest.raises(ValueError):
            HTTPCallBudget(path, maximum=maximum, binding="original-pilot")
        assert path.read_bytes() == used
    assert budget.snapshot()["maximum"] == 10 and len(budget.snapshot()["calls"]) == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("maximum", [2, 40])
async def test_current_transport_reuses_production_protocol_and_hard_api_limit(tmp_path, maximum):
    source = source_at(tmp_path)
    captured = capture_current_profile(source)
    data, answers = corpus(tmp_path, count=maximum + 1)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return success(request)

    report = await run_current_bfcl(data, answers, tmp_path / "report.json", profile=captured,
        case_ids=[f"simple_python_{i}" for i in range(maximum + 1)], max_api_calls=maximum,
        _test_transport_factory=lambda: httpx.MockTransport(handler))
    assert calls == ["/responses"] * maximum
    assert report["api_calls_reserved"] == maximum and report["metadata_requests"] == 0
    assert report["totals"] == {"unsupported": maximum, "not_run": 1}
    assert all(row["prediction_check"]["passed"] for row in report["results"][:maximum])
    assert report["official_score"] is False and report["paid_calls_authorized"] is False
    assert "fake-current-key" not in (tmp_path / "report.json").read_text()


@pytest.mark.asyncio
async def test_campaign_requires_explicit_valid_api_budget_before_loading_or_dispatch(tmp_path):
    captured = capture_current_profile(source_at(tmp_path))
    output = tmp_path / "never-created.json"
    arguments = (tmp_path / "missing-data.jsonl", tmp_path / "missing-answers.jsonl", output)
    with pytest.raises(TypeError, match="max_api_calls"):
        await run_current_bfcl(*arguments, profile=captured, case_ids=["simple_python_0"])
    for maximum in (0, 41, True, -1, None, 2.5):
        with pytest.raises(ValueError, match="API cap"):
            await run_current_bfcl(*arguments, profile=captured, case_ids=["simple_python_0"],
                                   max_api_calls=maximum)
    assert not output.exists() and not output.with_suffix(".api-budget.json").exists()


@pytest.mark.asyncio
async def test_authentication_failure_stops_remaining_cases_without_retries(tmp_path):
    source = source_at(tmp_path)
    data, answers = corpus(tmp_path)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(401, json={"error": {"message": "fixture private secret"}})

    report = await run_current_bfcl(data, answers, tmp_path / "auth.json", profile=capture_current_profile(source),
        case_ids=["simple_python_0", "simple_python_1"], max_api_calls=10,
        _test_transport_factory=lambda: httpx.MockTransport(handler))
    assert len(calls) == 1 and report["api_calls_reserved"] == 1
    assert report["results"][1]["reason"] == "fatal_provider_failure"
    assert report["results"][0]["model_failures"][0]["status_code"] == 401
    assert "fixture private secret" not in (tmp_path / "auth.json").read_text()


@pytest.mark.asyncio
async def test_metadata_and_other_origins_are_blocked_before_budget_or_dispatch(tmp_path):
    source = source_at(tmp_path)
    captured = capture_current_profile(source)
    budget = HTTPCallBudget(tmp_path / "budget.json", maximum=10, binding="fixture")
    for method, url in (("GET", "https://api.deepseek.com/models"), ("POST", "https://other.example/responses")):
        with pytest.raises(ProviderError):
            await budget.before_request(httpx.Request(method, url), profile=captured, case_id="fixture")
    assert not budget.snapshot()["calls"]


def test_key_resolver_returns_the_same_single_validated_read(tmp_path, monkeypatch):
    from aster.evaluation import bfcl_current

    source = source_at(tmp_path)
    captured = capture_current_profile(source)
    original = bfcl_current._read_key
    reads = []

    def read(*args):
        reads.append(True)
        return original(*args)

    monkeypatch.setattr(bfcl_current, "_read_key", read)
    assert captured.key() == "fake-current-key"
    assert len(reads) == 1


@pytest.mark.asyncio
async def test_native_client_preserves_proxy_flag_disables_redirects_and_closes(tmp_path, monkeypatch):
    from aster.evaluation import bfcl_current

    source = source_at(tmp_path)
    data, answers = corpus(tmp_path, count=1)
    original = httpx.AsyncClient
    clients, options = [], []

    def create(**kwargs):
        options.append(kwargs)
        client = original(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(bfcl_current.httpx, "AsyncClient", create)
    await run_current_bfcl(data, answers, tmp_path / "proxy.json", profile=capture_current_profile(source),
        case_ids=["simple_python_0"], max_api_calls=1, _test_transport_factory=lambda: httpx.MockTransport(success))
    assert options[0]["trust_env"] is True and options[0]["follow_redirects"] is False
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_transient_report_lock_retries_local_write_without_replaying_api(tmp_path, monkeypatch):
    from aster.evaluation import bfcl_current

    source = source_at(tmp_path)
    data, answers = corpus(tmp_path, count=1)
    original = bfcl_current._write_report
    attempts, calls = [], []

    def write(output, report):
        if report["results"]:
            attempts.append(True)
            if len(attempts) == 1:
                raise PermissionError("fixture report reader lock")
        original(output, report)

    def handle(request):
        calls.append(True)
        return success(request)

    monkeypatch.setattr(bfcl_current, "_write_report", write)
    output = tmp_path / "report.json"
    report = await run_current_bfcl(data, answers, output, profile=capture_current_profile(source),
        case_ids=["simple_python_0"], max_api_calls=1, _test_transport_factory=lambda: httpx.MockTransport(handle))
    assert len(calls) == 1 and report["api_calls_reserved"] == 1
    assert len(attempts) >= 2
    receipts = list(output.with_suffix(".receipts").glob("*.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())["result"]["prediction_check"]["passed"]


@pytest.mark.asyncio
async def test_persistent_final_report_failure_preserves_all_receipts_without_api_replay(tmp_path, monkeypatch):
    from aster.evaluation import bfcl_current

    source = source_at(tmp_path)
    data, answers = corpus(tmp_path, count=2)
    original = bfcl_current._write_report
    calls, replacements = [], []

    def write(output, report):
        if len(report["results"]) == 2:
            replacements.append(True)
            raise PermissionError("fixture final reader lock")
        original(output, report)

    def handle(request):
        calls.append(True)
        return success(request)

    monkeypatch.setattr(bfcl_current, "_write_report", write)
    output = tmp_path / "report.json"
    options = {"profile": capture_current_profile(source), "case_ids": ["simple_python_0", "simple_python_1"],
               "max_api_calls": 2, "_test_transport_factory": lambda: httpx.MockTransport(handle)}
    with pytest.raises(PermissionError):
        await run_current_bfcl(data, answers, output, **options)
    assert len(calls) == 2 and len(replacements) == 6
    assert len(json.loads(output.read_text())["results"]) == 1
    receipts = [json.loads(path.read_text())["result"] for path in output.with_suffix(".receipts").glob("*.json")]
    assert {row["id"] for row in receipts} == {"simple_python_0", "simple_python_1"}
    assert all(row["prediction_check"]["passed"] for row in receipts)
    assert len(json.loads(output.with_suffix(".api-budget.json").read_text())["calls"]) == 2
    with pytest.raises(ValueError):
        await run_current_bfcl(data, answers, output, **options)
    assert len(calls) == 2
