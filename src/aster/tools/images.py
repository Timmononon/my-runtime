"""Bounded local image/PDF rendering in owned worker processes, never desktop GUI."""
from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path

from jsonschema import validate

from aster.domain import ToolResult, ToolSpec
from aster.platform.filesystem import digest
from aster.tools.files import TEXT, artifact, failure, read_path, schema


def page_numbers(value: str, maximum: int) -> list[int]:
    pages = []
    for item in value.split(","):
        match = re.fullmatch(r"\s*([1-9]\d*)(?:\s*-\s*([1-9]\d*))?\s*", item)
        if not match:
            raise ValueError("pages must use 1-based numbers/ranges, for example 1-3,5")
        first, last = int(match[1]), int(match[2] or match[1])
        if last < first or last - first + 1 > maximum:
            raise ValueError("Page range is reversed or exceeds the page budget")
        pages.extend(range(first, last + 1))
        pages = list(dict.fromkeys(pages))
        if len(pages) > maximum:
            raise ValueError("Requested pages exceed max_pages")
    return pages


def render_worker(kind: str, source: Path, target: Path, options: dict) -> dict:
    from PIL import Image, ImageOps
    maximum = options.get("max_dimension", 1600)
    target.mkdir(parents=True, exist_ok=True)
    images = []

    def save(image, location):
        image = image.convert("RGB")
        image.thumbnail((maximum, maximum), Image.Resampling.LANCZOS)
        path = target / f"{uuid.uuid4().hex}.jpg"
        image.save(path, "JPEG", quality=88, optimize=True)
        if path.stat().st_size > 8 * 1024 * 1024:
            path.unlink()
            raise ValueError("Rendered image exceeds the 8 MiB wire budget")
        images.append({"path": str(path), "width": image.width, "height": image.height, **location})

    if kind == "image_read":
        with Image.open(source) as image:
            if image.format not in {"PNG", "JPEG", "GIF", "WEBP"}:
                raise ValueError("Supported image formats: PNG, JPEG, GIF, WebP")
            if image.width * image.height > 40_000_000:
                raise ValueError("Image exceeds 40 megapixels")
            width, height = image.size
            image.seek(0)
            image.load()
            save(ImageOps.exif_transpose(image), {"frame": 1})
        return {"images": images, "source_width": width, "source_height": height, "method": "pillow"}
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(source) as document:
        count = len(document)
        if not count:
            raise ValueError("PDF contains no pages")
        pages = page_numbers(options["pages"], options["max_pages"]) if options.get("pages") else list(
            range(1, min(count, options["max_pages"]) + 1))
        if max(pages) > count:
            raise ValueError(f"PDF has {count} pages; requested page is out of range")
        for number in pages:
            page = document[number - 1]
            try:
                width, height = page.get_size()
                if min(width, height) <= 0:
                    raise ValueError("Invalid PDF page dimensions")
                scale = min(2, maximum / max(width, height))
                bitmap = page.render(scale=scale, may_draw_forms=False)
                try:
                    save(bitmap.to_pil(), {"page": number})
                finally:
                    bitmap.close()
            finally:
                page.close()
    return {"images": images, "total_pages": count, "pages": pages, "method": "pdfium-page-render"}


class ImageTool:
    def __init__(self, supervisor, *, pdf=False):
        self.supervisor = supervisor
        properties = {"path": TEXT,
                      "max_dimension": {"type": "integer", "minimum": 256, "maximum": 2048},
                      "timeout": {"type": "number", "minimum": 1, "maximum": 120}}
        if pdf:
            properties.update(pages=TEXT, max_pages={"type": "integer", "minimum": 1, "maximum": 10})
        self.spec = ToolSpec("pdf_read_pages" if pdf else "image_read",
                             "Render selected PDF pages as model-visible images with page provenance" if pdf else
                             "Read a local image visually; returns a bounded image artifact, first animation frame only",
                             schema(properties, ["path"]), concurrency_safe=True, replay_safe=True,
                             timeout_seconds=130)

    async def execute(self, arguments, context):
        try:
            validate(arguments, self.spec.parameters)
            if context.cancel.is_set():
                return ToolResult("Cancelled before image read", True, "cancelled", "not_executed")
            source = read_path(arguments["path"], context)
            if source.stat().st_size > 100 * 1024 * 1024:
                raise ValueError("Input exceeds 100 MiB")
            options = {**arguments, "max_pages": arguments.get("max_pages", 5)}
            if options.get("pages"):
                page_numbers(options["pages"], options["max_pages"])
            before = digest(source)
            target = context.artifact_dir / ("images-" + uuid.uuid4().hex)
            result = await self.supervisor.run(
                [sys.executable, "-m", "aster.tools.images", self.spec.name, str(source), str(target), json.dumps(options)],
                cwd=context.cwd, artifact_dir=context.artifact_dir, cancel=context.cancel,
                timeout=arguments.get("timeout", 60), env={"PYTHONIOENCODING": "utf-8"})
            if result.cancelled or result.timed_out or result.exit_code != 0 or result.error:
                category = "cancelled" if result.cancelled else "timeout" if result.timed_out else "image"
                return ToolResult(f"Image rendering failed: {result.error or result.stderr or category}", True, category)
            if digest(source) != before:
                raise ValueError("Source changed during rendering; retry the read")
            output = next(item for item in result.artifacts if item["stream"] == "stdout")
            payload = json.loads(Path(output["path"]).read_text(encoding="utf-8"))
            records, parts = [], []
            for item in payload.pop("images"):
                path = Path(item.pop("path"))
                record = artifact(path, context, "image/jpeg", kind="rendered-image", source=str(source),
                                  source_sha256=before, **item)
                records.append(record)
                parts.append({"type": "input_image", "artifact_id": record["artifact_id"],
                              "path": record["path"], "mime_type": "image/jpeg"})
            body = json.dumps({"source": str(source), "sha256": before, **payload}, ensure_ascii=False)
            return ToolResult(body, artifacts=records,
                              data={**payload, "content_parts": [{"type": "input_text", "text": body}, *parts]})
        except Exception as exc:  # noqa: BLE001 - tool operational failures are serialized.
            return failure(exc)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(render_worker(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), json.loads(sys.argv[4]))))
    except Exception as error:  # noqa: BLE001 - worker terminal error boundary.
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
