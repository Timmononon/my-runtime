"""Immutable per-run configuration snapshots; no global environment changes."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from aster.platform.filesystem import FileLock


def canonical_http_origin(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("HTTP credential origins must be strings")  # noqa: TRY004 - invalid operator config maps to API 422.
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or "*" in parsed.hostname):
        raise ValueError("Credential origins must be exact HTTP(S) origins without userinfo, paths or wildcards")
    host = parsed.hostname.encode("idna").decode().lower().rstrip(".")
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}"


def _credential_origins(value):
    if not isinstance(value, dict):
        raise ValueError("http_credential_origins must map credential references to exact origins")  # noqa: TRY004 - API configuration validation.
    result = {}
    for reference, origins in value.items():
        if not isinstance(reference, str) or not reference.startswith(("env:", "keyring:")) or not reference.partition(":")[2]:
            raise ValueError("Credential origin keys must be non-empty env: or keyring: references")
        if not isinstance(origins, list):
            raise ValueError("Each credential reference requires a list of exact origins")  # noqa: TRY004 - API configuration validation.
        result[reference] = {canonical_http_origin(origin) for origin in origins}
    return result


def safe_identifier(value: str) -> str:
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{n}" for n in range(1, 10)),
        *(f"LPT{n}" for n in range(1, 10)),
    }
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value)
        or value.endswith(".")
        or value.split(".")[0].upper() in reserved
    ):
        raise ValueError("Identifier must be a safe single path component")
    return value


def read_yaml(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(data, dict):
        raise TypeError(f"Configuration must be a mapping: {path}")
    return data


def _merge(base: dict, overlay: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if key == "permissions":
            continue
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


@dataclass(frozen=True)
class ConfigSnapshot:
    _json: str
    version: str
    agent_id: str
    project_id: str | None
    instructions: str

    @property
    def data(self) -> dict:
        return json.loads(self._json)

    @property
    def cwd(self) -> Path:
        return Path(self.data["cwd"])

    @property
    def allowed_tools(self) -> frozenset[str] | None:
        values = self.data.get("permissions", {}).get("allowed_tools")
        return None if values is None else frozenset(values)

    @property
    def denied_tools(self) -> frozenset[str]:
        return frozenset(self.data.get("permissions", {}).get("denied_tools", []))

    def get(self, key: str, default: Any = None):
        return self.data.get(key, default)


def load_config(
    data_dir: Path,
    *,
    agent_id: str = "default",
    project_id: str | None = None,
    overrides: dict | None = None,
    cwd: Path | None = None,
) -> ConfigSnapshot:
    """Read one coherent configuration while management commits file pairs."""
    agent_id = safe_identifier(agent_id)
    with FileLock(Path(data_dir).resolve() / ".management.lock"):
        from aster.customization.transactions import recover_management_transactions
        recover_management_transactions(data_dir, agent_id=agent_id)
        return _load_config(data_dir, agent_id=agent_id, project_id=project_id, overrides=overrides, cwd=cwd)


def _load_config(
    data_dir: Path,
    *,
    agent_id: str = "default",
    project_id: str | None = None,
    overrides: dict | None = None,
    cwd: Path | None = None,
) -> ConfigSnapshot:
    """Global -> agent -> project -> run; permission overlays only narrow access.

    Instruction files additionally include the exact working directory's rules,
    in increasing specificity. They never change executable permissions or read
    ancestor directories; project links cannot introduce files outside that cwd.
    """
    data_dir = Path(data_dir).resolve()
    agent_id = safe_identifier(agent_id)
    if project_id is not None:
        project_id = safe_identifier(project_id)
    agent_dir = data_dir / "agents" / agent_id
    project_dir = data_dir / "projects" / project_id if project_id else None
    layers = [
        read_yaml(data_dir / "config.yaml"),
        read_yaml(agent_dir / "config.yaml"),
        read_yaml(project_dir / "config.yaml") if project_dir else {},
        overrides or {},
    ]
    merged: dict = {"model": "deepseek-flash", "reasoning_effort": "high", "max_output_tokens": 8192}
    allowed: set[str] | None = None
    denied, ask, auto_approve = set(), set(), set()
    for layer_index, layer in enumerate(layers):
        if not isinstance(layer, dict):
            raise TypeError("Configuration layer must be a mapping")
        if any(str(key).startswith("_") for key in layer):
            raise ValueError("Configuration keys beginning with an underscore are reserved for Runtime snapshots")
        merged = _merge(merged, layer)
        permissions = layer.get("permissions", {})
        if not isinstance(permissions, dict):
            raise TypeError("permissions must be a mapping")
        for key in ("allowed_tools", "denied_tools", "ask_tools", "auto_approve_tools"):
            values = permissions.get(key)
            if values is not None and (
                not isinstance(values, list) or any(not isinstance(v, str) for v in values)
            ):
                raise ValueError(f"permissions.{key} must be a list of tool names")
        if permissions.get("allowed_tools") is not None:
            values = set(permissions["allowed_tools"])
            allowed = values if allowed is None else allowed & values
        denied.update(permissions.get("denied_tools") or [])
        ask.update(permissions.get("ask_tools") or [])
        if layer_index == 0:
            auto_approve = set(permissions.get("auto_approve_tools") or [])
        elif permissions.get("auto_approve_tools") is not None:
            auto_approve &= set(permissions["auto_approve_tools"])
    merged["permissions"] = {
        "allowed_tools": sorted(allowed) if allowed is not None else None,
        "denied_tools": sorted(denied),
        "ask_tools": sorted(ask),
        "auto_approve_tools": sorted(
            (auto_approve if allowed is None else auto_approve & allowed) - denied - ask
        ),
    }
    # Private-network HTTP grants are owned by trusted global configuration;
    # lower layers may only narrow this exact-host allowlist.
    hosts = layers[0].get("http_allowed_hosts", [])
    if not isinstance(hosts, list) or any(not isinstance(host, str) for host in hosts):
        raise ValueError("http_allowed_hosts must be a list of exact host names")
    host_set = {host.lower() for host in hosts}
    for layer in layers[1:]:
        if "http_allowed_hosts" in layer:
            values = layer["http_allowed_hosts"]
            if not isinstance(values, list) or any(not isinstance(host, str) for host in values):
                raise ValueError("http_allowed_hosts must be a list of exact host names")
            host_set &= {host.lower() for host in values}
    merged["http_allowed_hosts"] = sorted(host_set)
    # Resolver compatibility is owned globally. Lower layers can disable the
    # fixed public-DNS fallback, but cannot re-enable an operator's opt-out.
    dns_fallback = layers[0].get("http_dns_fallback", "google")
    for layer in layers:
        mode = layer.get("http_dns_fallback", "google")
        if not isinstance(mode, str) or mode not in {"off", "google"}:
            raise ValueError("http_dns_fallback must be off or google")
        if mode == "off":
            dns_fallback = "off"
    merged["http_dns_fallback"] = dns_fallback
    credential_origins = _credential_origins(layers[0].get("http_credential_origins", {}))
    for layer in layers[1:]:
        if "http_credential_origins" in layer:
            narrower = _credential_origins(layer["http_credential_origins"])
            credential_origins = {reference: origins & narrower.get(reference, set())
                                  for reference, origins in credential_origins.items()}
    merged["http_credential_origins"] = {reference: sorted(origins) for reference, origins in credential_origins.items()}
    target_cwd = Path(cwd or merged.get("cwd") or data_dir / "workspace").expanduser()
    if not target_cwd.is_absolute():
        target_cwd = data_dir / target_cwd
    merged["cwd"] = str(target_cwd.resolve())
    endpoint = merged.get("base_url", "https://api.deepseek.com")
    if not isinstance(endpoint, str) or endpoint.rstrip("/") != "https://api.deepseek.com":
        raise ValueError("This release only sends credentials to https://api.deepseek.com")
    merged["base_url"] = "https://api.deepseek.com"

    # Direct credentials would be copied to run snapshots and exported diagnostics.
    def check_secrets(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "http_credential_origins":
                    _credential_origins(child)
                    continue
                reference = isinstance(child, str) and child.startswith(("env:", "keyring:"))
                sensitive = re.search(
                    r"(?:^|[_-])(api[_-]?key|key|token|secret|password|passwd|credential|authorization)(?:$|[_-])",
                    str(key),
                    re.IGNORECASE,
                )
                if sensitive and child and not reference:
                    raise ValueError("Store credential references, not credentials, in configuration")
                check_secrets(child)
        elif isinstance(value, list):
            for child in value:
                check_secrets(child)

    check_secrets(merged)
    instructions, seen = [], set()
    workspace = target_cwd.resolve()
    paths = [
        data_dir / "INSTRUCTIONS.md",
        agent_dir / "AGENT.md",
        project_dir / "PROJECT.md" if project_dir else None,
        project_dir / "AGENTS.md" if project_dir else None,
    ]
    local_paths = [workspace / "AGENTS.md", workspace / ".aster" / "INSTRUCTIONS.md",
                   *sorted((workspace / ".aster" / "rules").glob("*.md"), key=lambda path: path.name),
                   workspace / ".aster" / "instructions.local.md"]
    for path in local_paths:
        if path.exists() and not path.resolve().is_relative_to(workspace):
            raise ValueError("Project instruction files must remain inside the working directory")
    for path in [*paths, *local_paths]:
        if path is not None and path.is_file() and path.resolve() not in seen:
            seen.add(path.resolve())
            instructions.append(f"Source: {path}\n{path.read_text(encoding='utf-8-sig')}")
    serialized = json.dumps(merged, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    instruction_text = "\n\n".join(instructions)
    version = hashlib.sha256((serialized + "\n" + instruction_text).encode()).hexdigest()
    return ConfigSnapshot(serialized, version, agent_id, project_id, instruction_text)
