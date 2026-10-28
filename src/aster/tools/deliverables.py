"""Publish a completed local file into the existing artifact registry and preview bridge."""
import dataclasses
import json
import mimetypes
import os
import re
import stat
import uuid
from pathlib import Path

from jsonschema import validate

from aster.domain import ToolResult, ToolSpec
from aster.platform.filesystem import atomic_write, digest, scoped_path
from aster.service.previews import PREVIEW_EXTENSIONS
from aster.tools.files import TEXT, artifact, failure, schema

ASSET_EXTENSIONS = PREVIEW_EXTENSIONS - {".json", ".txt", ".pdf"}


def private_asset(path):
    return (path.name.startswith(".") or bool(re.search(r"secret|credential|password|token|(?:^|[-_.])env(?:$|[-_.])", path.name, re.IGNORECASE))
            or bool(getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 2)))


class DeliverableTool:
    spec = ToolSpec("present_deliverable", "Register a finished local file for download and optional isolated preview",
                    schema({"path": TEXT, "title": TEXT, "preview": {"type": "boolean"},
                            "interactive": {"type": "boolean"}, "include_assets": {"type": "boolean"}}, ["path"]),
                    risk="read", replay_safe=True, concurrency_safe=False)

    def classify(self, arguments):
        return dataclasses.replace(self.spec, risk="external" if arguments.get("interactive") else "read")

    async def execute(self, arguments, context):
        try:
            validate(arguments, self.spec.parameters)
            if context.cancel.is_set():
                return ToolResult("Cancelled before presenting deliverable", True, "cancelled", "not_executed")
            source = scoped_path(arguments["path"], context, must_exist=True)
            if not source.is_file() or source.stat().st_size > 25 * 1024 * 1024:
                raise ValueError("Deliverable must be a regular file of at most 25 MiB")
            directory = context.artifact_dir / ("deliverable-" + uuid.uuid4().hex)
            target = directory / source.name
            before = digest(source)
            atomic_write(target, source.read_bytes())
            if digest(source) != before or digest(target) != before:
                raise ValueError("Deliverable changed while being copied")
            records = [artifact(target, context, mimetypes.guess_type(source.name)[0] or "application/octet-stream",
                                kind="deliverable", title=arguments.get("title", source.name), sha256=before)]
            root = None
            if arguments.get("include_assets"):
                used, count = target.stat().st_size, 1
                # Explicit request snapshots only browser assets beside the entry;
                # no source tree, configuration files or arbitrary file browsing.
                candidates = []
                for scanned, (current, directories, files) in enumerate(os.walk(source.parent, followlinks=False), 1):
                    directories[:] = [name for name in directories if not private_asset(Path(current) / name)
                                      and name.casefold() not in {"node_modules", "private", "secrets", "credentials", "__pycache__"}
                                      and not (Path(current) / name).is_symlink()
                                      and not (Path(current) / name).is_junction()
                                      and not (Path(current) / name).resolve().is_relative_to(context.artifact_dir.resolve())]
                    if scanned > 500:
                        raise ValueError("Preview asset scan exceeds 500 directories")
                    candidates.extend(Path(current) / name for name in files)
                    if len(candidates) > 5000:
                        raise ValueError("Preview asset scan exceeds 5000 files")
                for candidate in candidates:
                    if private_asset(candidate) or candidate.is_symlink() or candidate.is_junction():
                        continue
                    if candidate == source or candidate.suffix.lower() not in ASSET_EXTENSIONS:
                        continue
                    candidate = scoped_path(str(candidate), context, must_exist=True)
                    relative = candidate.relative_to(source.parent.resolve())
                    used, count = used + candidate.stat().st_size, count + 1
                    if used > 25 * 1024 * 1024 or count > 200:
                        raise ValueError("Preview assets exceed 25 MiB or 200 files; use a smaller asset directory")
                    copy = directory / relative
                    atomic_write(copy, candidate.read_bytes())
                    records.append(artifact(copy, context, mimetypes.guess_type(copy.name)[0] or "application/octet-stream",
                                            kind="deliverable-asset", sha256=digest(copy)))
                root = directory
                records[0]["preview_assets"] = True
            preview = None
            manager = context.services.get("preview_manager")
            if arguments.get("preview", source.suffix.lower() in {".html", ".htm"}):
                if not manager:
                    raise ValueError("Preview service is unavailable")
                preview = manager.register(target, context.session_id, root=root, interactive=arguments.get("interactive", False))
            store = context.services.get("store")
            data = {"artifact_id": records[0]["artifact_id"], "filename": source.name,
                    "title": records[0]["title"], "download_path": f"/artifacts/{records[0]['artifact_id']}",
                    "preview_url": preview, "preview_expires_on_session_close": True,
                    "interactive": arguments.get("interactive", False)}
            if store:
                with store.transaction() as db:
                    store._event(db, context.session_id, context.run_id, "artifact.registered", {"artifacts": records})
                    store._event(db, context.session_id, context.run_id, "deliverable.presented", data)
            return ToolResult(json.dumps(data, ensure_ascii=False), artifacts=records, data=data)
        except Exception as exc:  # noqa: BLE001 - tool operational failures are serialized.
            return failure(exc)
