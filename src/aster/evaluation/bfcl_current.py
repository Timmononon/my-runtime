"""BFCL campaign with a read-only captured provider and a total HTTP call budget.

No production Store is opened. Credentials remain in the source credential
store and are resolved only against the captured, revalidated endpoint identity.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sqlite3
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from aster.config import read_yaml
from aster.customization.credentials import CredentialStore
from aster.evaluation.bfcl import load_cases
from aster.evaluation.bfcl_runner import _NoCredentials, _run_case, _write_report
from aster.platform.filesystem import FileLock, atomic_write
from aster.providers.compatible import CompatibleClient, ScopedModel
from aster.providers.deepseek import DeepSeekResponsesClient, ModelCapabilities, ProviderError
from aster.providers.profiles import PublicProviderTransport, endpoint_url

MAX_API_CALLS = 40


def _write(path, value):
    atomic_write(Path(path), json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode())


async def _persist_report(output, report):
    """Retry only a known local replacement failure, never a model request."""
    for attempt in range(6):
        try:
            _write_report(output, report)
            return
        except PermissionError:
            if attempt == 5:
                raise
            await asyncio.sleep(min(.05 * 2**attempt, .4))


def _persist_result_receipt(output, report, result):
    """Keep each completed result independent of a contended summary path."""
    root = Path(output).with_suffix(".receipts")
    root.mkdir(parents=True, exist_ok=True)
    identifier = hashlib.sha256(result["id"].encode()).hexdigest()[:24]
    target = root / (identifier + ".json")
    if target.exists():
        raise ValueError("A completed result receipt must not be overwritten")
    _write(target, {"schema_version": 1, "dataset_binding": report["dataset_binding"], "result": result,
                    "api_calls_reserved": report["api_calls_reserved"]})


def _row(data_dir, provider_id):
    database = Path(data_dir) / "runtime.db"
    if not database.is_file():
        raise ValueError("Current provider metadata database is missing")
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT document,credential_ref FROM provider_profiles WHERE id=? AND deleted=0",
                                 (provider_id,)).fetchone()
        if row is None:
            raise ValueError("Selected provider is unavailable")
        return json.loads(row["document"]), row["credential_ref"]
    finally:
        connection.close()


def _read_key(source, credential_ref):
    if not isinstance(credential_ref, str):
        raise TypeError("Selected provider has no credential binding")
    store = CredentialStore(data_dir=source)
    key = store.get("deepseek" if credential_ref == "legacy:deepseek" else credential_ref,
                    use_environment=False)
    if not key:
        raise ValueError("The captured provider has no configured file credential")
    return key


@dataclass(frozen=True)
class CapturedProfile:
    source: Path = field(repr=False)
    provider_id: str
    name: str
    base_url: str
    protocol: str
    model: str
    reasoning_effort: str | None
    identity_revision: int
    capabilities: dict = field(repr=False)
    credential_ref: str = field(repr=False)
    credential_digest: str = field(repr=False)

    def public_summary(self):
        return {"provider_id": self.provider_id, "provider_name": self.name, "base_url": self.base_url,
                "protocol": self.protocol, "model": self.model, "reasoning_effort": self.reasoning_effort,
                "identity_revision": self.identity_revision, "capabilities": copy.deepcopy(self.capabilities)}

    def _validated_key(self):
        document, reference = _row(self.source, self.provider_id)
        if (document.get("base_url") != self.base_url or document.get("protocol") != self.protocol
                or document.get("identity_revision", 1) != self.identity_revision or reference != self.credential_ref):
            raise ProviderError("Captured provider identity changed", category="endpoint")
        key = _read_key(self.source, reference)
        if hashlib.sha256(key.encode()).hexdigest() != self.credential_digest:
            raise ProviderError("Captured provider credential changed", category="authentication")
        return key

    def validate(self):
        self._validated_key()

    def key(self):
        return self._validated_key()


def capture_current_profile(data_dir, *, provider_id=None, model=None, reasoning_effort=None):
    """Read global/default-agent settings and provider metadata without migration."""
    source = Path(data_dir).expanduser().resolve()
    settings = {"provider_id": "deepseek", "model": "deepseek-flash", "reasoning_effort": "high"}
    for path in (source / "config.yaml", source / "agents/default/config.yaml"):
        document = read_yaml(path)
        settings.update({key: document[key] for key in settings if key in document})
    pid = provider_id if provider_id is not None else settings["provider_id"]
    mid = model if model is not None else settings["model"]
    effort = reasoning_effort if reasoning_effort is not None else settings["reasoning_effort"]
    if not isinstance(pid, str) or not isinstance(mid, str) or not pid or not mid:
        raise ValueError("Current provider and model selection is invalid")
    document, reference = _row(source, pid)
    protocol = document.get("protocol")
    base_url = endpoint_url(document.get("base_url"), protocol)
    caps = next((row for row in document.get("models", []) if row.get("id") == mid), None)
    if caps is None or effort is not None and effort not in caps.get("effort_levels", []):
        raise ValueError("Selected model/effort is absent from cached capabilities")
    key = _read_key(source, reference)
    return CapturedProfile(source, pid, document.get("name", pid), base_url, protocol, mid, effort,
                           document.get("identity_revision", 1), copy.deepcopy(caps), reference,
                           hashlib.sha256(key.encode()).hexdigest())


class HTTPCallBudget:
    """Reserve every attempted target API request before any transport dispatch."""

    def __init__(self, path, *, maximum, binding):
        if type(maximum) is not int or not 1 <= maximum <= MAX_API_CALLS:
            raise ValueError(f"This campaign requires an explicit total API cap from 1 to {MAX_API_CALLS}")
        self.path, self.maximum = Path(path), maximum
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock():
            if self.path.exists():
                previous = self.snapshot_unlocked()
                if previous.get("binding") != binding or previous.get("maximum") != maximum or previous.get("calls"):
                    raise ValueError("Existing API budget cannot be rebound or replayed")
            else:
                _write(self.path, {"schema_version": 1, "maximum": maximum, "binding": binding, "calls": []})

    def lock(self):
        return FileLock(self.path.with_suffix(self.path.suffix + ".lock"), timeout=10)

    def snapshot_unlocked(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def snapshot(self):
        with self.lock():
            return self.snapshot_unlocked()

    async def before_request(self, request, *, profile, case_id):
        profile.validate()
        expected = profile.base_url + ("/chat/completions" if profile.protocol == "openai_chat" else "/responses")
        if request.method != "POST" or str(request.url) != expected:
            raise ProviderError("Unexpected API operation rejected before dispatch", category="endpoint")
        with self.lock():
            document = self.snapshot_unlocked()
            if len(document["calls"]) >= self.maximum:
                raise ProviderError("Total API budget exhausted", category="budget")
            index = len(document["calls"])
            document["calls"].append({"case_id": case_id, "method": request.method, "path": request.url.path,
                                      "started_at": time.time(), "status": "outcome_unknown"})
            _write(self.path, document)
        request.extensions["aster_bfcl_api_index"] = index

    async def after_response(self, response):
        index = response.request.extensions.get("aster_bfcl_api_index")
        if index is None:
            return
        with self.lock():
            document = self.snapshot_unlocked()
            document["calls"][index].update(status="response_headers_received", status_code=response.status_code)
            _write(self.path, document)


class _ObservedModel:
    def __init__(self, inner, failures):
        self.inner, self.failures = inner, failures

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def generate(self, *args, **kwargs):
        try:
            return await self.inner.generate(*args, **kwargs)
        except Exception as error:
            failure = {"type": type(error).__name__, "category": getattr(error, "category", "unknown"),
                       "status_code": getattr(error, "status_code", None)}
            self.failures.append(failure)
            raise


async def run_current_bfcl(data_path, answers_path, output, *, profile, case_ids, max_api_calls,
                           max_output_tokens=1024, _test_transport_factory=None):
    """Run actual current-model predictions; an injected transport is test-only.

    No metadata endpoint is called. The explicit API cap covers every attempted
    request including failures; provider and engine retries are both disabled.
    """
    if not isinstance(profile, CapturedProfile):
        raise TypeError("A read-only captured provider is required")
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 2048:
        raise ValueError("Output limit must be between 1 and 2048")
    if (not isinstance(case_ids, (list, tuple)) or not case_ids
            or any(not isinstance(case_id, str) for case_id in case_ids) or len(set(case_ids)) != len(case_ids)):
        raise ValueError("Explicit distinct case IDs are required")
    if type(max_api_calls) is not int or not 1 <= max_api_calls <= MAX_API_CALLS:
        raise ValueError(f"The authorized total API cap must be from 1 to {MAX_API_CALLS}")
    output = Path(output).resolve()
    if output.suffix.lower() != ".json" or output.is_relative_to(profile.source):
        raise ValueError("Report must be JSON outside the source Runtime")
    generated = {output, output.with_suffix(".md"), output.with_suffix(".api-budget.json"), output.with_suffix(".receipts")}
    if generated & {Path(data_path).resolve(), Path(answers_path).resolve()}:
        raise ValueError("Report must not overwrite input data")
    cases = {case["id"]: case for case in load_cases(data_path, answers_path)}
    if not set(case_ids) <= cases.keys():
        raise ValueError("Selected cases are missing")
    profile.validate()
    output.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(output.with_suffix(".campaign.lock"), timeout=.1):
        if (output.exists() or output.with_suffix(".md").exists() or output.with_suffix(".evidence").exists()
                or output.with_suffix(".receipts").exists()):
            raise ValueError("Existing campaign output cannot be replayed")
        public = profile.public_summary()
        binding = hashlib.sha256(json.dumps({"profile": public, "case_ids": case_ids,
                  "data": hashlib.sha256(Path(data_path).read_bytes()).hexdigest(),
                  "answers": hashlib.sha256(Path(answers_path).read_bytes()).hexdigest()}, sort_keys=True).encode()).hexdigest()
        budget = HTTPCallBudget(output.with_suffix(".api-budget.json"), maximum=max_api_calls, binding=binding)
        test_only = _test_transport_factory is not None
        report = {"schema_version": 1, "benchmark": "BFCL local structured-call subset", "mode": "test_transport" if test_only else "live",
                  "official_score": False, "model_quality": "unsupported" if test_only else "evaluated",
                  "paid_calls_authorized": not test_only, "profile": public,
                  "planned_cases": len(case_ids), "dataset_binding": binding, "results": [], "totals": {},
                  "api_call_limit": max_api_calls, "metadata_requests": 0}
        await _persist_report(output, report)
        fatal_failure = None
        with tempfile.TemporaryDirectory(prefix="aster-bfcl-current-") as scratch:
            for case_id in case_ids:
                if fatal_failure is not None:
                    result = {"id": case_id, "result": "not_run", "model_quality": "unsupported",
                              "reason": "fatal_provider_failure", "runtime_status": "not_started"}
                elif len(budget.snapshot()["calls"]) >= max_api_calls:
                    result = {"id": case_id, "result": "not_run", "model_quality": "unsupported",
                              "reason": "total_api_budget_exhausted", "runtime_status": "not_started"}
                else:
                    clients, models, failures = [], [], []

                    def factory(settings, *, current_case=case_id, current_failures=failures,
                                current_clients=clients, current_models=models):
                        try:
                            profile.validate()
                        except Exception as error:
                            current_failures.append({"type": type(error).__name__, "category": getattr(error, "category", "endpoint"),
                                                     "status_code": getattr(error, "status_code", None)})
                            raise

                        async def before(request):
                            await budget.before_request(request, profile=profile, case_id=current_case)

                        transport = (_test_transport_factory() if test_only else
                                     (None if profile.protocol == "deepseek_responses" else PublicProviderTransport(profile.base_url)))
                        client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=20), follow_redirects=False,
                                                   trust_env=profile.protocol == "deepseek_responses", transport=transport,
                                                   event_hooks={"request": [before], "response": [budget.after_response]})
                        current_clients.append(client)
                        common = {"model": profile.model, "base_url": profile.base_url, "effort": profile.reasoning_effort,
                                  "max_output_tokens": min(max_output_tokens, profile.capabilities.get("max_output_tokens") or max_output_tokens),
                                  "max_retries": 0, "timeout_seconds": 120, "client": client,
                                  "artifact_root": Path(settings["cwd"]).parent / "runtime/artifacts"}
                        inner = (DeepSeekResponsesClient(profile.key, **common) if profile.protocol == "deepseek_responses"
                                 else CompatibleClient(profile.key, protocol=profile.protocol, **common))
                        caps = profile.capabilities
                        inner.capabilities = {profile.model: ModelCapabilities(profile.model, caps.get("context_window"),
                            caps.get("max_output_tokens"), tuple(caps.get("input_modalities", ["text"])),
                            tuple(caps.get("effort_levels", [])), "captured-no-metadata-request", {})}
                        identity = {"provider_id": profile.provider_id, "model": profile.model,
                                    "protocol": profile.protocol, "revision": profile.identity_revision}
                        observed = _ObservedModel(ScopedModel(inner, identity, profile.validate), current_failures)
                        current_models.append(observed)
                        return observed

                    try:
                        result = await _run_case(cases[case_id], scratch, output.with_suffix(".evidence"),
                            mode="offline" if test_only else "live", factory=factory, credentials=_NoCredentials(), budget=None,
                            settings={"model": profile.model, "reasoning_effort": profile.reasoning_effort,
                                      "max_output_tokens": max_output_tokens})
                    except Exception as error:  # noqa: BLE001 - sanitized per-case evidence.
                        result = {"id": case_id, "result": "fail", "model_quality": "unsupported",
                                  "runtime_status": "case_error", "error_type": type(error).__name__}
                    finally:
                        for model in models:
                            await model.aclose()
                        for client in clients:
                            await client.aclose()
                    if failures:
                        result["model_failures"] = failures
                        if any(item["category"] in {"authentication", "endpoint"} for item in failures):
                            fatal_failure = failures[-1]
                    api_rows = [row for row in budget.snapshot()["calls"] if row["case_id"] == case_id]
                    result["api_attempts"] = len(api_rows)
                    result["api_status_codes"] = [row["status_code"] for row in api_rows if "status_code" in row]
                    if any(row.get("status_code") in {401, 402, 403} for row in api_rows):
                        fatal_failure = {"category": "authentication_or_balance"}
                report["results"].append(result)
                report["totals"] = dict(Counter(row["result"] for row in report["results"]))
                report["api_calls_reserved"] = len(budget.snapshot()["calls"])
                _persist_result_receipt(output, report, result)
                await _persist_report(output, report)
        report["api_calls_reserved"] = len(budget.snapshot()["calls"])
        await _persist_report(output, report)
        return report
