from __future__ import annotations

import asyncio
import io
import json
import math
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aster.memory import local_embedding as local
from aster.providers.deepseek import ProviderError


def model_directory(tmp_path):
    root = tmp_path / "model"
    root.mkdir()
    (root / "1_Pooling").mkdir()
    (root / "modules.json").write_text(json.dumps([
        {"idx": 0, "name": "0", "path": "", "type": "sentence_transformers.models.Transformer"},
        {"idx": 1, "name": "1", "path": "1_Pooling", "type": "sentence_transformers.models.Pooling"},
        {"idx": 2, "name": "2", "path": "2_Normalize", "type": "sentence_transformers.models.Normalize"},
    ]), encoding="utf-8")
    (root / "config.json").write_text('{"model_type":"bert","hidden_size":3}', encoding="utf-8")
    (root / "tokenizer.json").write_text('{}', encoding="utf-8")
    (root / "1_Pooling/config.json").write_text('{"pooling_mode_mean_tokens":true}', encoding="utf-8")
    (root / "model.safetensors").write_bytes(b"synthetic-weights-not-loaded")
    return {"backend": "local", "model_path": str(root), "device": "auto", "local_max_tokens": 1024}


FAKE_WORKER = r'''
import json, os, sys, time
for line in sys.stdin:
    request=json.loads(line)
    if request['op']=='load':
        response={'id':request['id'],'state':'ready','device':'cpu','dtype':'float32'}
    else:
        texts=request['texts']
        if texts[0]=='sleep': time.sleep(20)
        if texts[0]=='short_sleep': time.sleep(0.8)
        if texts[0]=='fail':
            print(json.dumps({'id':request['id'],'error':'secret-bearer-should-not-escape'}), flush=True)
            continue
        if texts[0]=='crash': sys.exit(9)
        if texts[0]=='environment':
            assert 'DEEPSEEK_API_KEY' not in os.environ
            assert 'HF_TOKEN' not in os.environ
            assert 'PYTHONPATH' not in os.environ
            assert os.environ['HF_HUB_OFFLINE']=='1'
            assert os.environ['TRANSFORMERS_OFFLINE']=='1'
        counts=[1025 if text=='long' else 3 for text in texts]
        response={'id':request['id'],'vectors':[[3,4,0] for text in texts],
                  'token_counts':counts,'truncated_indices':[i for i,n in enumerate(counts) if n>1024],
                  'model_max_tokens':1024}
        if texts[0]=='invalid': response['vectors']=[[float('nan'),0,1]]
    print(json.dumps(response), flush=True)
'''


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(local, "_dependencies", lambda: {name: "test" for name in local._PACKAGES})
    config = model_directory(tmp_path)
    worker = tmp_path / "worker.py"
    worker.write_text(FAKE_WORKER, encoding="utf-8")
    return config, local.LocalEmbeddingManager(worker_script=worker)


def test_status_has_no_model_import_or_process_and_handles_missing_dependencies(setup, monkeypatch):
    config, manager = setup
    loaded = {key for key in sys.modules if key in {"torch", "sentence_transformers"}}
    monkeypatch.setattr(manager, "_spawn", lambda: pytest.fail("Status must not start a worker"))
    assert local.validate_local_config(config)["available"]
    assert manager.status(config)["state"] == "unloaded"
    assert loaded == {key for key in sys.modules if key in {"torch", "sentence_transformers"}}
    monkeypatch.setattr(local, "_dependencies", lambda: None)
    status = local.validate_local_config(config)
    assert status["configured"] and not status["available"]
    assert status["status"] == "missing_dependencies"


@pytest.mark.parametrize("name,version", [("transformers", "5.14.1"), ("transformers", "4.57.5"),
                                         ("torch", "2.5.0"), ("sentence-transformers", "5.5.0")])
def test_metadata_rejects_incompatible_optional_dependencies_without_imports(monkeypatch, name, version):
    versions = {"torch": "2.7.1+cu128", "sentence-transformers": "5.6.0", "transformers": "4.57.6", "safetensors": "0.8.0"}
    monkeypatch.setattr(local.importlib.metadata, "version", lambda key: versions[key])
    assert local._dependencies() == versions
    versions[name] = version
    assert local._dependencies() is None


