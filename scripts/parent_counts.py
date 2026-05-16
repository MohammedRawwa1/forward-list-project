#!/usr/bin/env python3
import json
from pathlib import Path
root = Path(__file__).resolve().parent.parent
p = root / 'parsed_entries.json'
if not p.exists():
    print('parsed_entries.json not found; run scripts/check_parse_coverage.py first')
    raise SystemExit(1)
entries = json.load(p.open(encoding='utf-8'))
counts = {}
for e in entries:
    p = e.get('parent') or 'Uncategorized'
    counts[p] = counts.get(p, 0) + 1
for k, v in sorted(counts.items(), key=lambda t: -t[1]):
    print(f"{v:4d}  {k}")
