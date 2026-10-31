"""Offline dense embeddings in a lazily owned, cancellable model worker.

Importing this module and reading status never imports torch or loads a model.
Only the explicitly chosen model directory is used; custom modules and pickle
weights are not accepted by this backend.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
import re
import signal
import subprocess
import sys
import uuid
from pathlib import Path, PurePosixPath

from aster.providers.deepseek import ProviderError, _cancellable

# Bounds the IPC memory for embedding batches. Reduce batch_size if it binds.
MAX_FRAME_BYTES = 16 * 1024 * 1024
_MODULES = {"sentence_transformers.models.Transformer", "sentence_transformers.models.Pooling",
            "sentence_transformers.models.Normalize"}
_PACKAGES = ("torch", "sentence-transformers", "transformers", "safetensors")
_SUPPORTED = {"torch": ((2, 6, 0), (3, 0, 0)), "sentence-transformers": ((5, 6, 0), (6, 0, 0)),
              "transformers": ((4, 57, 6), (5, 0, 0)), "safetensors": ((0, 5, 0), (1, 0, 0))}
_ERRORS = {
    "missing_dependencies": "Install Aster's local-embedding optional dependencies first",
    "invalid_model": "Select a local SentenceTransformers model with supported modules and safetensors weights",
    "configuration_changed": "Local embedding model files or settings changed; start a new query",
    "local_device_unavailable": "The selected CUDA device is unavailable; select CPU or install a compatible CUDA runtime",
    "local_out_of_memory": "Local embedding memory is insufficient; reduce batch_size or local_max_tokens",
    "local_load_failed": "The local embedding model could not be loaded; check model files and optional dependencies",
    "local_inference_failed": "Local embedding inference failed; check device and model configuration",
    "local_worker_failed": "The local embedding worker stopped or returned an invalid response",
    "local_budget": "Local embedding IPC budget exceeded; reduce batch_size or input_chars",
    "closed": "The local embedding runtime is closed",
}


def _error(code):
    return ProviderError(_ERRORS.get(code, _ERRORS["local_worker_failed"]), category=code)


def _dependencies():
    versions = {}
    for name in _PACKAGES:
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return None
        match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
        if not match or not _SUPPORTED[name][0] <= tuple(map(int, match.groups())) < _SUPPORTED[name][1]:
            return None
        versions[name] = version
    return versions


def _check_local_drive(path):
    if str(path).startswith(("\\\\", "//")):
        raise ValueError("Local embedding models cannot use network shares")
    if os.name == "nt":
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) == 4:
            raise ValueError("Local embedding models cannot use mapped network drives")


def _json_file(path):
    if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Unsupported local model configuration")
    value = json.loads(path.read_text(encoding="utf-8"))

    def check(item):
        if isinstance(item, dict):
            if item.get("auto_map") or item.get("trust_remote_code"):
                raise ValueError("Custom model code is unsupported")
            if any(item.get(key) for key in ("tokenizer_name_or_path", "model_name_or_path",
                                              "pretrained_model_name_or_path", "subfolder")):
                raise ValueError("Model configuration cannot redirect local file loading")
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
    check(value)
    return value


def _contained(root, relative):
    if not isinstance(relative, str) or "\\" in relative or ":" in relative:
        raise ValueError("Invalid local model path")
    parts = PurePosixPath(relative)
    if parts.is_absolute() or ".." in parts.parts:
        raise ValueError("Invalid local model path")
    value = (root / relative).resolve()
    _check_local_drive(value)
    if not value.is_relative_to(root):
        raise ValueError("Model files must remain inside the selected directory")
    return value


def _model_manifest(config):
    """Bounded metadata identity; source weights are never changed or deserialized."""
    raw = config.get("model_path")
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 4096 or "\x00" in raw:
        raise ValueError("A local model directory is required")
    root = Path(raw)
    if not root.is_absolute():
        raise ValueError("The local model directory must be absolute")
    _check_local_drive(root)
    root = root.resolve(strict=True)
    _check_local_drive(root)
    if not root.is_dir():
        raise ValueError("The local model directory is missing")
    device = config.get("device", "auto")
    maximum = config.get("local_max_tokens", 1024)
    if device not in {"auto", "cpu", "cuda"}:
        raise ValueError("Local embedding device must be auto, cpu or cuda")
    if type(maximum) is not int or not 32 <= maximum <= 8192:
        raise ValueError("Local embedding token limit must be between 32 and 8192")
    modules = _json_file(_contained(root, "modules.json"))
    if not isinstance(modules, list) or not 2 <= len(modules) <= 8:
        raise ValueError("A standard SentenceTransformers module list is required")
    directories = {root}
    transformer = None
    for index, module in enumerate(modules):
        if not isinstance(module, dict) or module.get("type") not in _MODULES or module.get("idx") != index:
            raise ValueError("Custom SentenceTransformers modules are unsupported")
        directory = _contained(root, module.get("path", ""))
        kind = module["type"].rsplit(".", 1)[1]
        if kind == "Transformer":
            if index != 0 or transformer is not None:
                raise ValueError("One leading text Transformer is required")
            transformer = directory
        elif kind == "Pooling":
            if index != 1:
                raise ValueError("A text pooling module must follow the Transformer")
            _json_file(_contained(root, (directory / "config.json").relative_to(root).as_posix()))
        elif kind == "Normalize" and index < 2:
            raise ValueError("Normalization must follow pooling")
        if kind != "Normalize" and not directory.is_dir():
            raise ValueError("A local model module is missing")
        directories.add(directory)
    if transformer is None or modules[1]["type"] != "sentence_transformers.models.Pooling":
        raise ValueError("The model needs a Transformer and Pooling")
    _json_file(_contained(root, (transformer / "config.json").relative_to(root).as_posix()))
    if not (transformer / "tokenizer.json").is_file() and not (transformer / "vocab.txt").is_file():
        raise ValueError("A local tokenizer is required")
    weights = transformer / "model.safetensors"
    weight_files = [weights]
    if not weights.is_file():
        index = _json_file(transformer / "model.safetensors.index.json")
        mapping = index.get("weight_map") if isinstance(index, dict) else None
        if not isinstance(mapping, dict) or not mapping or len(mapping) > 100000:
            raise ValueError("Safetensors weights are required")
        names = set(mapping.values())
        if len(names) > 128 or any(not isinstance(name, str) or not name.endswith(".safetensors") for name in names):
            raise ValueError("Invalid safetensors shards")
        weight_files = [_contained(root, (transformer.relative_to(root) / name).as_posix()) for name in names]
    for weight in weight_files:
        if not weight.is_file() or weight.stat().st_size < 16:
            raise ValueError("Safetensors weights are missing")
        _contained(root, weight.relative_to(root).as_posix())
    files = set(weight_files)
    for directory in directories:
        if not directory.is_dir():
            continue  # Normalize is stateless and often has no directory.
        if (directory / "adapter_config.json").exists():
            raise ValueError("Adapter models are unsupported by the offline dense backend")
        entries = list(directory.iterdir())
        if len(entries) > 256:
            raise ValueError("Local model directory contains too many entries")
        for file in entries:
            if file.is_file() and file.suffix.lower() in {".json", ".model", ".txt", ".safetensors"}:
                _contained(root, file.relative_to(root).as_posix())
                if file.suffix == ".json" and file.name != "tokenizer.json":
                    _json_file(file)
                files.add(file)
    manifest = []
    for file in sorted(files):
        stat = file.stat()
        row = {"path": file.relative_to(root).as_posix(), "bytes": stat.st_size,
               "mtime_ns": stat.st_mtime_ns, "ctime_ns": stat.st_ctime_ns, "inode": stat.st_ino}
        if stat.st_size <= 2 * 1024 * 1024:
            row["sha256"] = hashlib.sha256(file.read_bytes()).hexdigest()
        manifest.append(row)
    return {"backend": "local", "model_path": str(root), "device": device,
            "local_max_tokens": maximum, "manifest": manifest, "encoder_version": 1}


def validate_local_config(config):
    dependencies = _dependencies()
    result = {"configured": False, "available": False, "status": "unconfigured",
              "reason": _ERRORS["invalid_model"], "model_path": config.get("model_path"),
              "dependencies_available": dependencies is not None}
    try:
        model = _model_manifest(config)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return result
    result.update(configured=True, model_path=model["model_path"])
    if dependencies is None:
        result.update(status="missing_dependencies", reason=_ERRORS["missing_dependencies"])
    else:
        result.update(available=True, status="ready", reason="Local files and dependencies are ready; inference has not been verified")
    return result


def _worker_environment():
    # Only operating-system/runtime locations are inherited, never provider keys,
    # tokens, proxy credentials, Python injection variables or user model settings.
    allowed = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP",
               "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "LANG", "LC_ALL", "LD_LIBRARY_PATH"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
               HF_HUB_DISABLE_IMPLICIT_TOKEN="1", TOKENIZERS_PARALLELISM="false")
    return env


class LocalEmbeddingManager:
    """One daemon-owned model process; waiting cancellations never kill its owner."""

    def __init__(self, *, worker_script=None):
        self._worker_script = Path(worker_script) if worker_script else Path(__file__).with_name("local_embedding_worker.py")
        self._lock, self._stop_lock = asyncio.Lock(), asyncio.Lock()
        self._process = self._job = None
        self._identity = None
        self._state, self._device, self._dtype, self._error_code = "unloaded", None, None, None
        self._closed = False
        self._io_tasks: set[asyncio.Task] = set()

    def pin(self, config):
        try:
            model = _model_manifest(config)
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            raise _error("invalid_model") from None
        dependencies = _dependencies()
        if dependencies is None:
            raise _error("missing_dependencies")
        model["libraries"] = dependencies
        model["identity_sha"] = hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()
        return model

    def validate_pin(self, pin):
        try:
            current = self.pin(pin)
        except ProviderError:
            raise _error("configuration_changed") from None
        if current != pin:
            raise _error("configuration_changed")

    def status(self, config):
        readiness = validate_local_config(config)
        state, device, dtype, error = "unloaded", None, None, None
        try:
            current = self.pin(config)["identity_sha"]
        except ProviderError:
            current = None
        if current is not None and current == self._identity:
            state, device, dtype, error = self._state, self._device, self._dtype, self._error_code
            if self._process is not None and self._process.poll() is not None and state in {"loading", "ready"}:
                state, error = "error", "local_worker_failed"
        return {**readiness, "state": "closed" if self._closed else state,
                "device": device, "dtype": dtype, "error_code": error}

    async def _stop(self):
        async with self._stop_lock:
            process, job = self._process, self._job
            if process is None:
                return
            try:
                if job is not None:
                    job.close()  # KILL_ON_JOB_CLOSE owns the complete Windows tree.
                    self._job = None
                elif process.poll() is None:
                    if os.name != "nt":
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        process.kill()
                await asyncio.to_thread(process.wait, timeout=5)
                # A cancelled to_thread await does not terminate its underlying
                # thread. Reaping the owned process closes the pipe, then drain
                # every shielded transfer before disposing its handles.
                if self._io_tasks:
                    await asyncio.gather(*tuple(self._io_tasks), return_exceptions=True)
            finally:
                for stream in (process.stdin, process.stdout):
                    if stream:
                        stream.close()
                self._process = None

    def _spawn(self):
        options = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL,
                   "env": _worker_environment(), "cwd": str(Path(sys.executable).parent), "bufsize": 65536}
        job = None
        if os.name == "nt":
            from aster.platform.processes import WindowsJob
            job = WindowsJob()
            options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004
        else:
            options["start_new_session"] = True
        try:
            process = subprocess.Popen([sys.executable, "-I", "-X", "utf8", "-u", str(self._worker_script)], **options)
            self._process, self._job = process, job
            if job is not None:
                job.assign_and_resume(process.pid)
        except BaseException:
            if job:
                job.close()
            if self._process and self._process.poll() is None:
                self._process.kill()
            raise

    async def _exchange(self, value):
        process = self._process
        encoded = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
        if len(encoded) > MAX_FRAME_BYTES:
            raise _error("local_budget")
        if process is None or process.poll() is not None:
            raise _error("local_worker_failed")

        def transfer():
            view = memoryview(encoded)
            while view:
                written = process.stdin.write(view)
                if not written:
                    raise OSError("Worker input closed")
                view = view[written:]
            process.stdin.flush()
            return process.stdout.readline(MAX_FRAME_BYTES + 1)
        try:
            task = asyncio.create_task(asyncio.to_thread(transfer))
            self._io_tasks.add(task)

            def finished(done):
                self._io_tasks.discard(done)
                if not done.cancelled():
                    done.exception()  # Retrieve an abandoned transfer's exception after cancellation.

            task.add_done_callback(finished)
            raw = await asyncio.shield(task)
            if not raw or len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
                raise ValueError()
            response = json.loads(raw)
            if not isinstance(response, dict) or response.get("id") != value["id"]:
                raise ValueError()
        except (OSError, ValueError, TypeError):
            raise _error("local_worker_failed") from None
        if response.get("error"):
            raise _error(response["error"] if response["error"] in _ERRORS else "local_worker_failed")
        return response

    async def _embed_serial(self, texts, pin, batch_size):
        # Teardown belongs to the request holding this lock; cancelling a
        # waiter must not stop another caller's active model worker.
        async with self._lock:
            if self._closed:
                raise _error("closed")
            try:
                self.validate_pin(pin)
                if (self._process is None or self._process.poll() is not None or self._identity != pin["identity_sha"]):
                    await self._stop()
                    self._identity, self._state = pin["identity_sha"], "loading"
                    self._device = self._dtype = self._error_code = None
                    self._spawn()
                    ready = await self._exchange({"id": uuid.uuid4().hex, "op": "load", "pin": pin})
                    if ready.get("state") != "ready" or ready.get("device") not in {"cpu", "cuda"}:
                        raise _error("local_worker_failed")
                    self.validate_pin(pin)
                    self._device, self._dtype, self._state = ready["device"], ready.get("dtype"), "ready"
                response = await self._exchange({"id": uuid.uuid4().hex, "op": "embed", "texts": texts,
                                                 "batch_size": batch_size})
                if self._closed:
                    raise asyncio.CancelledError()
                self.validate_pin(pin)
                vectors = response.get("vectors")
                counts, truncated = response.get("token_counts"), response.get("truncated_indices")
                maximum = response.get("model_max_tokens")
                if (not isinstance(vectors, list) or len(vectors) != len(texts) or
                        not isinstance(counts, list) or len(counts) != len(texts) or
                        any(type(count) is not int or count < 1 for count in counts) or
                        type(maximum) is not int or not 1 <= maximum <= pin["local_max_tokens"] or
                        not isinstance(truncated, list) or any(type(i) is not int for i in truncated) or
                        truncated != [i for i, count in enumerate(counts) if count > maximum]):
                    raise _error("local_worker_failed")
                dimensions = set()
                for vector in vectors:
                    if (not isinstance(vector, list) or not 1 <= len(vector) <= 65536 or
                            any(type(v) not in {int, float} or not math.isfinite(v) for v in vector)):
                        raise _error("local_worker_failed")
                    norm = math.sqrt(sum(v * v for v in vector))
                    if not math.isfinite(norm) or norm <= 0:
                        raise _error("local_worker_failed")
                    vector[:] = [v / norm for v in vector]
                    dimensions.add(len(vector))
                if len(dimensions) != 1:
                    raise _error("local_worker_failed")
                return {"vectors": vectors, "truncated_indices": truncated, "token_counts": counts,
                        "model_max_tokens": maximum, "device": self._device, "dtype": self._dtype}
            except BaseException as exc:
                await self._stop()
                self._device = self._dtype = None
                if isinstance(exc, asyncio.CancelledError):
                    self._state, self._error_code = "unloaded", None
                    raise
                self._state, self._error_code = "error", getattr(exc, "category", "local_worker_failed")
                if isinstance(exc, ProviderError):
                    raise
                raise _error("local_worker_failed") from None

    async def embed(self, texts, pin, *, cancel=None, timeout_seconds=30, batch_size=8):
        if (not isinstance(texts, list) or not 1 <= len(texts) <= 128 or
                any(not isinstance(t, str) or not t or len(t) > 32000 for t in texts) or
                type(batch_size) is not int or not 1 <= batch_size <= 128 or
                type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds) or
                not 1 <= timeout_seconds <= 300):
            raise ValueError("Local embedding input or budget is invalid")
        try:
            async with asyncio.timeout(timeout_seconds):
                return await _cancellable(self._embed_serial(texts, pin, batch_size), cancel)
        except TimeoutError:
            raise ProviderError("Local embedding timed out; the owned worker was stopped", category="timeout") from None

    async def close(self):
        self._closed = True
        await self._stop()
        async with self._lock:
            self._state = "closed"