@pytest.mark.parametrize("change", ["custom", "escape", "remote_code", "pickle_only", "missing_tokenizer", "redirect", "adapter"])
def test_rejects_untrusted_or_incomplete_local_models(setup, tmp_path, change):
    config, manager = setup
    root = Path(config["model_path"])
    if change in {"custom", "escape"}:
        modules = json.loads((root / "modules.json").read_text())
        modules[0]["type" if change == "custom" else "path"] = "evil.Loader" if change == "custom" else "../outside"
        (root / "modules.json").write_text(json.dumps(modules))
    elif change == "remote_code":
        (root / "config.json").write_text('{"auto_map":{"AutoModel":"evil.Loader"}}')
    elif change == "pickle_only":
        (root / "model.safetensors").rename(root / "pytorch_model.bin")
    elif change == "redirect":
        (root / "sentence_bert_config.json").write_text('{"tokenizer_name_or_path":"C:/another-model"}')
    elif change == "adapter":
        (root / "adapter_config.json").write_text('{}')
    else:
        (root / "tokenizer.json").rename(root / "tokenizer.backup")
    assert not local.validate_local_config(config)["configured"]
    with pytest.raises(ProviderError) as error:
        manager.pin(config)
    assert error.value.category == "invalid_model"


def test_pin_changes_for_model_configs_weights_and_settings(setup):
    config, manager = setup
    original = manager.pin(config)
    for field, value in (("device", "cpu"), ("local_max_tokens", 512)):
        assert manager.pin({**config, field: value})["identity_sha"] != original["identity_sha"]
    root = Path(config["model_path"])
    (root / "tokenizer.json").write_text('{"changed":true}')
    with pytest.raises(ProviderError, match="changed"):
        manager.validate_pin(original)
    updated = manager.pin(config)
    (root / "model.safetensors").write_bytes(b"different-safe-weights-not-loaded")
    assert manager.pin(config)["identity_sha"] != updated["identity_sha"]


async def wait_loaded(manager, config):
    for _ in range(100):
        if manager.status(config)["state"] == "ready":
            return
        await asyncio.sleep(0.02)
    pytest.fail("Worker failed to load")


async def test_lazy_real_process_normalizes_reports_truncation_and_reuses_model(setup):
    config, manager = setup
    pin = manager.pin(config)
    try:
        first = await manager.embed(["short", "long"], pin)
        process = manager._process
        assert first["device"] == "cpu" and first["dtype"] == "float32"
        assert first["truncated_indices"] == [1] and first["token_counts"] == [3, 1025]
        assert first["vectors"][0] == pytest.approx([0.6, 0.8, 0])
        assert math.isclose(sum(v * v for v in first["vectors"][1]), 1)
        await manager.embed(["again"], pin)
        assert manager._process is process
        assert manager.status(config)["state"] == "ready"
        assert manager.status({**config, "device": "cpu"})["state"] == "unloaded"
    finally:
        process = manager._process
        await manager.close()
    assert process.poll() is not None
    assert manager.status(config)["state"] == "closed"


async def test_active_cancel_stops_owned_worker_and_next_call_recovers(setup):
    config, manager = setup
    pin, cancel = manager.pin(config), asyncio.Event()
    try:
        task = asyncio.create_task(manager.embed(["sleep"], pin, cancel=cancel))
        await wait_loaded(manager, config)
        process = manager._process
        cancel.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.poll() is not None
        assert manager._process is None
        assert not manager._io_tasks
        assert manager.status(config)["state"] == "unloaded"
        assert (await manager.embed(["next"], pin))["vectors"]
        assert manager._process.pid != process.pid
    finally:
        await manager.close()


async def test_cancel_waiting_request_does_not_kill_active_owner(setup):
    config, manager = setup
    pin, cancel = manager.pin(config), asyncio.Event()
    try:
        first = asyncio.create_task(manager.embed(["short_sleep"], pin))
        await wait_loaded(manager, config)
        process = manager._process
        second = asyncio.create_task(manager.embed(["queued"], pin, cancel=cancel))
        await asyncio.sleep(0.05)
        cancel.set()
        with pytest.raises(asyncio.CancelledError):
            await second
        assert process.poll() is None
        assert (await first)["vectors"]
        assert manager._process is process
    finally:
        await manager.close()


async def test_timeout_closes_worker_and_shutdown_waits_for_active_query(setup):
    config, manager = setup
    pin = manager.pin(config)
    with pytest.raises(ProviderError) as error:
        await manager.embed(["sleep"], pin, timeout_seconds=1)
    assert error.value.category == "timeout" and manager._process is None
    task = asyncio.create_task(manager.embed(["sleep"], pin))
    await wait_loaded(manager, config)
    process = manager._process
    await asyncio.wait_for(manager.close(), timeout=5)
    await asyncio.gather(task, return_exceptions=True)
    assert process.poll() is not None and manager._process is None
    assert not manager._io_tasks


@pytest.mark.parametrize("text", ["fail", "crash", "invalid"])
async def test_worker_failures_are_sanitized_and_restartable(setup, text):
    config, manager = setup
    pin = manager.pin(config)
    try:
        with pytest.raises(ProviderError) as error:
            await manager.embed([text], pin)
        assert "secret-bearer" not in str(error.value)
        assert manager._process is None
        assert (await manager.embed(["valid"], pin))["vectors"]
    finally:
        await manager.close()


