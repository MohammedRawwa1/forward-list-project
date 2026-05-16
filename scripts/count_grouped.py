#!/usr/bin/env python3
from pathlib import Path
p = Path('migrate_grouped.txt')
if not p.exists():
    print('migrate_grouped.txt not found')
    raise SystemExit(1)
lines = [l.rstrip('\n') for l in p.open(encoding='utf-8')]
count = 0
for l in lines:
    s = l.strip()
    if not s:
        continue
    if s.startswith('====='):
        continue
    count += 1
print('Non-header lines in grouped file:', count)
