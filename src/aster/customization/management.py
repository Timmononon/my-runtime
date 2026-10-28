"""Operator configuration forms with CAS writes and credential-free projections."""
from __future__ import annotations

import copy
import hashlib
import re
import tempfile
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from aster.config import canonical_http_origin, load_config, read_yaml, safe_identifier
from aster.customization.hooks import EVENTS, HookCommand, HookManager
from aster.customization.transactions import commit_agent_pair, recover_management_transactions
from aster.mcp import MCPServerConfig
from aster.platform.filesystem import FileLock, atomic_write
from aster.providers.profiles import endpoint_url
from aster.state import StateError

PERMISSION_KEYS = ("allowed_tools", "denied_tools", "ask_tools", "auto_approve_tools")
GLOBAL_FIELDS = {"mcp_servers", "hooks", "permissions", "embedding", "web_search", "browser"}
REDACTED = "<redacted>"


class ManagementConflict(StateError):
    def __init__(self):
        super().__init__("Configuration changed; reload before saving")


class ManagementValidation(ValueError):
    def __init__(self):
        super().__init__("Management fields are invalid")


class ManagementUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Local management files could not be saved")


def _reference(value):
    return isinstance(value, str) and bool(re.fullmatch(
        r"(?:env:[A-Za-z_][A-Za-z0-9_]*|keyring:[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)", value))


def permissions(value):
    if not isinstance(value, dict) or set(value) - set(PERMISSION_KEYS):
        raise ManagementValidation()
    output = {}
    for key in PERMISSION_KEYS:
        values = value.get(key, None if key == "allowed_tools" else [])
        if key == "allowed_tools" and values is None:
            output[key] = None
            continue
        if not isinstance(values, list) or len(values) > 5000 or any(
            not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", item) for item in values
        ):
            raise ManagementValidation()
        output[key] = sorted(set(values))
    return output


def public_arguments(values):
    output, redact_next = [], False
    for value in values if isinstance(values, (list, tuple)) else []:
        if not isinstance(value, str):
            continue
        sensitive = bool(re.search(r"(?:token|password|passwd|api[-_]?key|secret|authorization)", value, re.IGNORECASE))
        if redact_next:
            output.append(REDACTED)
            redact_next = False
        elif sensitive and value.startswith("-"):
            output.append(value.split("=", 1)[0] + "=" + REDACTED if "=" in value else value)
            redact_next = "=" not in value
        else:
            output.append(value)
    return output


def _restore_arguments(values, previous, *, same_identity):
    if any(isinstance(value, str) and REDACTED in value for value in values):
        if not same_identity or list(values) != public_arguments(previous):
            raise ManagementValidation()
        return list(previous)
    return values


def validate_web_search(value, providers=None):
    # Older disable saves left a null reference alone, with no service or key.
    if not value or value == {"credential_ref": None}:
        return {}
    if isinstance(value, dict) and value.get("backend") == "deepseek":
        if set(value) - {"backend", "provider_id", "model", "timeout_seconds", "result_limit", "max_tokens", "max_uses"}:
            raise ManagementValidation()
        result = {"backend": "deepseek", "provider_id": value.get("provider_id", "deepseek"),
                  "model": value.get("model", "deepseek-flash"), "timeout_seconds": value.get("timeout_seconds", 30),
                  "result_limit": value.get("result_limit", 10), "max_tokens": value.get("max_tokens", 4096),
                  "max_uses": value.get("max_uses", 5)}
        if (result["provider_id"] != "deepseek" or not isinstance(result["model"], str)
                or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", result["model"])
                or type(result["timeout_seconds"]) not in {int, float} or not 1 <= result["timeout_seconds"] <= 300
                or type(result["result_limit"]) is not int or not 1 <= result["result_limit"] <= 50
                or type(result["max_tokens"]) is not int or not 1 <= result["max_tokens"] <= 65536
                or type(result["max_uses"]) is not int or not 1 <= result["max_uses"] <= 1000):
            raise ManagementValidation()
        if providers is not None:
            profile = providers.get("deepseek")
            if (profile["protocol"] != "deepseek_responses" or profile["base_url"] != "https://api.deepseek.com"
                    or not profile["configured"]):
                raise ManagementValidation()
            providers.pin("deepseek", result["model"], None)
        return result
    allowed = {"backend", "base_url", "credential_ref", "timeout_seconds", "result_limit"}
    if not isinstance(value, dict) or set(value) - allowed or value.get("backend") not in {"brave", "searxng"}:
        raise ManagementValidation()
    url = endpoint_url(value.get("base_url", ""), "openai_chat")
    if urlsplit(url).path or (value["backend"] == "brave" and url != "https://api.search.brave.com"):
        raise ManagementValidation()
    reference = value.get("credential_ref")
    if reference is not None and not _reference(reference):
        raise ManagementValidation()
    timeout, limit = value.get("timeout_seconds", 15), value.get("result_limit", 10)
    if type(timeout) not in {int, float} or not 1 <= timeout <= 300 or type(limit) is not int or not 1 <= limit <= 50:
        raise ManagementValidation()
    return {"backend": value["backend"], "base_url": url, "credential_ref": reference,
            "timeout_seconds": timeout, "result_limit": limit}


