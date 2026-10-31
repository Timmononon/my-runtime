"""Explicit remote or offline local embeddings over authorized effective facts."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from pathlib import Path

import httpx

from aster.memory.facts import _json, _time
from aster.providers.deepseek import ProviderError, _cancellable
from aster.providers.profiles import PublicProviderTransport, endpoint_url
from aster.state import StateError

DEFAULTS = {"enabled": False, "backend": "remote", "provider_id": None, "model": None,
            "model_path": None, "device": "auto", "local_max_tokens": 1024, "timeout_seconds": 30,
            "batch_size": 32, "rank_limit": 200, "scan_limit": 2000, "input_chars": 8000}


def validate_embedding_config(value, providers=None):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("Embedding settings require only the documented typed fields")
    config = {**DEFAULTS, **value}
    if type(config["enabled"]) is not bool:
        raise ValueError("Embedding enabled must be a boolean")
    if not isinstance(config["backend"], str) or config["backend"] not in {"remote", "local"}:
        raise ValueError("Embedding backend must be remote or local")
    if not isinstance(config["device"], str) or config["device"] not in {"auto", "cpu", "cuda"}:
        raise ValueError("Local embedding device must be auto, cpu or cuda")
    path = config["model_path"]
    if path is not None and (not isinstance(path, str) or not path.strip() or len(path) > 4096
                            or any(ord(char) < 32 for char in path) or not Path(path).is_absolute()
                            or path.startswith(("\\\\", "//"))):
        raise ValueError("Local embedding model_path must be an absolute local directory")
    for field in ("provider_id", "model"):
        item = config[field]
        if item is not None and (not isinstance(item, str) or not item.strip() or len(item) > 200 or any(c.isspace() for c in item)):
            raise ValueError("Embedding provider and model require explicit bounded identifiers")
    for field, minimum, maximum in (("batch_size", 1, 128), ("rank_limit", 1, 200),
                                    ("scan_limit", 1, 10000), ("input_chars", 100, 32000),
                                    ("local_max_tokens", 32, 8192)):
        if type(config[field]) is not int or not minimum <= config[field] <= maximum:
            raise ValueError(f"Embedding {field} must be between {minimum} and {maximum}")
    timeout = config["timeout_seconds"]
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 1 <= timeout <= 300:
        raise ValueError("Embedding timeout_seconds must be between 1 and 300")
    if config["enabled"] and config["backend"] == "local" and not path:
        raise ValueError("Enabled local embeddings require an explicit model_path")
    if config["enabled"] and config["backend"] == "remote" and not all(config[field] for field in ("provider_id", "model")):
        raise ValueError("Enabled embeddings require an explicit provider_id and model")
    if config["backend"] == "remote" and config["provider_id"] and providers is not None:
        try:
            profile = providers.get(config["provider_id"])
        except KeyError:
            raise ValueError("Embedding provider does not exist") from None
        if profile["protocol"] not in {"openai_chat", "openai_responses"}:
            raise ValueError("Embeddings require an explicitly configured OpenAI compatible provider")
        endpoint_url(profile["base_url"], profile["protocol"])
    return config


def embedding_status(value, providers, local_manager=None):
    config = validate_embedding_config(value)
    if config["backend"] == "local":
        from aster.memory.local_embedding import validate_local_config
        readiness = validate_local_config(config)
        configured, available = bool(readiness["configured"]), bool(readiness["available"])
        status = "disabled" if not config["enabled"] else "ready" if available else "unconfigured"
        local = (local_manager.status(config) if local_manager is not None else
                 {"state": "unloaded", "device": None, "error_code": None,
                  "dependencies_available": readiness.get("dependencies_available", False)})
        return {"backend": "local", "enabled": config["enabled"], "configured": configured,
                "available": config["enabled"] and available, "status": status, "settings": config,
                "reason": "Semantic retrieval is disabled" if not config["enabled"] else readiness["reason"],
                "local": local}
    try:
        profile = providers.get(config["provider_id"]) if config["provider_id"] else None
        if profile and profile["protocol"] not in {"openai_chat", "openai_responses"}:
            profile = None
    except KeyError:
        profile = None
    configured = bool(config["model"] and profile and profile["configured"])
    status = "disabled" if not config["enabled"] else "ready" if configured else "unconfigured"
    return {"backend": "remote", "enabled": config["enabled"], "configured": configured, "available": status == "ready",
            "status": status, "settings": config,
            "reason": "Configuration readiness only; embedding quality and provider support are not verified" if status == "ready" else
                      "Semantic retrieval is disabled" if status == "disabled" else "Embedding provider credentials are not configured"}


def _fingerprint(fact):
    return hashlib.sha256(_json(fact).encode()).hexdigest()


def _text(fact):
    return f"Entity: {fact['entity']}\nRelation: {fact['relation']}\nValue: {_json(fact['value'])}\nEntity type: {fact.get('entity_type')}\nTarget type: {fact.get('target_type')}"


class SemanticMemory:
    def __init__(self, memory, providers, config, *, local_manager=None, config_loader=None):
        self.memory, self.providers = memory, providers
        self.config = validate_embedding_config(config)
        self.local_manager, self.config_loader = local_manager, config_loader
        with memory.store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS memory_embeddings(
                fact_id TEXT NOT NULL,identity_sha TEXT NOT NULL,content_sha TEXT NOT NULL,
                vector_json TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(fact_id,identity_sha))""")

    def status(self):
        return embedding_status(self.config, self.providers, self.local_manager)

    def _validate_config(self):
        if self.config_loader is not None:
            try:
                current = validate_embedding_config(self.config_loader())
            except (ValueError, TypeError, KeyError, OSError):
                raise ProviderError("Embedding configuration changed; start a new query", category="configuration_changed") from None
            if current != self.config:
                raise ProviderError("Embedding configuration changed; start a new query", category="configuration_changed")

    def _validate_pin(self, pin):
        self._validate_config()
        if self.config["backend"] == "local":
            self.local_manager.validate_pin(pin)
        else:
            self.providers.validate_pin(pin)

    def _pin(self):
        self._validate_config()
        if not self.status()["available"]:
            raise ProviderError("Semantic retrieval is disabled or unconfigured; configure an embedding model explicitly", category="unavailable")
        if self.config["backend"] == "local":
            if self.local_manager is None:
                raise ProviderError("Local embedding worker is not available", category="unavailable")
            pin = self.local_manager.pin(self.config)
            identity = hashlib.sha256(_json({"backend": "local", "model": pin["identity_sha"],
                                            "input_chars": self.config["input_chars"]}).encode()).hexdigest()
            return pin, identity
        profile = self.providers.get(self.config["provider_id"])
        row = self.providers._row(profile["id"])
        key = self.providers._key(row)
        pin = {"id": profile["id"], "revision": profile.get("identity_revision", 1),
               "base_url": profile["base_url"], "protocol": profile["protocol"], "model": self.config["model"],
               "credential_digest": hashlib.sha256(key.encode()).hexdigest() if key else None}
        identity = hashlib.sha256(_json({**pin, "input_chars": self.config["input_chars"]}).encode()).hexdigest()
        return pin, identity

    async def _embed(self, texts, pin, *, cancel=None):
        key = self.providers.resolve_pin(pin)
        client = (self.providers.client_factory() if self.providers.client_factory else
                  httpx.AsyncClient(timeout=self.config["timeout_seconds"], follow_redirects=False, trust_env=False,
                                    transport=PublicProviderTransport(pin["base_url"])))
        try:
            async with client.stream("POST", pin["base_url"] + "/embeddings",
                                     headers={"Authorization": "Bearer " + key},
                                     json={"model": pin["model"], "input": texts, "encoding_format": "float"},
                                     follow_redirects=False) as response:
                if response.status_code != 200:
                    raise ProviderError(f"Embedding provider returned HTTP {response.status_code}", category="embedding")
                received = bytearray()
                async for chunk in response.aiter_bytes():
                    received.extend(chunk)
                    if len(received) > 16 * 1024 * 1024:
                        raise ProviderError("Embedding response exceeds the receive budget; reduce batch_size", category="budget")
            try:
                data = json.loads(received)["data"]
                if not isinstance(data, list) or len(data) != len(texts):
                    raise ValueError()
                vectors = {}
                dimensions = set()
                for item in data:
                    index, vector = item["index"], item["embedding"]
                    if (type(index) is not int or index not in range(len(texts)) or index in vectors or
                            not isinstance(vector, list) or not 1 <= len(vector) <= 65536 or
                            any(type(value) not in {int, float} or not math.isfinite(value) for value in vector)):
                        raise ValueError()
                    norm = math.sqrt(sum(value * value for value in vector))
                    if not math.isfinite(norm) or norm == 0:
                        raise ValueError()
                    vectors[index] = [value / norm for value in vector]
                    dimensions.add(len(vector))
                if len(dimensions) != 1:
                    raise ValueError()
                return [vectors[index] for index in range(len(texts))]
            except (ValueError, KeyError, TypeError, OverflowError):
                raise ProviderError("Embedding provider returned an invalid vector schema", category="protocol") from None
        except httpx.TransportError:
            raise ProviderError("Embedding transport failed; the request is not automatically replayed", category="transport") from None
        finally:
            await client.aclose()

    async def _request(self, texts, pin, cancel):
        try:
            if cancel is not None and cancel.is_set():
                raise asyncio.CancelledError()
            self._validate_pin(pin)
            async with asyncio.timeout(self.config["timeout_seconds"]):
                if self.config["backend"] == "local":
                    result = await self.local_manager.embed(texts, pin, cancel=cancel,
                        timeout_seconds=self.config["timeout_seconds"], batch_size=self.config["batch_size"])
                    if cancel is not None and cancel.is_set():
                        raise asyncio.CancelledError()
                    self._validate_pin(pin)
                    vectors, clipped = result["vectors"], result.get("truncated_indices", [])
                    if (not isinstance(vectors, list) or len(vectors) != len(texts)
                            or any(type(index) is not int or index not in range(len(texts)) for index in clipped)):
                        raise ProviderError("Local embedding worker returned invalid vectors", category="protocol")
                    dimensions = set()
                    for vector in vectors:
                        if (not isinstance(vector, list) or not 1 <= len(vector) <= 65536
                                or any(type(value) not in {int, float} or not math.isfinite(value) for value in vector)):
                            raise ProviderError("Local embedding worker returned invalid vectors", category="protocol")
                        norm = math.sqrt(sum(value * value for value in vector))
                        if not math.isfinite(norm) or norm == 0:
                            raise ProviderError("Local embedding worker returned invalid vectors", category="protocol")
                        vector[:] = [value / norm for value in vector]
                        dimensions.add(len(vector))
                    if len(dimensions) > 1:
                        raise ProviderError("Local embedding worker returned inconsistent dimensions", category="protocol")
                    return vectors, set(clipped)
                vectors = await _cancellable(self._embed(texts, pin), cancel)
                if cancel is not None and cancel.is_set():
                    raise asyncio.CancelledError()
                self._validate_pin(pin)
                return vectors, set()
        except TimeoutError:
            raise ProviderError("Embedding request exceeded its configured time budget; no automatic replay", category="timeout") from None
        except ProviderError:
            if self.config["backend"] == "local":
                # A settings change can deliberately stop the old worker while
                # its request is waiting; report that boundary, not a model fault.
                self._validate_config()
            raise
        except StateError:
            raise ProviderError("Embedding provider identity changed; start a new explicitly approved query", category="configuration_changed") from None

    async def recall(self, *, query, scopes=None, at=None, limit=None, cancel=None):
        if not isinstance(query, str) or not query.strip() or len(query) > self.config["input_chars"]:
            raise ValueError("Semantic query requires nonempty text within configured input_chars")
        limit = min(50, self.config["rank_limit"]) if limit is None else limit
        if type(limit) is not int or not 1 <= limit <= self.config["rank_limit"]:
            raise ValueError("Semantic limit exceeds the configured rank_limit")
        pin, identity = self._pin()
        source = self.memory.candidates(scopes=scopes, at=at, scan_limit=self.config["scan_limit"])
        facts = source["facts"]
        vectors, missing, clipped = {}, [], set()
        with self.memory.store.transaction() as db:
            db.execute("DELETE FROM memory_embeddings WHERE fact_id NOT IN (SELECT id FROM memory_facts WHERE status!='retracted')")
            for fact in facts:
                if len(_text(fact)) > self.config["input_chars"]:
                    clipped.add(fact["id"])
                row = db.execute("SELECT * FROM memory_embeddings WHERE fact_id=? AND identity_sha=?",
                                 (fact["id"], identity)).fetchone()
                if row and row["content_sha"] == _fingerprint(fact):
                    cached = json.loads(row["vector_json"])
                    if self.config["backend"] == "local":
                        vectors[fact["id"]] = cached["values"]
                        if cached.get("truncated"):
                            clipped.add(fact["id"])
                    else:
                        vectors[fact["id"]] = cached
                else:
                    missing.append(fact)
        for offset in range(0, len(missing), self.config["batch_size"]):
            batch = missing[offset:offset + self.config["batch_size"]]
            embedded, token_clipped = await self._request([_text(fact)[:self.config["input_chars"]] for fact in batch], pin, cancel)
            clipped.update(batch[index]["id"] for index in token_clipped)
            # Recheck versions after network awaits; withdrawn/stale facts cannot
            # re-enter an index or results through a late embedding response.
            current = {fact["id"]: fact for fact in self.memory.candidates(scopes=scopes, at=at, scan_limit=self.config["scan_limit"])["facts"]}
            with self.memory.store.transaction() as db:
                for fact, vector in zip(batch, embedded, strict=True):
                    if fact["id"] not in current or _fingerprint(current[fact["id"]]) != _fingerprint(fact):
                        continue
                    vectors[fact["id"]] = vector
                    stored = {"values": vector, "truncated": fact["id"] in clipped} if self.config["backend"] == "local" else vector
                    db.execute("INSERT OR REPLACE INTO memory_embeddings VALUES(?,?,?,?,?)",
                               (fact["id"], identity, _fingerprint(fact), _json(stored), _time()))
        query_truncated = False
        if not facts:
            query_vector = None
        else:
            query_vectors, query_clipped = await self._request([query], pin, cancel)
            query_vector, query_truncated = query_vectors[0], bool(query_clipped)
        # The query await may outlive a fact edit or backend change; validate
        # the original pin and re-read scoped candidates before ranking.
        self._validate_pin(pin)
        current_source = self.memory.candidates(scopes=scopes, at=at, scan_limit=self.config["scan_limit"])
        current = {fact["id"]: fact for fact in current_source["facts"]}
        state_changed = ({fact["id"]: _fingerprint(fact) for fact in facts} !=
                         {fact_id: _fingerprint(fact) for fact_id, fact in current.items()})
        ranked = []
        for fact in facts:
            vector = vectors.get(fact["id"])
            if vector is None or fact["id"] not in current or _fingerprint(current[fact["id"]]) != _fingerprint(fact):
                continue
            if query_vector is None or len(vector) != len(query_vector):
                raise ProviderError("Embedding dimensions changed; configure a stable model identity", category="protocol")
            ranked.append({**current[fact["id"]], "score": sum(a * b for a, b in zip(vector, query_vector, strict=True))})
        ranked.sort(key=lambda fact: (-fact["score"], fact["id"]))
        return {"facts": ranked[:limit], "conflicts": current_source["conflicts"], "at": current_source["at"],
                "no_data": not ranked, "retrieval": "local_embedding_cosine" if self.config["backend"] == "local" else "configured_embedding_cosine",
                "truncated": source["truncated"] or current_source["truncated"] or len(ranked) > limit or bool(clipped) or query_truncated or len(ranked) < len(current),
                "scan_truncated": source["truncated"] or current_source["truncated"],
                "state_changed": state_changed,
                "indexed_count": len(ranked), "available_candidate_count": len(current),
                "input_truncated_fact_ids": sorted(fact_id for fact_id in clipped if fact_id in current),
                "query_truncated": query_truncated, "scan_limit": self.config["scan_limit"],
                "embedding": ({"backend": "local", "model_path": pin["model_path"],
                               "local_max_tokens": self.config["local_max_tokens"],
                               "device": self.local_manager.status(self.config).get("device")}
                              if self.config["backend"] == "local" else
                              {"provider_id": pin["id"], "model": pin["model"]})}
