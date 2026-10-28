from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import stat
import tarfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from jsonschema import ValidationError, validate

from aster.domain import ToolContext, ToolResult, ToolSpec
from aster.platform.filesystem import FileLock, atomic_write, digest, scoped_path


def schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


TEXT = {"type": "string", "minLength": 1}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 10000}
# Large source reads keep up to 500K Unicode characters, independent of the
# generic observation spill limit. When it binds, the result reports an exact
# line/column continuation; lower per-call max_chars for tighter requests.
FILE_READ_MAX_CHARS = 500_000


def read_path(raw: str, context: ToolContext) -> Path:
    """Attachment grants are exact-file, read-only grants; they never widen write roots."""
    service = context.services.get("attachment_service")
    if service:
        try:
            return service.resolve_path(context.session_id, raw)
        except PermissionError:
            pass
    resolver = context.services.get("artifact_read_resolver")
    if resolver:
        try:
            return resolver(context.session_id, raw)
        except PermissionError:
            pass
    return scoped_path(raw, context, must_exist=True)


def artifact(path: Path, context: ToolContext, mime_type="text/plain", **extra) -> dict:
    return {"artifact_id": uuid.uuid4().hex, "path": str(path.resolve()), "type": mime_type,
            "mime_type": mime_type, "size": path.stat().st_size, "session_id": context.session_id,
            "run_id": context.run_id, **extra}


def bounded_result(content: str, context: ToolContext, *, data=None, limit=50_000) -> ToolResult:
    if len(content) <= limit:
        return ToolResult(content, data=data or {})
    context.artifact_dir.mkdir(parents=True, exist_ok=True)
    path = context.artifact_dir / f"{uuid.uuid4().hex}-output.txt"
    atomic_write(path, content.encode("utf-8"))
    notice = f"\n[truncated; full output: {path}]"
    return ToolResult(content[:max(0, limit - len(notice))] + notice,
                      artifacts=[artifact(path, context)], data={**(data or {}), "truncated": True})


def failure(exc: Exception, *, outcome="known") -> ToolResult:
    category = "validation" if isinstance(exc, (ValueError, KeyError, ValidationError)) else (
        "permission" if isinstance(exc, PermissionError) else "io")
    return ToolResult(str(exc), is_error=True, error_category=category, outcome=outcome)


