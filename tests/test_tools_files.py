import asyncio
import zipfile
from pathlib import Path

import pytest

from aster.domain import ToolContext
from aster.platform.filesystem import FileLock
from aster.tools.files import build_file_tools


@pytest.fixture
def context(tmp_path):
    return ToolContext("session", "run", tmp_path, tmp_path / "artifacts", asyncio.Event())


@pytest.fixture
def tools():
    return {tool.spec.name: tool for tool in build_file_tools()}


async def test_required_content_and_explicit_empty_file(tools, context):
    path = context.cwd / "中文 空格.txt"
    path.write_text("keep me", encoding="utf-8")
    result = await tools["file_write"].execute({"path": str(path)}, context)
    assert result.is_error and result.error_category == "validation"
    assert path.read_text(encoding="utf-8") == "keep me"
    result = await tools["file_write"].execute({"path": "empty.txt", "content": ""}, context)
    assert not result.is_error
    assert (context.cwd / "empty.txt").read_bytes() == b""


async def test_versioned_edit_and_conflict(tools, context):
    path = context.cwd / "中文 空格.txt"
    path.write_bytes("第一行\r\nsecond\r\n".encode())
    read = await tools["file_read"].execute({"path": str(path)}, context)
    args = {"path": str(path), "old_text": "second", "new_text": "changed", "expected_hash": read.data["sha256"]}
    changed = await tools["file_edit"].execute(args, context)
    assert not changed.is_error
    assert path.read_bytes() == "第一行\r\nchanged\r\n".encode()
    conflict = await tools["file_edit"].execute(args, context)
    assert conflict.is_error and "changed since read" in conflict.content
    assert b"changed" in path.read_bytes()


async def test_scope_glob_search_and_large_output(tools, context):
    (context.cwd / "folder").mkdir()
    (context.cwd / "folder" / "a.txt").write_text("needle\n" * 500, encoding="utf-8")
    result = await tools["glob"].execute({"path": ".", "pattern": "**/*.txt"}, context)
    assert "folder/a.txt" in result.content
    result = await tools["text_search"].execute({"path": ".", "query": "needle", "limit": 500}, context)
    assert result.data["count"] == 500 and result.artifacts
    assert Path(result.artifacts[0]["path"]).is_file()
    denied = await tools["file_read"].execute({"path": str(context.cwd.parent / "outside.txt")}, context)
    assert denied.is_error


async def test_archive_traversal_rejected_before_writes(tools, context):
    path = context.cwd / "bad.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("safe.txt", "safe")
        archive.writestr("../escape.txt", "bad")
    result = await tools["archive_extract"].execute({"path": str(path), "destination": "out"}, context)
    assert result.is_error and not (context.cwd / "out").exists()


async def test_archive_extract_and_no_overwrite(tools, context):
    with zipfile.ZipFile(context.cwd / "good.zip", "w") as archive:
        archive.writestr("sub/数据.txt", "text")
    args = {"path": "good.zip", "destination": "out"}
    result = await tools["archive_extract"].execute(args, context)
    assert not result.is_error
    assert (context.cwd / "out/sub/数据.txt").read_text() == "text"
    assert (await tools["archive_extract"].execute(args, context)).is_error


def test_sidecar_lock_remains_and_rejects_contender(tmp_path):
    path = tmp_path / "stable.lock"
    with FileLock(path), pytest.raises(TimeoutError):
        FileLock(path, timeout=0.03).acquire()
    assert path.exists()
    with FileLock(path, timeout=0.1):
        pass


async def test_atomic_replace_failure_keeps_original(tools, context, monkeypatch):
    path = context.cwd / "occupied.txt"
    path.write_text("original")
    read = await tools["file_read"].execute({"path": str(path)}, context)

    def denied(source, destination):
        raise PermissionError("file is open in another program")

    monkeypatch.setattr("aster.platform.filesystem.os.replace", denied)
    result = await tools["file_write"].execute({"path": str(path), "content": "replacement",
                                              "mode": "overwrite", "expected_hash": read.data["sha256"]}, context)
    assert result.is_error and result.error_category == "permission"
    assert path.read_text() == "original"
