"""Operator-managed provider identities. SQLite holds metadata, never key values."""
from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import os
import time
import uuid
from urllib.parse import urlsplit

import httpx

from aster.customization.credentials import CredentialError
from aster.providers.deepseek import ModelCapabilities, ProviderError, native_effort_levels
from aster.state import StateError
from aster.state.store import dumps

PROTOCOLS = [
    {"id": "deepseek_responses", "label": "DeepSeek Responses", "supported": True},
    {"id": "openai_responses", "label": "OpenAI compatible Responses", "supported": True},
    {"id": "openai_chat", "label": "OpenAI compatible Chat Completions", "supported": True},
]
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
DEEPSEEK_ORIGIN = "https://api.deepseek.com"


def native_deepseek_client(timeout: float):
    """Fixed official DeepSeek operations share the native chat's proxy/TUN path."""
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=min(20, timeout)),
                             follow_redirects=False, trust_env=True)


def endpoint_url(value: str, protocol: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.port not in {None, 443}
            or "\\" in value or any(c.isspace() for c in value)):
        raise ValueError("Provider address must be HTTPS on port 443 without credentials, query or fragment")
    if protocol not in {p["id"] for p in PROTOCOLS}:
        raise ValueError("Unsupported provider protocol")
    host = parsed.hostname.encode("idna").decode().lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Provider address must be a public host")
    try:
        if not ipaddress.ip_address(host).is_global:
            raise ValueError("Provider address must be public")
    except ValueError:
        # A syntactically valid IP must not be mistaken for a hostname.
        if host.replace(".", "").isdigit() or ":" in host:
            raise ValueError("Provider address must be a public hostname") from None
    path = parsed.path.rstrip("/")
    if any(part in {".", ".."} for part in path.split("/")) or "%" in path:
        raise ValueError("Provider API path must not contain escaped or relative segments")
    target = f"https://{'[' + host + ']' if ':' in host else host}{path}"
    if protocol == "deepseek_responses" and target != "https://api.deepseek.com":
        raise ValueError("DeepSeek protocol is pinned to https://api.deepseek.com")
    return target


async def verify_public_endpoint(value: str, *, dns_fallback="off", dns_client_factory=None):
    """Verify and pin public HTTPS; only proxy fake-IP DNS may use fixed DoH."""
    from aster.tools.http import PublicDNSFailure, pin_url

    try:
        target = endpoint_url(value, "openai_chat")
        host = urlsplit(target).hostname
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if "." not in host or host.endswith((".home.arpa", ".test", ".invalid")):
                raise ValueError() from None
        pinned, _, _ = await pin_url(target, set(), dns_fallback=dns_fallback,
                                     dns_client_factory=dns_client_factory)
        return pinned.host
    except (OSError, ValueError, TimeoutError, PublicDNSFailure):
        raise ProviderError("Provider DNS address is unavailable or non-public", category="endpoint") from None


class PublicProviderTransport(httpx.AsyncBaseTransport):
    """Resolve once, validate, and connect to that IP with original TLS SNI.

    This avoids a second DNS lookup changing a public check into a private
    connection. HTTP redirects remain responses; no credential forwarding.
    """
    def __init__(self, base_url, *, dns_fallback="off", dns_client_factory=None):
        self.base = httpx.URL(base_url)
        self.dns_fallback, self.dns_client_factory = dns_fallback, dns_client_factory
        self.inner = httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        if request.url.scheme != "https" or request.url.host != self.base.host or request.url.port != self.base.port:
            raise ProviderError("Provider request origin differs from its saved profile", category="endpoint")
        address = await verify_public_endpoint(str(self.base), dns_fallback=self.dns_fallback,
                                               dns_client_factory=self.dns_client_factory)
        headers = request.headers.copy()
        headers["Host"] = self.base.netloc.decode()
        pinned = httpx.Request(request.method, request.url.copy_with(host=address), headers=headers,
                               stream=request.stream, extensions={**request.extensions, "sni_hostname": self.base.host})
        return await self.inner.handle_async_request(pinned)

    async def aclose(self):
        await self.inner.aclose()