class FileTool:
    def __init__(self, name: str, description: str, properties: dict, required: list[str], *, write=False):
        self.spec = ToolSpec(name, description, schema(properties, required),
                             risk="write" if write else "read", concurrency_safe=not write,
                             replay_safe=not write, resource="filesystem" if write else None)

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        try:
            validate(arguments, self.spec.parameters)
            if context.cancel.is_set():
                return ToolResult("Cancelled before file operation", True, "cancelled")
            return await asyncio.to_thread(self._execute, arguments, context)
        except Exception as exc:  # noqa: BLE001 - tool boundary serializes operational failures
            return failure(exc)

    def _execute(self, args, ctx):
        action = self.spec.name
        path = (read_path(args["path"], ctx) if action == "file_read" else
                scoped_path(args.get("path", "."), ctx, must_exist=action not in {"file_write"}))
        if action == "directory_list":
            entries = []
            for child in sorted(path.iterdir(), key=lambda item: item.name.casefold()):
                if ctx.cancel.is_set():
                    return ToolResult("Cancelled", True, "cancelled")
                if len(entries) >= args.get("limit", 1000):
                    break
                info = child.lstat()
                entries.append({"name": child.name, "directory": child.is_dir(),
                                "symlink": child.is_symlink(), "size": info.st_size})
            return bounded_result(json.dumps(entries, ensure_ascii=False, indent=2), ctx,
                                  data={"path": str(path), "count": len(entries)})
        if action in {"glob", "text_search"}:
            matches = []
            pattern = args.get("pattern", "**/*")
            query = args.get("query", "")
            limit = args.get("limit", 1000)
            for root, directories, filenames in os.walk(path, followlinks=False):
                directories[:] = [name for name in sorted(directories)
                                   if name not in {".git", ".venv", "node_modules", "__pycache__"}
                                   and not Path(root, name).is_symlink()
                                   and not (hasattr(Path(root, name), "is_junction") and Path(root, name).is_junction())]
                for filename in sorted(filenames):
                    if ctx.cancel.is_set():
                        return ToolResult("Cancelled", True, "cancelled")
                    target = Path(root, filename)
                    relative = target.relative_to(path).as_posix()
                    if not (fnmatch.fnmatch(relative, pattern) or Path(relative).match(pattern)
                            or (pattern.startswith("**/") and fnmatch.fnmatch(relative, pattern[3:]))):
                        continue
                    try:
                        scoped_path(str(target), ctx, must_exist=True)
                        if action == "glob":
                            matches.append(relative)
                        elif target.stat().st_size <= args.get("max_file_bytes", 2_000_000):
                            with target.open(encoding="utf-8", errors="strict") as source:
                                for number, line in enumerate(source, 1):
                                    text = line.rstrip("\r\n")
                                    found = query in text if args.get("case_sensitive", True) else query.casefold() in text.casefold()
                                    if found:
                                        matches.append({"path": relative, "line": number, "text": text[:4000]})
                                    if len(matches) >= limit:
                                        break
                    except (UnicodeError, PermissionError, OSError):
                        continue
                    if len(matches) >= limit:
                        break
                if len(matches) >= limit:
                    break
            content = json.dumps(matches, ensure_ascii=False, indent=2)
            data = {"count": len(matches), "limit_reached": len(matches) >= limit}
            if action == "text_search":
                # Direct tool callers and the Engine share the 20K policy. The
                # Engine reuses this registered local spill rather than spilling
                # the already bounded excerpt into a second random file.
                return bounded_result(content, ctx, data=data, limit=20_000)
            return bounded_result(content, ctx, data=data)
        if action == "file_read":
            if path.stat().st_size > 25_000_000:
                raise ValueError("File exceeds 25 MB reading budget; use a scoped processing script")
            version = digest(path)
            start, count = args.get("start_line", 1), args.get("max_lines", 300)
            first_column = args.get("start_column", 1)
            char_cap = args.get("max_chars", FILE_READ_MAX_CHARS)
            # Reserve room for an explicit continuation notice inside the cap.
            body_cap, used = char_cap - 256, 0
            rows, continuation, reason = [], None, None
            with path.open(encoding=args.get("encoding", "utf-8"), errors="strict") as source:
                for number, line in enumerate(source, 1):
                    if ctx.cancel.is_set():
                        return ToolResult("Cancelled", True, "cancelled")
                    if number < start:
                        continue
                    text = line.rstrip("\r\n")
                    offset = first_column - 1 if number == start else 0
                    if offset > len(text):
                        raise ValueError("start_column is beyond the requested line")
                    prefix = f"{number}: "
                    remaining = body_cap - used - (1 if rows else 0)
                    segment = text[offset:]
                    if len(prefix) + len(segment) > remaining:
                        kept = max(0, remaining - len(prefix))
                        if remaining >= len(prefix):
                            rows.append(prefix + segment[:kept])
                        continuation, reason = (number, offset + kept + 1), "character limit"
                        break
                    rows.append(prefix + segment)
                    used += len(prefix) + len(segment) + (1 if len(rows) > 1 else 0)
                    if len(rows) >= count:
                        if source.readline():
                            continuation, reason = (number + 1, 1), "line limit"
                        break
            if digest(path) != version:
                raise ValueError("File changed while being read; read again")
            content = "\n".join(rows)
            data = {"path": str(path), "sha256": version, "start_line": start,
                    "start_column": first_column, "lines": len(rows), "encoding": args.get("encoding", "utf-8"),
                    "truncated": continuation is not None, "max_chars": char_cap,
                    # More file lines may exist even when every requested line
                    # was delivered. Only a column/character cut is an incomplete
                    # line range for continuity and deduplication purposes.
                    "range_complete": reason != "character limit" and first_column == 1}
            if continuation:
                data.update(next_start_line=continuation[0], next_start_column=continuation[1],
                            truncation_reason=reason)
                content += (f"\n[Read truncated at {reason}; continue file_read with start_line={continuation[0]}, "
                            f"start_column={continuation[1]}. Full source path and SHA-256 are in metadata.]\n")
            return ToolResult(content, data=data)
        if action in {"file_write", "file_edit"}:
            with FileLock(path.parent / f".{path.name}.aster.lock"):
                exists = path.exists()
                if exists:
                    if not args.get("expected_hash"):
                        raise ValueError("Existing files require expected_hash from file_read")
                    if digest(path) != args["expected_hash"]:
                        raise ValueError("File changed since read; read again before editing")
                elif args.get("expected_hash"):
                    raise ValueError("File no longer exists")
                encoding = args.get("encoding", "utf-8")
                if action == "file_edit":
                    if path.stat().st_size > 25_000_000:
                        raise ValueError("File exceeds 25 MB editing budget; use a scoped script")
                    original = path.read_bytes().decode(encoding)
                    occurrences = original.count(args["old_text"])
                    if not occurrences or (occurrences != 1 and not args.get("replace_all", False)):
                        raise ValueError(f"old_text has {occurrences} matches; must be unique unless replace_all")
                    content = original.replace(args["old_text"], args["new_text"],
                                               -1 if args.get("replace_all", False) else 1)
                else:
                    content = args["content"]
                    if args.get("mode", "create") == "create" and exists:
                        raise ValueError("File exists; explicitly select mode=overwrite or append")
                    if args.get("mode") == "append" and exists:
                        if path.stat().st_size > 25_000_000:
                            raise ValueError("File exceeds append budget")
                        content = path.read_bytes().decode(encoding) + content
                if ctx.cancel.is_set():
                    return ToolResult("Cancelled before commit", True, "cancelled")
                atomic_write(path, content.encode(encoding))
            return ToolResult(f"Saved {path}", data={"path": str(path), "sha256": digest(path),
                                                    "bytes": path.stat().st_size})
        if action.startswith("archive_"):
            return self._archive(path, args, ctx)
        raise ValueError(f"Unknown file action: {action}")

    def _archive(self, path, args, ctx):
        archive = zipfile.ZipFile(path) if zipfile.is_zipfile(path) else tarfile.open(path, "r:*")  # noqa: SIM115 - shared finally closes both adapters
        try:
            is_zip = isinstance(archive, zipfile.ZipFile)
            members = archive.infolist() if is_zip else archive.getmembers()
            if len(members) > 10000:
                raise ValueError("Archive exceeds 10000 member budget")
            rows = []
            names = set()
            total = 0
            for entry in members:
                name = entry.filename if is_zip else entry.name
                size = entry.file_size if is_zip else entry.size
                parts = PurePosixPath(name.replace("\\", "/"))
                if parts.is_absolute() or ".." in parts.parts or ":" in name:
                    raise ValueError(f"Unsafe archive path: {name}")
                normalized = str(parts).casefold()
                if normalized in names or any(part.rstrip(" .") != part or
                        part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *{f"COM{i}" for i in range(1, 10)},
                                                        *{f"LPT{i}" for i in range(1, 10)}} for part in parts.parts):
                    raise ValueError(f"Duplicate or Windows-reserved archive path: {name}")
                names.add(normalized)
                if (is_zip and stat.S_ISLNK(entry.external_attr >> 16)) or (
                    not is_zip and not (entry.isfile() or entry.isdir())):
                    raise ValueError(f"Archive link or special file rejected: {name}")
                total += size
                if total > args.get("max_bytes", 200_000_000):
                    raise ValueError("Archive exceeds extraction byte budget")
                rows.append({"name": name, "size": size})
            if self.spec.name == "archive_inspect":
                return bounded_result(json.dumps(rows, ensure_ascii=False, indent=2), ctx,
                                      data={"members": len(rows), "bytes": total})
            destination = scoped_path(args["destination"], ctx)
            if destination.exists():
                raise ValueError("Extraction destination must not already exist")
            destination.mkdir(parents=True)
            try:
                for entry, row in zip(members, rows):
                    if ctx.cancel.is_set():
                        return ToolResult("Extraction cancelled; partial output retained", True, "cancelled",
                                          data={"destination": str(destination)})
                    target = destination.joinpath(*PurePosixPath(row["name"].replace("\\", "/")).parts)
                    target = scoped_path(str(target), ctx)
                    if not target.is_relative_to(destination.resolve()):
                        raise ValueError("Archive path escapes destination")
                    directory = entry.is_dir() if is_zip else entry.isdir()
                    if directory:
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.open(entry) if is_zip else archive.extractfile(entry)
                    with source, target.open("xb") as output:
                        remaining = row["size"]
                        while block := source.read(min(65536, remaining + 1)):
                            remaining -= len(block)
                            if remaining < 0:
                                raise ValueError("Archive expanded beyond declared size")
                            output.write(block)
                return ToolResult(f"Extracted {len(rows)} members to {destination}",
                                  data={"destination": str(destination), "members": len(rows)})
            except Exception as exc:  # noqa: BLE001 - report retained partial extraction
                return ToolResult(f"Partial extraction at {destination}: {exc}", True, "io")
        finally:
            archive.close()


