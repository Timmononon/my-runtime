"""Recoverable Agent config/instruction file pairs under the management lock."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from aster.platform.filesystem import atomic_write


class ManagementRecoveryError(RuntimeError):
    def __init__(self):
        super().__init__("An interrupted Agent configuration conflicts with local files; inspect it before continuing")


def _path(root, relative):
    target = root / relative
    parts = Path(relative).parts
    if (len(parts) != 3 or parts[0] != "agents" or parts[2] not in {"config.yaml", "AGENT.md"}
            or not target.resolve().is_relative_to(root.resolve()) or target.is_symlink()):
        raise ManagementRecoveryError()
    return target


def _encoded(content):
    return base64.b64encode(content).decode("ascii") if content is not None else None


def _decoded(value):
    if value is not None and not isinstance(value, str):
        raise ManagementRecoveryError()
    try:
        return base64.b64decode(value, validate=True) if isinstance(value, str) else None
    except ValueError:
        raise ManagementRecoveryError() from None


def _sha(content):
    return hashlib.sha256(content).hexdigest() if content is not None else None


def _apply(root, journal, *, rollback=False):
    try:
        if not journal.resolve().is_relative_to(root.resolve()) or journal.is_symlink():
            raise ManagementRecoveryError()
        document = json.loads(journal.read_text(encoding="utf-8"))
        if document.get("version") != 1 or len(document["entries"]) != 2:
            raise ManagementRecoveryError()
        entries = []
        for entry in document["entries"]:
            target = _path(root, entry["path"])
            if target.parent != journal.parent:
                raise ManagementRecoveryError()
            before, after = _decoded(entry["before"]), _decoded(entry["after"])
            if (entry.get("before_sha256") != _sha(before) or entry.get("after_sha256") != _sha(after)
                    or (after is None and (before is not None or target.name == "config.yaml"))):
                raise ManagementRecoveryError()
            current = target.read_bytes() if target.exists() else None
            if current not in (before, after):
                raise ManagementRecoveryError()
            entries.append((target, before if rollback else after))
        if {target.name for target, _ in entries} != {"config.yaml", "AGENT.md"}:
            raise ManagementRecoveryError()
        for target, content in entries:
            if content is None:
                target.unlink(missing_ok=True)
            else:
                atomic_write(target, content)
        journal.unlink()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ManagementRecoveryError() from None


def commit_agent_pair(root, agent_id, config, instructions, *, writer=atomic_write):
    root = Path(root)
    folder = root / "agents" / agent_id
    folder.mkdir(parents=True, exist_ok=True)
    journal = folder / ".management-transaction.json"
    entries = []
    for name, after in (("config.yaml", config), ("AGENT.md", instructions)):
        target = _path(root, str(Path("agents") / agent_id / name))
        before = target.read_bytes() if target.exists() else None
        entries.append({"path": str(target.relative_to(root)), "before": _encoded(before), "after": _encoded(after),
                        "before_sha256": _sha(before), "after_sha256": _sha(after)})
    atomic_write(journal, json.dumps({"version": 1, "entries": entries}).encode())
    try:
        for entry in entries:
            target, content = _path(root, entry["path"]), _decoded(entry["after"])
            if content is None:
                target.unlink(missing_ok=True)
            else:
                writer(target, content)
    except OSError:
        _apply(root, journal, rollback=True)
        raise
    journal.unlink()


def recover_management_transactions(root, *, agent_id=None):
    root = Path(root)
    journals = ((root / "agents" / agent_id / ".management-transaction.json",) if agent_id else
                sorted((root / "agents").glob("*/.management-transaction.json")))
    for journal in journals:
        if not journal.exists():
            continue
        _apply(root, journal)
