"""Private offline worker protocol. Never expose input text or library errors."""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sys

from aster.memory.local_embedding import MAX_FRAME_BYTES, _model_manifest


def _deny_network(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.bind"}:
        raise PermissionError("Local embedding workers do not access the network")


def _load(pin):
    if _model_manifest(pin) != {key: value for key, value in pin.items() if key not in {"identity_sha", "libraries"}}:
        raise ValueError("Model changed")
    import torch
    from sentence_transformers import SentenceTransformer

    requested = pin["device"]
    available = torch.cuda.is_available()
    if requested == "cuda" and not available:
        raise RuntimeError("DEVICE_UNAVAILABLE")
    device = "cuda" if available and requested in {"auto", "cuda"} else "cpu"
    dtype = torch.float16 if device == "cuda" else torch.float32
    # Keep CPU embedding batches from occupying every core of the workbench host.
    torch.set_num_threads(max(1, min(8, os.cpu_count() or 1)))
    model = SentenceTransformer(pin["model_path"], device=device, local_files_only=True,
                                trust_remote_code=False, token=False, backend="torch",
                                model_kwargs={"use_safetensors": True, "torch_dtype": dtype,
                                              "local_files_only": True},
                                config_kwargs={"local_files_only": True},
                                prompts={"query": "", "document": ""})
    native = model.max_seq_length
    maximum = min(pin["local_max_tokens"], native) if isinstance(native, int) and native > 0 else pin["local_max_tokens"]
    model.max_seq_length = maximum
    model.eval()
    return model, device, "float16" if device == "cuda" else "float32", maximum


def main():
    sys.addaudithook(_deny_network)
    logging.disable(logging.CRITICAL)
    output = sys.stdout
    model = None
    maximum = None

    def send(value):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n"
        if len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
            raw = json.dumps({"id": value.get("id"), "error": "local_budget"}) + "\n"
        output.write(raw)
        output.flush()

    # Library progress, warnings and banners must never corrupt the framed wire.
    with open(os.devnull, "w", encoding="utf-8") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
        while True:
            raw = sys.stdin.buffer.readline(MAX_FRAME_BYTES + 1)
            if not raw:
                return
            if len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
                return
            request = None
            try:
                request = json.loads(raw)
                if not isinstance(request, dict) or not isinstance(request.get("id"), str):
                    return
                if request.get("op") == "load" and model is None:
                    model, device, dtype, maximum = _load(request["pin"])
                    send({"id": request["id"], "state": "ready", "device": device, "dtype": dtype})
                elif request.get("op") == "embed" and model is not None:
                    import torch
                    texts = request["texts"]
                    if (not isinstance(texts, list) or not 1 <= len(texts) <= 128 or
                            any(not isinstance(t, str) or not t or len(t) > 32000 for t in texts) or
                            type(request.get("batch_size")) is not int or not 1 <= request["batch_size"] <= 128):
                        raise ValueError("Invalid request")
                    tokens = model.tokenizer(texts, add_special_tokens=True, padding=False, truncation=False,
                                             return_attention_mask=False, verbose=False)
                    counts = [len(ids) for ids in tokens["input_ids"]]
                    with torch.inference_mode():
                        vectors = model.encode(texts, batch_size=request["batch_size"], show_progress_bar=False,
                                               normalize_embeddings=True, convert_to_numpy=True, prompt="")
                    send({"id": request["id"], "vectors": vectors.astype("float32").tolist(),
                          "token_counts": counts, "truncated_indices": [i for i, count in enumerate(counts) if count > maximum],
                          "model_max_tokens": maximum})
                else:
                    raise ValueError("Invalid operation")
            except Exception as exc:  # noqa: BLE001 - third-party errors cross IPC only as fixed safe codes.
                if not isinstance(request, dict):
                    return
                if isinstance(exc, (ImportError, ModuleNotFoundError)):
                    code = "missing_dependencies"
                elif "out of memory" in str(exc).lower():
                    code = "local_out_of_memory"
                elif str(exc) == "DEVICE_UNAVAILABLE":
                    code = "local_device_unavailable"
                else:
                    code = "local_load_failed" if model is None else "local_inference_failed"
                send({"id": request.get("id"), "error": code})
                return


if __name__ == "__main__":
    main()
