"""Mechanical source-marker/TODO inventory; no network or factual verification."""
import argparse
import json
import re
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("document", type=Path)
args = parser.parse_args()
text = args.document.read_text(encoding="utf-8")
markers = sorted(set(re.findall(r"\[S\d+\]", text)))
todos = [{"line": i, "text": line.strip()} for i, line in enumerate(text.splitlines(), 1) if "TODO" in line]
print(json.dumps({"source_markers": markers, "unresolved": todos, "factual_verification": "not_performed"}, ensure_ascii=False))
