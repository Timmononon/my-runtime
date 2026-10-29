"""Bounded model image projection; original evidence is never rewritten.

The per-image 5 MiB base64 and request 25 MiB budgets mirror the source
runtime's conservative wire limits. All inputs pass the same pixel guard,
including old archives and third-party MCP images that missed source shaping.
"""
from __future__ import annotations

import base64
import copy
import io
from dataclasses import dataclass

from PIL import Image, ImageOps

MAX_SOURCE_IMAGE_BYTES = 25 * 1024 * 1024
MAX_IMAGE_PIXELS = 64_000_000
MAX_INLINE_BASE64_BYTES = 5 * 1024 * 1024
MAX_AGGREGATE_BASE64_BYTES = 25 * 1024 * 1024
MODEL_IMAGE_DIMENSION = 2000
TARGET_RAW_IMAGE_BYTES = (MAX_INLINE_BASE64_BYTES // 4) * 3 - 32
SUPPORTED_IMAGE_MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp"}


class ImagePreparationError(ValueError):
    """Safe actionable image validation, never a raw parser diagnostic."""


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    mime_type: str
    width: int
    height: int
    changed: bool


def inspect_image(raw: bytes, mime_type: str | None = None) -> dict:
    """Validate dimensions before pixel allocation and reject malformed pixels."""
    if not raw or len(raw) > MAX_SOURCE_IMAGE_BYTES:
        raise ImagePreparationError("Image must contain 1 byte to 25 MiB; crop the image or export a smaller copy")
    try:
        with Image.open(io.BytesIO(raw)) as source:
            width, height = source.size
            mime = SUPPORTED_IMAGE_MIMES.get(source.format)
            if not mime:
                raise ImagePreparationError("Use a PNG, JPEG, GIF or WebP image")
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise ImagePreparationError("Image exceeds 64 megapixels; split it into smaller images")
            if mime_type and mime_type != mime:
                raise ImagePreparationError("Image MIME does not match the actual pixel format")
            source.verify()
        # verify() checks the container; load() catches truncated pixel streams.
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
    except ImagePreparationError:
        raise
    except Image.DecompressionBombError:
        raise ImagePreparationError("Image exceeds 64 megapixels; split it into smaller images") from None
    except Exception:  # noqa: BLE001 - parser diagnostics may contain source data.
        raise ImagePreparationError("Image pixels are malformed; export a valid PNG or JPEG copy") from None
    return {"mime_type": mime, "width": width, "height": height}


def prepare_model_image(raw: bytes, mime_type: str | None = None) -> PreparedImage:
    """Return the original bytes or a bounded derivative, never an in-place edit."""
    info = inspect_image(raw, mime_type)
    if len(raw) <= TARGET_RAW_IMAGE_BYTES and max(info["width"], info["height"]) <= MODEL_IMAGE_DIMENSION:
        return PreparedImage(raw, info["mime_type"], info["width"], info["height"], False)
    with Image.open(io.BytesIO(raw)) as source:
        image = ImageOps.exif_transpose(source).convert("RGBA")
        image.thumbnail((MODEL_IMAGE_DIMENSION, MODEL_IMAGE_DIMENSION), Image.Resampling.LANCZOS)
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.getchannel("A"))
    for dimension in (MODEL_IMAGE_DIMENSION, 1024):
        background.thumbnail((dimension, dimension), Image.Resampling.LANCZOS)
        for quality in (80, 60, 40, 20):
            output = io.BytesIO()
            background.save(output, format="JPEG", quality=quality, optimize=True)
            if output.tell() <= TARGET_RAW_IMAGE_BYTES:
                return PreparedImage(output.getvalue(), "image/jpeg", *background.size, True)
    raise ValueError("Image cannot fit the model budget; crop it or request a smaller screenshot")


def image_data_url(raw: bytes, mime_type: str | None = None) -> str:
    prepared = prepare_model_image(raw, mime_type)
    return f"data:{prepared.mime_type};base64,{base64.b64encode(prepared.data).decode('ascii')}"


def normalize_inline_image(url: str) -> str:
    if not isinstance(url, str) or not url.startswith("data:") or ";base64," not in url:
        raise ValueError("Inline images require a registered local file or a base64 data URL")
    header, encoded = url.split(";base64,", 1)
    if len(encoded) > ((MAX_SOURCE_IMAGE_BYTES + 2) // 3) * 4:
        raise ValueError("Inline image exceeds 25 MiB; crop it or export a smaller copy")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise ValueError("Inline image encoding is malformed; export a valid image copy") from None
    return image_data_url(raw, header.removeprefix("data:"))


def enforce_image_budget(encoded: list[dict], *, max_base64_bytes=MAX_AGGREGATE_BASE64_BYTES) -> list[dict]:
    """Evict oldest image blocks individually, preserving pairs and reasoning.

    Only canonical message/tool-output content slots are traversed. Provider
    reasoning items are opaque and must never be rewritten by image policies.
    """
    images = []
    for message_index, item in enumerate(encoded):
        if item.get("type") == "reasoning":
            continue
        field = "output" if item.get("type") == "function_call_output" else "content"
        parts = item.get(field)
        if not isinstance(parts, list):
            continue
        for part_index, part in enumerate(parts):
            if not isinstance(part, dict) or part.get("type") not in {"input_image", "image_url"}:
                continue
            url = part.get("image_url")
            if isinstance(url, dict):
                url = url.get("url")
            if isinstance(url, str) and url.startswith("data:") and ";base64," in url:
                images.append((message_index, field, part_index, len(url.partition(";base64,")[2])))
    total = sum(item[3] for item in images)
    if total <= max_base64_bytes:
        return encoded
    # A shallow copy would still share nested content lists with the caller.
    # Budget eviction must replace blocks only in this request's projection.
    shaped = copy.deepcopy(encoded)
    for message_index, field, part_index, size in images:
        if total <= max_base64_bytes:
            break
        part = shaped[message_index][field][part_index]
        shaped[message_index][field][part_index] = {
            "type": "text" if part["type"] == "image_url" else "input_text",
            "text": f"[Older image omitted: request image payload exceeded {max_base64_bytes} base64 bytes. "
                    "Original image evidence remains in session history; read the required image again.]",
        }
        total -= size
    return shaped
