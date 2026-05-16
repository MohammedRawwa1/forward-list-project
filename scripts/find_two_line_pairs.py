#!/usr/bin/env python3
import re
from pathlib import Path
root = Path(__file__).resolve().parent.parent
p = root / 'migrate data.txt'
lines = [ln.rstrip('\n') for ln in p.open(encoding='utf-8')]
url_re = re.compile(r'https?://|t\.me/|telegram\.me/|www\.')

pairs = []
for i, ln in enumerate(lines[:-1]):
    s = ln.strip()
    if not s:
        continue
    if '[' in s:
        continue
    if any(sep in s for sep in ['—','–',' - ',' -','-']):
        nxt = lines[i+1].strip()
        if url_re.search(nxt):
            pairs.append((i+1, s, i+2, nxt))

print('Found two-line title+url pairs:', len(pairs))
if pairs:
    out = root / 'two_line_pairs.txt'
    with out.open('w', encoding='utf-8') as fh:
        for a, b, c, d in pairs:
            fh.write(f"{a}: {b}\n{c}: {d}\n\n")
    print('Wrote two_line_pairs.txt')
