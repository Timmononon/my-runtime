"""Generate a local palette showcase from the packaged theme references."""
import argparse
import html
import re
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", required=True, type=Path)
args = parser.parse_args()
cards = []
for source in sorted((Path(__file__).resolve().parents[1] / "themes").glob("*.md")):
    text = source.read_text(encoding="utf-8")
    title = text.splitlines()[0].lstrip("# ")
    colors = re.findall(r"#[0-9a-fA-F]{6}\b", text)
    swatches = "".join(f'<span style="background:{color}"></span><code>{color}</code>' for color in colors)
    cards.append(f"<section><h2>{html.escape(title)}</h2><div>{swatches}</div><pre>{html.escape(text)}</pre></section>")
document = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Theme catalog</title><style>body{font:16px Segoe UI,sans-serif;max-width:72rem;margin:2rem auto;padding:1rem;color:#172e31;background:#fafafa}section{border-top:1px solid #999;padding:1rem 0}div{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap}span{width:3rem;height:3rem;border:1px solid #777}pre{white-space:pre-wrap;font:inherit}</style><h1>Local theme catalog</h1>' + "".join(cards) + "</html>"
with args.output.open("x", encoding="utf-8") as output:
    output.write(document)
print(str(args.output.resolve()))