async def test_child_has_offline_environment_without_provider_secrets(setup, monkeypatch):
    config, manager = setup
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-secret")
    monkeypatch.setenv("HF_TOKEN", "synthetic-token")
    monkeypatch.setenv("PYTHONPATH", "untrusted-injection")
    try:
        assert (await manager.embed(["environment"], manager.pin(config)))["vectors"]
    finally:
        await manager.close()


async def test_changed_model_during_inference_discards_result(setup):
    config, manager = setup
    try:
        task = asyncio.create_task(manager.embed(["short_sleep"], manager.pin(config)))
        await wait_loaded(manager, config)
        (Path(config["model_path"]) / "config.json").write_text('{"model_type":"bert","hidden_size":7}')
        with pytest.raises(ProviderError) as error:
            await task
        assert error.value.category == "configuration_changed"
        assert manager._process is None
    finally:
        await manager.close()


def test_worker_network_audit_denies_outbound_without_attempting_it():
    from aster.memory.local_embedding_worker import _deny_network
    for name in ("socket.connect", "socket.getaddrinfo", "socket.bind"):
        with pytest.raises(PermissionError):
            _deny_network(name, ())
    _deny_network("open", ())


def test_network_share_is_rejected_before_any_filesystem_resolution(monkeypatch):
    monkeypatch.setattr(Path, "resolve", lambda *a, **kw: pytest.fail("Do not resolve a network model path"))
    assert not local.validate_local_config({"model_path": r"\\server\share\model"})["configured"]


@pytest.mark.skipif(os.name != "nt", reason="Windows mapped-drive boundary")
def test_mapped_network_model_drive_is_rejected_before_resolution(setup, monkeypatch):
    import ctypes
    config, _manager = setup
    monkeypatch.setattr(ctypes.windll.kernel32, "GetDriveTypeW", lambda anchor: 4)
    monkeypatch.setattr(Path, "resolve", lambda *a, **kw: pytest.fail("Do not resolve a mapped network model path"))
    assert not local.validate_local_config(config)["configured"]


@pytest.mark.skipif(os.name != "nt", reason="Windows network junction boundary")
def test_resolved_network_target_is_rejected(setup, monkeypatch):
    config, _manager = setup
    monkeypatch.setattr(Path, "resolve", lambda *a, **kw: Path(r"\\server\share\model"))
    assert not local.validate_local_config(config)["configured"]


async def test_ipc_writes_complete_frame_when_pipe_accepts_short_chunks():
    class ShortWriter:
        def __init__(self):
            self.data = bytearray()

        def write(self, value):
            count = min(3, len(value))
            self.data.extend(value[:count])
            return count

        def flush(self):
            pass

    manager = local.LocalEmbeddingManager()
    writer = ShortWriter()
    response = {"id": "expected", "state": "ready"}
    manager._process = SimpleNamespace(stdin=writer, stdout=io.BytesIO((json.dumps(response) + "\n").encode()), poll=lambda: None)
    request = {"id": "expected", "texts": ["大批量的中文输入" * 200]}
    assert await manager._exchange(request) == response
    assert json.loads(writer.data) == request
    manager._process = None


@pytest.mark.parametrize("requested,available,expected,dtype", [
    ("cpu", True, "cpu", "float32"), ("auto", True, "cuda", "float16"),
    ("auto", False, "cpu", "float32"), ("cuda", True, "cuda", "float16"),
])
def test_model_loader_is_offline_safe_and_reports_actual_device(setup, monkeypatch, requested, available, expected, dtype):
    from aster.memory.local_embedding_worker import _load
    config, manager = setup
    config["device"] = requested
    calls = []
    fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: available), float16="half", float32="single",
                                 set_num_threads=lambda value: None)
    model = SimpleNamespace(max_seq_length=512, eval=lambda: None)

    def constructor(*args, **kwargs):
        calls.append((args, kwargs))
        return model
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=constructor))
    loaded, device, actual_dtype, maximum = _load(manager.pin(config))
    assert loaded is model and device == expected and actual_dtype == dtype and maximum == 512
    kwargs = calls[0][1]
    assert kwargs["local_files_only"] and kwargs["token"] is False and kwargs["trust_remote_code"] is False
    assert kwargs["model_kwargs"]["use_safetensors"] is True
    assert kwargs["model_kwargs"]["local_files_only"] is True
    assert kwargs["model_kwargs"]["torch_dtype"] == ("half" if expected == "cuda" else "single")


def test_explicit_cuda_is_never_silently_downgraded_to_cpu(setup, monkeypatch):
    from aster.memory.local_embedding_worker import _load
    config, manager = setup
    config["device"] = "cuda"
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=lambda *a, **kw: None))
    with pytest.raises(RuntimeError, match="DEVICE_UNAVAILABLE"):
        _load(manager.pin(config))
