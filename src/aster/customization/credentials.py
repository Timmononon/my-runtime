"""Credential references backed by a separate, user-selected local secret file."""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from aster.paths import resolve_data_dir
from aster.platform.filesystem import FileLock, atomic_write


class CredentialError(RuntimeError):
    pass


class FileCredentialBackend:
    """Keep keyring service/account identities without consulting the OS vault."""

    def __init__(self, data_dir: str | Path | None = None):
        self.path = resolve_data_dir(data_dir) / "credentials.yaml"
        self.lock_path = self.path.parent / ".credentials.lock"

    def _document(self):
        if self.path.is_symlink() or (hasattr(self.path, "is_junction") and self.path.is_junction()):
            raise PermissionError("Credential file cannot be a link")
        if not self.path.exists():
            return {"version": 1, "services": {}}
        document = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        if document is None:
            return {"version": 1, "services": {}}
        if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(document.get("services"), dict):
            raise ValueError("Invalid credential file format")
        for service, accounts in document["services"].items():
            if not isinstance(service, str) or not isinstance(accounts, dict):
                raise TypeError("Credential services must be mappings")
            if any(not isinstance(name, str) or not isinstance(value, str) for name, value in accounts.items()):
                raise TypeError("Credential accounts and values must be text")
            if any(not name or not value or "\n" in value or "\r" in value for name, value in accounts.items()):
                raise ValueError("Credential values must be non-empty single-line text")
        return document

    def get_password(self, service, name):
        return self._document()["services"].get(service, {}).get(name)

    def set_password(self, service, name, value):
        with FileLock(self.lock_path):
            document = self._document()
            document["services"].setdefault(service, {})[name] = value
            # atomic_write uses a private temporary file on POSIX and retains the
            # containing directory's ACL on Windows. Never merge keys into config.
            atomic_write(self.path, yaml.safe_dump(document, allow_unicode=True, sort_keys=True).encode("utf-8"))

    def delete_password(self, service, name):
        with FileLock(self.lock_path):
            document = self._document()
            accounts = document["services"].get(service, {})
            accounts.pop(name, None)
            if not accounts:
                document["services"].pop(service, None)
            atomic_write(self.path, yaml.safe_dump(document, allow_unicode=True, sort_keys=True).encode("utf-8"))


def sensitive_name(name: str) -> bool:
    return bool(
        re.search(
            r"(?:^|[_-])(api[_-]?key|key|token|secret|password|passwd|credential|authorization)(?:$|[_-])",
            name,
            re.IGNORECASE,
        )
    )


def resolve_environment(values: dict[str, str], *, resolver=None) -> dict[str, str]:
    """Resolve references into a new child-only mapping, never os.environ."""
    resolver = resolver or CredentialStore().resolve
    result = {}
    for name, value in values.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise TypeError("Environment names and values must be strings")
        if value.startswith(("env:", "keyring:")):
            result[name] = resolver(value)
        elif sensitive_name(name) and value:
            raise ValueError("Sensitive environment values must be env: or keyring: references")
        else:
            result[name] = value
    return result


class CredentialStore:
    def __init__(self, service: str = "aster", *, backend=None, data_dir: str | Path | None = None):
        if backend is None:
            backend = FileCredentialBackend(data_dir)
        self.service = service
        self.backend = backend
        self.storage = "file" if isinstance(backend, FileCredentialBackend) else "vault"

    def get(self, name: str = "deepseek", *, use_environment: bool = True) -> str | None:
        if use_environment and name == "deepseek" and os.environ.get("DEEPSEEK_API_KEY"):
            return os.environ["DEEPSEEK_API_KEY"]
        try:
            return self.backend.get_password(self.service, name)
        except Exception:  # noqa: BLE001 - Backend errors must never expose credential content.
            raise CredentialError("Local credential configuration is unavailable") from None

    def set(self, name: str, value: str):
        if not name or not value or "\n" in value or "\r" in value:
            raise ValueError("Credential name and a non-empty single-line value are required")
        try:
            self.backend.set_password(self.service, name, value)
        except Exception:  # noqa: BLE001 - Backend errors must never expose credential content.
            raise CredentialError("Could not save local credential configuration") from None

    def delete(self, name: str = "deepseek") -> bool:
        if self.get(name, use_environment=False) is None:
            return False
        try:
            self.backend.delete_password(self.service, name)
        except Exception:  # noqa: BLE001 - Backend errors must never expose credential content.
            raise CredentialError("Could not delete local credential configuration") from None
        return True

    def is_configured(self, name: str = "deepseek") -> bool:
        return bool(self.get(name))

    def resolve(self, reference: str) -> str:
        if reference.startswith("env:"):
            value = os.environ.get(reference[4:])
        elif reference.startswith("keyring:"):
            target = reference[8:]
            if "/" in target:
                service, name = target.split("/", 1)
            else:
                service, name = self.service, target
            try:
                value = self.backend.get_password(service, name)
            except Exception:  # noqa: BLE001 - Backend errors must never expose credential content.
                raise CredentialError("Local credential configuration is unavailable") from None
        else:
            raise ValueError("Unsupported credential reference")
        if not value:
            raise CredentialError("Credential reference is not configured")
        return value
