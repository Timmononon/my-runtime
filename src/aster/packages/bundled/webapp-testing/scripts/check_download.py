"""Verify a download without modifying it."""
import argparse
import hashlib
import json
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument('path',type=Path)
p.add_argument('--sha256')
p.add_argument('--min-bytes',type=int,default=1)
a=p.parse_args()
b=a.path.read_bytes()
h=hashlib.sha256(b).hexdigest()
ok=len(b)>=a.min_bytes and (a.sha256 is None or h==a.sha256)
print(json.dumps({'passed':ok,'bytes':len(b),'sha256':h}))
raise SystemExit(0 if ok else 1)