def models_metadata(rows: list[dict], *, protocol=None) -> list[dict]:
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError("Model catalogue must contain at most 1000 entries")
    output, seen = [], set()
    for row in rows:
        mid = row.get("id") if isinstance(row, dict) else None
        if not isinstance(mid, str) or not mid.strip() or len(mid) > 200 or any(c.isspace() for c in mid):
            raise ValueError("Model IDs must be non-empty identifiers")
        if mid in seen:
            raise ValueError("Duplicate model ID")
        seen.add(mid)
        entry = {"id": mid}
        if row.get("name") is not None:
            if not isinstance(row["name"], str) or len(row["name"]) > 200:
                raise ValueError("Model display names must be strings of at most 200 characters")
            entry["name"] = row["name"]
        for field in ("context_window", "max_output_tokens"):
            value = row.get(field)
            if value is not None and (type(value) is not int or not 1 <= value <= 10_000_000):
                raise ValueError("Model token capabilities must be positive bounded integers")
            entry[field] = value
        modalities = row.get("input_modalities", ["text"])
        efforts = row.get("effort_levels", (row.get("effort") or {}).get("supported_levels", []))
        if not isinstance(modalities, list) or not modalities or set(modalities) - {"text", "image"}:
            raise ValueError("Only text and image input modalities are supported")
        if not isinstance(efforts, list) or any(value not in EFFORTS for value in efforts):
            raise ValueError("Unsupported reasoning effort capability")
        if protocol == "deepseek_responses":
            efforts = list(native_effort_levels(efforts))
        entry.update(input_modalities=list(dict.fromkeys(modalities)), effort_levels=list(dict.fromkeys(efforts)))
        output.append(entry)
    return output


def validate_protocol_models(protocol, models):
    allowed = {"none", "low", "high", "max"} if protocol == "deepseek_responses" else EFFORTS - {"max"}
    if any(set(model["effort_levels"]) - allowed for model in models):
        raise ValueError("Model reasoning levels are not supported by this protocol")


