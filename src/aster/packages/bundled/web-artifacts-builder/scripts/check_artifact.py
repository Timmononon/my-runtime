"""Check a standalone HTML artifact's structure and local asset references."""
import argparse
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


def check(path):
    path = path.resolve()
    errors, references, tags = [], [], []

    class Parser(HTMLParser):
        def handle_starttag(self, tag, attrs):
            tags.append(tag)
            values = dict(attrs)
            if "src" in values:
                references.append(values["src"])
            if tag == "link" and "href" in values:
                references.append(values["href"])
            if tag == "html" and not values.get("lang"):
                errors.append("html language is missing")
            if "srcset" in values:
                errors.append("srcset requires manual dependency review")

    text = path.read_text(encoding="utf-8")
    Parser().feed(text)
    for tag in ("html", "title", "main"):
        if tag not in tags:
            errors.append("missing " + tag)
    references.extend(re.findall(r"url\(\s*['\"]?([^)'\"\s]+)", text, re.IGNORECASE))
    if re.search(r"@import\b|\b(?:fetch|WebSocket|importScripts)\s*\(|\bimport\s*\(", text):
        errors.append("dynamic/network dependency requires manual review")
    for reference in references:
        parts = urlsplit(reference)
        if parts.scheme or parts.netloc or "\\" in reference:
            errors.append("nonlocal asset: " + reference)
            continue
        target = (path.parent / unquote(parts.path)).resolve()
        if not target.is_relative_to(path.parent) or not target.is_file():
            errors.append("missing or out-of-scope asset: " + reference)
    return {"passed": not errors, "errors": errors, "asset_count": len(references), "javascript_execution": "not_performed"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", type=Path)
    args = parser.parse_args()
    result = check(args.document)
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["passed"] else 1)
