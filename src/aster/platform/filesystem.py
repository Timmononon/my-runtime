from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path


class FileLock:
    """Lock a stable sidecar; never remove it or lock a replaced target."""

    def __init__(self, path: Path, timeout: float = 10):
        self.path, self.timeout, self.file = Path(path), timeout, None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.file.seek(0, 2) == 0:
            self.file.write(b"\0")
            self.file.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.file.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.file.close()
                    self.file = None
                    raise TimeoutError(f"Lock busy: {self.path}") from None
                time.sleep(0.025)

    def release(self):
        if self.file is None:
            return
        try:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
            self.file = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.release()


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def scoped_path(raw: str, context, *, must_exist: bool = False) -> Path:
    if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
        raise ValueError("path must be a non-empty string")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = context.cwd / path
    path = path.resolve(strict=must_exist)
    roots = context.services.get("allowed_roots", [context.cwd])
    roots = [Path(root).resolve() for root in roots] + [context.artifact_dir.resolve()]
    if not any(path.is_relative_to(root) for root in roots):
        raise PermissionError(f"Path is outside allowed roots: {path}")
    return path
