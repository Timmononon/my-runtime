"""Digest-pinned local skill packages with preview, receipts and crash recovery."""
from __future__ import annotations

import hashlib
import importlib.resources
import json
import os
import re
import shutil
import stat
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath

import yaml

from aster.config import safe_identifier
from aster.platform.filesystem import FileLock, atomic_write


def tree_digest(files):
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        digest.update(name.encode("utf-8") + b"\0" + hashlib.sha256(content).digest())
    return digest.hexdigest()


def _safe_name(name):
    if "\\" in name or ":" in name or name.startswith("/"):
        raise ValueError("Package paths must be relative POSIX paths")
    path = PurePosixPath(name)
    if any(part in {"", ".", ".."} for part in name.split("/")):
        raise ValueError("Package path traversal is forbidden")
    for part in path.parts:
        safe_identifier(part)
    if any(part.casefold() in {".env", ".ssh", ".aws", "credentials", "credentials.json", "credentials.yaml", ".git"} for part in path.parts):
        raise ValueError("Credential and repository-private files cannot be packaged")
    if "__pycache__" in path.parts or path.suffix.casefold() in {".pyc", ".pyo"}:
        raise ValueError("Bytecode payloads cannot be packaged")
    return path.as_posix()


class SkillPackageManager:
    # Local workflow bundles are intentionally small. Larger packages fail before
    # extraction; callers may configure these budgets explicitly when warranted.
    def __init__(self, data_dir, *, max_files=250, max_file_bytes=1024 * 1024, max_bytes=10 * 1024 * 1024):
        self.root = Path(data_dir).resolve() / "skills"
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_files, self.max_file_bytes, self.max_bytes = max_files, max_file_bytes, max_bytes
        self.transactions = self.root / ".transactions"
        self.plans = self.root / ".plans"
        self.receipts = self.root / ".receipts"
        for path in (self.root, self.transactions, self.plans, self.receipts):
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise ValueError("Skill management directories cannot be links")
            path.mkdir(exist_ok=True)
        with self._lock():
            self._recover()

    def _lock(self):
        return FileLock(self.root / ".packages.lock", timeout=10)

    @staticmethod
    def _write(path, value):
        atomic_write(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode())

    def _remove(self, path):
        path = Path(path)
        if not path.resolve().is_relative_to(self.root.resolve()) or path.resolve() == self.root.resolve():
            raise ValueError("Refusing cleanup outside the package workspace")
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("Refusing linked package cleanup")
        if path.exists():
            shutil.rmtree(path)

    def _validate(self, files):
        if not files or len(files) > self.max_files or sum(map(len, files.values())) > self.max_bytes:
            raise ValueError("Package exceeds file/count/total-byte budget")
        folded = set()
        for name, content in files.items():
            _safe_name(name)
            if name.casefold() in folded:
                raise ValueError("Package has a Windows case-insensitive path collision")
            folded.add(name.casefold())
            if len(content) > self.max_file_bytes:
                raise ValueError("Package file exceeds byte budget")
            try:
                text = content.decode("utf-8-sig")
            except UnicodeDecodeError as error:
                raise ValueError("Workflow packages support UTF-8 text/templates/scripts only") from error
            if re.search(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{20,}", text):
                raise ValueError("Package appears to contain a credential")
            for match in re.finditer(r"(?im)^\s*(?:[A-Z_]*API_KEY|ACCESS_TOKEN|REFRESH_TOKEN|PASSWORD|SECRET)\s*[:=]\s*[\"']?([^\r\n]+)", text):
                if not match.group(1).strip("\"' ").startswith(("env:", "keyring:")):
                    raise ValueError("Packaged secret settings must use env:/keyring: references")
        if "SKILL.md" not in files:
            raise ValueError("A package must have SKILL.md at its root")
        match = re.match(r"\A---\s*\r?\n(.*?)\r?\n---(?:\r?\n|$)", files["SKILL.md"].decode("utf-8-sig"), re.DOTALL)
        metadata = yaml.safe_load(match[1]) if match else None
        if not isinstance(metadata, dict) or not metadata.get("name") or not metadata.get("description"):
            raise ValueError("Skill frontmatter requires name and description")
        name = safe_identifier(metadata["name"])
        secrets = metadata.get("secrets", {})
        if not isinstance(secrets, dict) or any(not isinstance(v, str) or not v.startswith(("env:", "keyring:")) for v in secrets.values()):
            raise ValueError("Skill secrets must be credential references")
        return name

    def _directory(self, source, *, installed=False):
        source = Path(source)
        if source.is_symlink() or (hasattr(source, "is_junction") and source.is_junction()):
            raise ValueError("Package source cannot be a link")
        files = {}
        for current, directories, names in os.walk(source, followlinks=False):
            for name in directories + names:
                child = Path(current, name)
                if child.is_symlink() or (hasattr(child, "is_junction") and child.is_junction()):
                    raise ValueError("Package source contains a link")
            if "__pycache__" in directories and not installed:
                raise ValueError("External packages cannot include compiled bytecode directories")
            directories[:] = [name for name in directories if name != "__pycache__"]
            for name in names:
                child = Path(current, name)
                relative = child.relative_to(source).as_posix()
                if relative == ".aster-receipt.json":
                    continue
                if child.stat().st_size > self.max_file_bytes:
                    raise ValueError("Package file exceeds byte budget")
                files[relative] = child.read_bytes()
                if len(files) > self.max_files or sum(map(len, files.values())) > self.max_bytes:
                    raise ValueError("Package exceeds configured budget")
        return files

    def _source(self, source):
        source = str(source)
        if source.startswith("bundled:"):
            name = safe_identifier(source.partition(":")[2])
            base = importlib.resources.files("aster.packages.bundled").joinpath(name)
            files = {}
            def walk(directory, prefix=""):
                for child in directory.iterdir():
                    if child.name == "__pycache__":
                        continue
                    if child.is_dir():
                        walk(child, prefix + child.name + "/")
                    else:
                        files[prefix + child.name] = child.read_bytes()
            walk(base)
            catalog = {item["name"]: item for item in self.catalog()}
            if name not in catalog or tree_digest(files) != catalog[name]["digest"]:
                raise ValueError("Bundled package does not match its pinned catalogue digest")
        else:
            path = Path(source).resolve(strict=True)
            if Path(source).is_symlink() or (hasattr(Path(source), "is_junction") and Path(source).is_junction()):
                raise ValueError("Package source cannot be a link")
            if path.is_dir():
                files = self._directory(path)
            else:
                files = {}
                with zipfile.ZipFile(path) as archive:
                    for item in archive.infolist():
                        # ZipInfo.filename normalizes Windows backslashes; validate
                        # the original central-directory spelling before normalization.
                        _safe_name(item.orig_filename.rstrip("/"))
                        if item.is_dir():
                            _safe_name(item.filename.rstrip("/"))
                            continue
                        name = _safe_name(item.filename)
                        if stat.S_ISLNK(item.external_attr >> 16) or item.file_size > self.max_file_bytes:
                            raise ValueError("Archive link or oversized entry rejected")
                        if name in files or len(files) >= self.max_files or sum(map(len, files.values())) + item.file_size > self.max_bytes:
                            raise ValueError("Archive duplicate or budget exceeded")
                        files[name] = archive.read(item)
        name = self._validate(files)
        return name, files

    def catalog(self):
        return json.loads(importlib.resources.files("aster.packages.bundled").joinpath("catalog.json").read_text(encoding="utf-8"))["packages"]

    def plan_source(self, plan_id):
        """Read immutable preview provenance for the caller's admission check."""
        safe_identifier(plan_id)
        with self._lock():
            plan = json.loads((self.plans / (plan_id + ".json")).read_text(encoding="utf-8"))
            if not isinstance(plan.get("source"), str):
                raise TypeError("Invalid package plan source")
            return plan["source"]

    def installed(self):
        result = []
        for path in sorted(self.root.iterdir()):
            if path.name.startswith(".") or not path.is_dir() or not (path / "SKILL.md").exists():
                continue
            files = self._directory(path, installed=True)
            receipt = json.loads((path / ".aster-receipt.json").read_text(encoding="utf-8")) if (path / ".aster-receipt.json").exists() else None
            digest = tree_digest(files)
            result.append({"name": path.name, "digest": digest, "receipt": receipt,
                           "modified": receipt is not None and receipt["digest"] != digest})
        return result

    def preview(self, source):
        name, files = self._source(source)
        with self._lock():
            target = self.root / name
            old = self._directory(target, installed=True) if target.exists() else {}
            plan = {"plan_id": uuid.uuid4().hex, "source": str(source) if str(source).startswith("bundled:") else str(Path(source).resolve()),
                    "name": name, "digest": tree_digest(files), "expected_installed_digest": tree_digest(old) if old else None,
                    "action": "upgrade" if old else "install", "created_at": time.time(),
                    "added": sorted(set(files)-set(old)), "removed": sorted(set(old)-set(files)),
                    "changed": sorted(k for k in files.keys() & old.keys() if files[k] != old[k]),
                    "bytes": sum(map(len, files.values())), "file_count": len(files)}
            self._write(self.plans / (plan["plan_id"] + ".json"), plan)
            return plan

    def install(self, plan_id, *, expected_digest):
        safe_identifier(plan_id)
        with self._lock():
            self._recover()
            plan = json.loads((self.plans / (plan_id + ".json")).read_text(encoding="utf-8"))
            name, files = self._source(plan["source"])
            if name != plan["name"] or tree_digest(files) != expected_digest or expected_digest != plan["digest"]:
                raise ValueError("Source changed after preview; preview again")
            target = self.root / name
            current = tree_digest(self._directory(target, installed=True)) if target.exists() else None
            if current != plan["expected_installed_digest"]:
                raise ValueError("Installed skill changed after preview")
            txn = self.transactions / uuid.uuid4().hex
            txn.mkdir()
            stage = txn / "new"
            stage.mkdir()
            for relative, content in files.items():
                path = stage / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(path, content)
            receipt = {"transaction_id": txn.name, "name": name, "digest": expected_digest,
                       "source": plan["source"], "action": plan["action"], "installed_at": time.time(),
                       "files": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}}
            self._write(stage / ".aster-receipt.json", receipt)
            self._write(txn / "transaction.json", {"name": name, "action": "install", "receipt": receipt})
            try:
                if target.exists():
                    os.replace(target, txn / "old")
                os.replace(stage, target)
                self._write(self.receipts / (txn.name + ".json"), receipt)
                self._remove(txn)
            except BaseException:
                self._recover()
                raise
            return receipt

    def uninstall(self, name, *, expected_digest):
        safe_identifier(name)
        with self._lock():
            self._recover()
            target = self.root / name
            if not target.is_dir() or tree_digest(self._directory(target, installed=True)) != expected_digest:
                raise ValueError("Installed skill changed or is missing; inspect it before uninstalling")
            txn = self.transactions / uuid.uuid4().hex
            txn.mkdir()
            receipt = {"transaction_id": txn.name, "name": name, "action": "uninstall", "digest": expected_digest, "installed_at": time.time()}
            self._write(txn / "transaction.json", {"name": name, "action": "uninstall", "receipt": receipt})
            try:
                os.replace(target, txn / "old")
                self._write(self.receipts / (txn.name + ".json"), receipt)
                self._remove(txn)
            except BaseException:
                self._recover()
                raise
            return receipt

    def _recover(self):
        for txn in self.transactions.iterdir():
            if not txn.is_dir() or txn.is_symlink():
                raise ValueError("Invalid package transaction directory")
            journal = txn / "transaction.json"
            if not journal.exists():
                self._remove(txn)
                continue
            state = json.loads(journal.read_text(encoding="utf-8"))
            target = self.root / safe_identifier(state["name"])
            committed = (self.receipts / (txn.name + ".json")).exists()
            if not committed and state["action"] == "install" and target.exists():
                receipt = target / ".aster-receipt.json"
                if receipt.exists() and json.loads(receipt.read_text(encoding="utf-8")).get("transaction_id") == txn.name:
                    self._write(self.receipts / (txn.name + ".json"), state["receipt"])
                    committed = True
            if not committed and (txn / "old").exists():
                if target.exists():
                    raise ValueError("Interrupted install conflicts with a changed target; manual review required")
                os.replace(txn / "old", target)
            self._remove(txn)
