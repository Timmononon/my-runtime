"""Collect redistribution notices from the locked packages used in the desktop UI."""
from __future__ import annotations

import json
import re
from pathlib import Path


def write_frontend_notices(frontend: Path, output: Path) -> None:
    frontend = frontend.resolve(strict=True)
    lock = json.loads((frontend / "package-lock.json").read_text(encoding="utf-8"))
    sections = {}
    for relative, record in lock["packages"].items():
        # The generated stylesheet includes Tailwind utilities and preflight.
        if not relative or (record.get("dev") and relative != "node_modules/tailwindcss"):
            continue
        package = (frontend / relative).resolve()
        if not package.is_relative_to(frontend / "node_modules"):
            raise ValueError(f"Expected an installed frontend dependency at {relative}")
        if record.get("optional") and not package.exists():
            continue
        metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
        if metadata["version"] != record["version"]:
            raise ValueError(f"Installed frontend version differs from the lock for {relative}")
        label = f"{metadata['name']} {metadata['version']}"
        if label in sections:
            continue
        texts = [f"{label} ({metadata.get('license', record.get('license', 'see notice below'))})"]
        for path in sorted(package.iterdir()):
            if path.is_file() and re.fullmatch(r"(licen[sc]e|licen[sc]ing|copying|copyright|notice)([-._].*)?",
                                               path.name, flags=re.IGNORECASE):
                texts.append(path.read_text(encoding="utf-8-sig").strip())
        sections[label] = "\n\n".join(texts)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("Aster desktop frontend — third-party notices\n\n" +
                      "\n\n--------------------\n\n".join(sections[label] for label in sorted(sections)) + "\n",
                      encoding="utf-8", newline="\n")