class ProviderProfiles:
    def __init__(self, store, credentials, *, client_factory=None, dns_fallback="off"):
        self.store, self.credentials = store, credentials
        self.client_factory = client_factory  # Explicit offline test injection only.
        # A trusted global reader is evaluated only for an explicit catalogue request.
        self.dns_fallback = dns_fallback
        with store.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS provider_profiles (id TEXT PRIMARY KEY, document TEXT NOT NULL, credential_ref TEXT, revision INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0)")
            db.execute("CREATE TABLE IF NOT EXISTS session_model_selection (session_id TEXT PRIMARY KEY REFERENCES sessions(id), document TEXT NOT NULL)")
            preset = {"name": "DeepSeek", "base_url": "https://api.deepseek.com", "protocol": "deepseek_responses", "models": [
                {"id": "deepseek-flash", "context_window": None, "max_output_tokens": None,
                 "input_modalities": ["text", "image"], "effort_levels": ["none", "low", "high", "max"]}], "builtin": True, "identity_revision": 1}
            db.execute("INSERT OR IGNORE INTO provider_profiles(id,document,credential_ref,revision) VALUES('deepseek',?,'legacy:deepseek',1)", (dumps(preset),))

    def _row(self, pid):
        row = self.store.connection.execute("SELECT * FROM provider_profiles WHERE id=? AND deleted=0", (pid,)).fetchone()
        if row is None:
            raise KeyError(pid)
        return dict(row)

    def _key(self, row):
        ref = row["credential_ref"]
        if not ref:
            return None
        if ref == "legacy:deepseek":
            return self.credentials.get()
        return self.credentials.get(ref, use_environment=False)

    def public(self, row):
        data = json.loads(row["document"])
        try:
            configured = bool(self._key(row))
        except CredentialError:
            configured = False
        source = "unconfigured"
        if configured:
            source = ("environment" if row["credential_ref"] == "legacy:deepseek" and os.environ.get("DEEPSEEK_API_KEY")
                      else getattr(self.credentials, "storage", "vault"))
        return {**data, "id": row["id"], "revision": row["revision"], "configured": configured, "credential_source": source}

    def list(self):
        return [self.public(dict(row)) for row in self.store.connection.execute("SELECT * FROM provider_profiles WHERE deleted=0 ORDER BY id")]

    def get(self, pid):
        return self.public(self._row(pid))

    def _new_key(self, key):
        if not isinstance(key, str) or not key.strip() or len(key) > 8192 or any(c.isspace() for c in key):
            raise ValueError("API key must be a non-empty single-line value without whitespace")
        # Unique slots make set-before-SQL crash safe: no old binding is overwritten.
        ref = "provider-" + uuid.uuid4().hex
        self.credentials.set(ref, key)
        return ref

    def create(self, *, name, base_url, protocol, models=None, api_key=None):
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError("Provider name is required and must be at most 100 characters")
        data = {"name": name.strip(), "base_url": endpoint_url(base_url, protocol), "protocol": protocol,
                "models": models_metadata(models or [], protocol=protocol), "builtin": False, "identity_revision": 1}
        validate_protocol_models(protocol, data["models"])
        if api_key is not None and api_key in dumps(data):
            raise ValueError("Do not include an API key in provider metadata")
        ref = self._new_key(api_key) if api_key is not None else None
        pid = "provider-" + uuid.uuid4().hex
        with self.store.transaction() as db:
            db.execute("INSERT INTO provider_profiles(id,document,credential_ref,revision) VALUES(?,?,?,1)", (pid, dumps(data), ref))
        return self.get(pid)

    def _revision(self, row, expected):
        if row["revision"] != expected:
            raise StateError("Provider profile changed; reload its current revision")

    def update(self, pid, *, expected_revision, **changes):
        row = self._row(pid)
        self._revision(row, expected_revision)
        data = json.loads(row["document"])
        new = {**data, **{k: v for k, v in changes.items() if k in {"name", "base_url", "protocol", "models"}}}
        if not isinstance(new["name"], str) or not new["name"].strip() or len(new["name"]) > 100:
            raise ValueError("Provider name is required and must be at most 100 characters")
        new["base_url"] = endpoint_url(new["base_url"], new["protocol"])
        new["models"] = models_metadata(new["models"], protocol=new["protocol"])
        identity_changed = any(new[field] != data[field] for field in ("base_url", "protocol"))
        if data.get("builtin") and identity_changed:
            raise ValueError("The built-in DeepSeek identity is fixed; add a separate provider")
        ref = row["credential_ref"]
        if identity_changed:
            ref = None
            if "models" not in changes:
                new["models"] = []
        validate_protocol_models(new["protocol"], new["models"])
        current_key = self._key(row)
        if any(value and value in dumps(new) for value in (current_key, changes.get("api_key"))):
            raise ValueError("Do not include an API key in provider metadata")
        if "api_key" in changes:
            ref = self._new_key(changes["api_key"])
        if identity_changed or ref != row["credential_ref"]:
            new["identity_revision"] = data.get("identity_revision", 1) + 1
        with self.store.transaction() as db:
            self._revision(self._row(pid), expected_revision)
            db.execute("UPDATE provider_profiles SET document=?,credential_ref=?,revision=revision+1 WHERE id=?", (dumps(new), ref, pid))
        self._retire(row["credential_ref"], ref)
        return self.get(pid)

    def _retire(self, previous, current=None):
        if previous and previous != current and previous != "legacy:deepseek":
            self.credentials.delete(previous)

    def delete_key(self, pid, expected_revision):
        row = self._row(pid)
        self._revision(row, expected_revision)
        data = json.loads(row["document"])
        data["identity_revision"] = data.get("identity_revision", 1) + 1
        with self.store.transaction() as db:
            db.execute("UPDATE provider_profiles SET document=?,credential_ref=NULL,revision=revision+1 WHERE id=?", (dumps(data), pid))
        self._retire(row["credential_ref"])
        return self.get(pid)

    def delete(self, pid, expected_revision):
        row = self._row(pid)
        self._revision(row, expected_revision)
        if json.loads(row["document"]).get("builtin"):
            raise StateError("The built-in provider can be disconnected but cannot be deleted")
        with self.store.transaction() as db:
            db.execute("UPDATE provider_profiles SET deleted=1,credential_ref=NULL,revision=revision+1 WHERE id=?", (pid,))
        self._retire(row["credential_ref"])
        return {"deleted": True, "id": pid}

    def pin(self, pid, model, effort, *, allow_legacy_model=False):
        profile = self.get(pid)
        selected = next((row for row in profile["models"] if row["id"] == model), None)
        if selected is None and allow_legacy_model and pid == "deepseek" and isinstance(model, str) and model.strip():
            # Existing YAML model choices still get verified against official
            # DeepSeek /models before generation. This is not a UI catalogue grant.
            selected = {"id": model, "context_window": None, "max_output_tokens": None,
                        "input_modalities": ["text"], "effort_levels": ["none", "low", "high", "max"]}
        if selected is None:
            raise ValueError("Selected model is absent; fetch models or add explicit model metadata")
        if effort is not None and effort not in selected["effort_levels"]:
            raise ValueError("Selected model does not advertise this reasoning effort")
        key = self._key(self._row(pid))
        return {"id": pid, "revision": profile.get("identity_revision", 1), "base_url": profile["base_url"],
                "protocol": profile["protocol"], "model": model, "reasoning_effort": effort, "capabilities": selected,
                "credential_digest": hashlib.sha256(key.encode()).hexdigest() if key else None}

    @staticmethod
    def _validate_pin_identity(pin, row):
        data = json.loads(row["document"])
        if data.get("identity_revision", 1) != pin["revision"]:
            raise StateError("Provider credential or endpoint changed since this run was captured")
        if any(pin[key] != data[key] for key in ("base_url", "protocol")):
            raise StateError("Provider identity no longer matches the captured run")

    @staticmethod
    def _validate_pin_credential(pin, key):
        # Old immutable run snapshots predate this field and remain compatible.
        # New bindings also cover edits made directly in credentials.yaml.
        digest = hashlib.sha256(key.encode()).hexdigest() if key else None
        if "credential_digest" in pin and pin["credential_digest"] != digest:
            raise StateError("Provider credential or endpoint changed since this run was captured")

    def validate_pin(self, pin):
        row = self._row(pin["id"])
        self._validate_pin_identity(pin, row)
        self._validate_pin_credential(pin, self._key(row))
        return row

    def resolve_pin(self, pin):
        row = self._row(pin["id"])
        self._validate_pin_identity(pin, row)
        key = self._key(row)
        self._validate_pin_credential(pin, key)
        if not key:
            raise ProviderError("Selected provider has no configured key", category="authentication")
        return key

    def selection(self, session_id, defaults):
        self.store.get_session(session_id)
        row = self.store.connection.execute("SELECT document FROM session_model_selection WHERE session_id=?", (session_id,)).fetchone()
        return json.loads(row[0]) if row else copy.deepcopy(defaults)

    def save_selection(self, session_id, selection):
        with self.store.transaction() as db:
            self.store._require_session_writable(db, session_id)
            db.execute("INSERT INTO session_model_selection VALUES(?,?) ON CONFLICT(session_id) DO UPDATE SET document=excluded.document", (session_id, dumps(selection)))

    async def fetch_models(self, pid, expected_revision):
        row = self._row(pid)
        self._revision(row, expected_revision)
        data = json.loads(row["document"])
        key = self._key(row)
        if not key:
            raise ProviderError("Selected provider has no configured key", category="authentication")
        if self.client_factory:
            client = self.client_factory()
        elif data["protocol"] == "deepseek_responses" and data["base_url"] == DEEPSEEK_ORIGIN:
            client = native_deepseek_client(20)
        else:
            dns_fallback = self.dns_fallback() if callable(self.dns_fallback) else self.dns_fallback
            client = httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False,
                                       transport=PublicProviderTransport(data["base_url"], dns_fallback=dns_fallback))
        try:
            async with client.stream("GET", data["base_url"] + "/models", headers={"Authorization": "Bearer " + key}, follow_redirects=False) as response:
                if response.status_code != 200:
                    category = {401: "authentication", 403: "authentication", 402: "balance", 429: "rate_limit",
                                500: "server", 502: "server", 503: "overloaded", 504: "server"}.get(
                                    response.status_code, "catalogue")
                    raise ProviderError("Model catalogue request was rejected", category=category,
                                        status_code=response.status_code)
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 2 * 1024 * 1024:
                        raise ProviderError("Model catalogue exceeds receive budget", category="budget")
            try:
                if key in content.decode("utf-8", errors="replace"):
                    raise ProviderError("Provider catalogue contained confidential data", category="protocol")
                raw_models = json.loads(content)["data"]
                models = models_metadata(raw_models, protocol=data["protocol"])
            except (ValueError, KeyError, TypeError) as exc:
                raise ProviderError("Model catalogue has an invalid schema", category="protocol") from exc
            # Preserve explicit known metadata when standard /models only lists IDs.
            previous = {m["id"]: m for m in data["models"]}
            for model, raw_model in zip(models, raw_models, strict=True):
                old = previous.get(model["id"], {})
                for key in ("context_window", "max_output_tokens", "input_modalities", "effort_levels", "name"):
                    present = key in raw_model or key == "effort_levels" and "effort" in raw_model
                    if not present and key in old:
                        model[key] = old[key]
            return self.update(pid, expected_revision=expected_revision, models=models)
        except httpx.TransportError as exc:
            raise ProviderError("Model catalogue transport failed", category="transport") from exc
        finally:
            await client.aclose()


def capability(pin):
    row = pin["capabilities"]
    return ModelCapabilities(pin["model"], row["context_window"], row["max_output_tokens"],
                             tuple(row["input_modalities"]), tuple(row["effort_levels"]), str(time.time()), {})