def validate_browser(value):
    if not isinstance(value, dict) or set(value) - {"mode", "headless", "cdp_endpoint", "allowed_pages"}:
        raise ManagementValidation()
    result = {"mode": value.get("mode", "isolated"), "headless": value.get("headless", True),
              "cdp_endpoint": value.get("cdp_endpoint"), "allowed_pages": value.get("allowed_pages", [])}
    if result["mode"] not in {"isolated", "bridge"} or type(result["headless"]) is not bool:
        raise ManagementValidation()
    endpoint = result["cdp_endpoint"]
    if endpoint is not None:
        parsed = urlsplit(endpoint)
        port = parsed.port
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"} or (port is not None and not 1 <= port <= 65535)):
            raise ManagementValidation()
    pages = result["allowed_pages"]
    if not isinstance(pages, list) or len(pages) > 100:
        raise ManagementValidation()
    for page in pages:
        parsed = urlsplit(page)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ManagementValidation()
    if result["mode"] == "bridge" and (not endpoint or not pages):
        raise ManagementValidation()
    return result


class ManagementService:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.root = coordinator.data_dir
        self.lock = self.root / ".management.lock"

    def _path(self, *parts):
        path = self.root.joinpath(*parts)
        if not path.resolve().is_relative_to(self.root.resolve()) or path.is_symlink():
            raise ManagementValidation()
        return path

    def _bytes(self, path):
        return path.read_bytes() if path.exists() else b""

    def revision(self):
        return hashlib.sha256(self._bytes(self._path("config.yaml"))).hexdigest()

    def _agent_revision(self, agent_id):
        return hashlib.sha256(b"\0".join(self._bytes(path) for path in (
            self._path("config.yaml"), self._path("agents", agent_id, "config.yaml"),
            self._path("agents", agent_id, "AGENT.md")))).hexdigest()

    def _snapshot(self, global_data, *, agent_id="default", agent_data=None):
        # Validate an exact candidate in an isolated tree before committing; a
        # running admission can never observe a transient candidate/rollback.
        with tempfile.TemporaryDirectory(prefix="aster-config-check-") as raw:
            root = Path(raw)
            (root / "config.yaml").write_text(yaml.safe_dump(global_data), encoding="utf-8")
            if agent_data is not None:
                folder = root / "agents" / agent_id
                folder.mkdir(parents=True)
                (folder / "config.yaml").write_text(yaml.safe_dump(agent_data), encoding="utf-8")
            return load_config(root, agent_id=agent_id, cwd=self.root / "workspace").data

    def _selection(self, settings):
        result = self.coordinator.default_selection(settings)
        self.coordinator.providers.pin(result["provider_id"], result["model"], result["reasoning_effort"],
                                       allow_legacy_model=result["provider_id"] == "deepseek")
        self.coordinator.workbench.snapshot_selected_skill(result.get("selected_skill"), settings)
        return result

    def _mcp(self, values, old):
        if not isinstance(values, list) or len(values) > 100:
            raise ManagementValidation()
        previous = {item.get("name"): item for item in old if isinstance(item, dict)}
        result, names = [], set()
        for item in values:
            value = copy.deepcopy(item)
            prior = previous.get(value.get("name"), {})
            value["args"] = _restore_arguments(value.get("args", []), prior.get("args", []),
                same_identity=all(value.get(key) == prior.get(key) for key in ("command", "url", "cwd")))
            for category in ("env", "headers"):
                for name, reference in value.get(category, {}).items():
                    if reference == REDACTED and name in prior.get(category, {}):
                        value[category][name] = prior[category][name]
                    elif not _reference(reference):
                        raise ManagementValidation()
            config = MCPServerConfig(**{**value, "cwd": Path(value["cwd"]) if value.get("cwd") else None})
            if config.name in names:
                raise ManagementValidation()
            names.add(config.name)
            result.append(value)
        return result

    def _hooks(self, values, previous):
        if not isinstance(values, list) or len(values) > 100:
            raise ManagementValidation()
        hooks = []
        for index, value in enumerate(values):
            prior = previous[index] if index < len(previous) else {}
            value = copy.deepcopy(value)
            value["args"] = _restore_arguments(value.get("args", []), prior.get("args", []),
                same_identity=all(value.get(key) == prior.get(key) for key in ("event", "executable")))
            hook = HookCommand(**value)
            if (hook.event not in EVENTS or not hook.executable or not isinstance(hook.executable, str)
                    or not isinstance(hook.args, list) or any(not isinstance(arg, str) for arg in hook.args)
                    or type(hook.timeout_seconds) not in {float, int} or not 0 < hook.timeout_seconds <= 3600
                    or type(hook.output_limit) is not int or not 1 <= hook.output_limit <= 10_000_000):
                raise ManagementValidation()
            if hook.tool_names is not None:
                permissions({"allowed_tools": hook.tool_names})
            hooks.append(hook)
        HookManager(hooks, None)
        return [{**asdict(hook), "args": list(hook.args)} for hook in hooks]

    def update(self, expected_revision, changes):
        try:
            with FileLock(self.lock):
                recover_management_transactions(self.root)
                if expected_revision != self.revision():
                    raise ManagementConflict()
                if set(changes) - GLOBAL_FIELDS:
                    raise ManagementValidation()
                before = read_yaml(self._path("config.yaml"))
                candidate = copy.deepcopy(before)
                for key, value in changes.items():
                    if key == "permissions":
                        value = permissions(value)
                    elif key == "mcp_servers":
                        value = self._mcp(value, before.get(key, []))
                    elif key == "hooks":
                        value = self._hooks(value, before.get(key, []))
                    elif key == "embedding":
                        from aster.memory.semantic import validate_embedding_config
                        value = validate_embedding_config(value)
                    elif key == "web_search":
                        value = validate_web_search(value)
                        previous = before.get(key, {})
                        if (value and value.get("backend") != "deepseek" and value.get("base_url") != previous.get("base_url")
                                and value.get("credential_ref") == previous.get("credential_ref")):
                            value["credential_ref"] = None
                    elif key == "browser":
                        value = validate_browser(value)
                    candidate[key] = value
                self._snapshot(candidate)
                if "web_search" in changes:
                    origins = copy.deepcopy(candidate.get("http_credential_origins", {}))
                    previous_binding = before.get("web_search_binding", {})
                    if previous_binding.get("owned"):
                        reference = previous_binding.get("credential_ref")
                        existing = origins.get(reference, [])
                        origins[reference] = [origin for origin in existing if origin != previous_binding.get("origin")]
                    search = candidate["web_search"]
                    reference = search.get("credential_ref")
                    if reference:
                        origin = canonical_http_origin(search["base_url"])
                        existing = origins.get(reference, [])
                        candidate["web_search_binding"] = {"credential_ref": reference, "origin": origin,
                                                           "owned": origin not in existing}
                        origins[reference] = sorted(set(existing) | {origin})
                    else:
                        candidate.pop("web_search_binding", None)
                    candidate["http_credential_origins"] = origins
                    self._snapshot(candidate)
            if "embedding" in changes:
                validate_embedding_config(candidate["embedding"], self.coordinator.providers)
            if "web_search" in changes:
                validate_web_search(candidate["web_search"], self.coordinator.providers)
            with FileLock(self.lock):
                if expected_revision != self.revision():
                    raise ManagementConflict()
                atomic_write(self._path("config.yaml"), yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False).encode())
        except ManagementConflict:
            raise
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ManagementValidation() from None
        except OSError:
            raise ManagementUnavailable() from None

    def save_agent(self, agent_id, expected_revision, values, *, create=False):
        try:
            agent_id = safe_identifier(agent_id)
            with FileLock(self.lock):
                recover_management_transactions(self.root)
                config_path = self._path("agents", agent_id, "config.yaml")
                instructions_path = self._path("agents", agent_id, "AGENT.md")
                if create:
                    if config_path.parent.exists() or agent_id == "default":
                        raise ManagementConflict()
                    revision = self.revision()
                else:
                    if agent_id != "default" and not config_path.parent.exists():
                        raise KeyError(agent_id)
                    revision = self._agent_revision(agent_id)
                if expected_revision != revision:
                    raise ManagementConflict()
                if set(values) - {"name", "instructions", "selection", "permissions"}:
                    raise ManagementValidation()
                if "name" in values and (not isinstance(values["name"], str) or not values["name"].strip()):
                    raise ManagementValidation()
                if "instructions" in values and not isinstance(values["instructions"], str):
                    raise ManagementValidation()
                global_data = read_yaml(self._path("config.yaml"))
                candidate = read_yaml(config_path)
                if "name" in values:
                    candidate["name"] = values["name"]
                if "permissions" in values:
                    candidate["permissions"] = permissions(values["permissions"])
                if "selection" in values:
                    selection = values["selection"]
                    if set(selection) != {"provider_id", "model", "reasoning_effort", "selected_skill"}:
                        raise ManagementValidation()
                    candidate.update(selection)
                settings = self._snapshot(global_data, agent_id=agent_id, agent_data=candidate)
            self._selection(settings)
            with FileLock(self.lock):
                revision = self.revision() if create else self._agent_revision(agent_id)
                if expected_revision != revision or (create and config_path.parent.exists()):
                    raise ManagementConflict()
                instructions = (values["instructions"].encode() if "instructions" in values else
                                instructions_path.read_bytes() if instructions_path.exists() else None)
                try:
                    commit_agent_pair(self.root, agent_id,
                                      yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False).encode(),
                                      instructions, writer=atomic_write)
                except OSError:
                    if create and not any(config_path.parent.iterdir()):
                        config_path.parent.rmdir()
                    raise
        except ManagementConflict:
            raise
        except (ValueError, TypeError, AttributeError):
            raise ManagementValidation() from None
        except OSError:
            raise ManagementUnavailable() from None

    def save_credential(self, expected_revision, account, value):
        try:
            safe_identifier(account)
            with FileLock(self.lock):
                if expected_revision != self.revision():
                    raise ManagementConflict()
                self.coordinator.credentials.set(account, value)
                return {"credential_ref": f"keyring:aster/{account}", "configured": True,
                        "revision": self.revision()}
        except ManagementConflict:
            raise
        except (ValueError, TypeError):
            raise ManagementValidation() from None

    async def read(self):
        with FileLock(self.lock):
            recover_management_transactions(self.root)
            data = read_yaml(self._path("config.yaml"))
            agent_ids = {"default"}
            folder = self._path("agents")
            if folder.exists():
                for entry in folder.iterdir():
                    if entry.is_dir() and not entry.is_symlink():
                        try:
                            agent_ids.add(safe_identifier(entry.name))
                        except ValueError:
                            continue
            agents = []
            for agent_id in sorted(agent_ids):
                own = read_yaml(self._path("agents", agent_id, "config.yaml"))
                public_keys = {"provider_id", "model", "reasoning_effort", "selected_skill", "permissions", "skills"}
                settings = self._snapshot({key: value for key, value in data.items() if key in public_keys},
                                          agent_id=agent_id,
                                          agent_data={key: value for key, value in own.items() if key in public_keys})
                agents.append({"id": agent_id, "name": own.get("name", agent_id),
                               "instructions": self._bytes(self._path("agents", agent_id, "AGENT.md")).decode("utf-8-sig"),
                               "revision": self._agent_revision(agent_id),
                               "selection": self.coordinator.default_selection(settings),
                               "permissions": permissions(own.get("permissions", {})),
                               "effective_permissions": settings["permissions"]})
            revision = self.revision()
        health = {row["name"]: row for row in await self.coordinator.mcp_health(data)}
        servers = []
        for item in data.get("mcp_servers", []):
            defaults = asdict(MCPServerConfig(**{**item, "cwd": Path(item["cwd"]) if item.get("cwd") else None}))
            defaults["cwd"] = str(defaults["cwd"]) if defaults["cwd"] else None
            defaults["args"] = public_arguments(defaults["args"])
            for category in ("env", "headers"):
                defaults[category] = {name: value if _reference(value) else REDACTED
                                      for name, value in defaults[category].items()}
            defaults["health"] = health[item["name"]]
            servers.append(defaults)
        hooks = [{**asdict(HookCommand(**item)), "args": public_arguments(item.get("args", []))}
                 for item in data.get("hooks", [])]
        from aster.memory.semantic import embedding_status, validate_embedding_config
        return {"schema_version": 1, "revision": revision, "agents": agents, "mcp_servers": servers,
                "hooks": hooks, "permissions": permissions(data.get("permissions", {})),
                "extensions": {"embedding": validate_embedding_config(data.get("embedding", {})),
                               "embedding_status": embedding_status(data.get("embedding", {}), self.coordinator.providers,
                                                                    self.coordinator.local_embeddings),
                               "web_search": validate_web_search(data.get("web_search", {})),
                               "browser": validate_browser(data.get("browser", {}))}}