def build_file_tools() -> list[FileTool]:
    common = {"path": TEXT}
    return [
        FileTool("directory_list", "List a scoped directory", {**common, "limit": LIMIT}, ["path"]),
        FileTool("glob", "Find scoped files with a glob pattern", {**common, "pattern": TEXT, "limit": LIMIT},
                 ["path", "pattern"]),
        FileTool("text_search", "Search UTF-8 files, returning paths and line numbers", {**common,
                 "query": TEXT, "pattern": TEXT, "limit": LIMIT,
                 "case_sensitive": {"type": "boolean"}, "max_file_bytes": {"type": "integer", "minimum": 1,
                                                                               "maximum": 25_000_000}}, ["path", "query"]),
        FileTool("file_read", "Read lines and get a SHA-256 version for later editing", {**common,
                 "start_line": {"type": "integer", "minimum": 1},
                 "start_column": {"type": "integer", "minimum": 1}, "max_lines": LIMIT,
                 "max_chars": {"type": "integer", "minimum": 512, "maximum": FILE_READ_MAX_CHARS},
                 "encoding": TEXT}, ["path"]),
        FileTool("file_write", "Create, overwrite or append text; existing files require expected_hash", {**common,
                 "content": {"type": "string"}, "expected_hash": TEXT, "encoding": TEXT,
                 "mode": {"enum": ["create", "overwrite", "append"]}}, ["path", "content"], write=True),
        FileTool("file_edit", "Replace exact text after checking the file version", {**common,
                 "old_text": TEXT, "new_text": {"type": "string"}, "expected_hash": TEXT, "encoding": TEXT,
                 "replace_all": {"type": "boolean"}}, ["path", "old_text", "new_text", "expected_hash"], write=True),
        FileTool("archive_inspect", "Inspect ZIP or TAR members without executing contents", common, ["path"]),
        FileTool("archive_extract", "Extract ZIP/TAR into a new scoped directory; reject links and traversal",
                 {**common, "destination": TEXT, "max_bytes": {"type": "integer", "minimum": 1,
                                                               "maximum": 1_000_000_000}},
                 ["path", "destination"], write=True),
    ]
