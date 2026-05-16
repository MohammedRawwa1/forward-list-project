#!/usr/bin/env python3
"""Check parsing coverage: compare raw markdown link lines to parser output.
Writes `missing_link_lines.txt` and `parsed_entries.json` in repo root.
"""
import re
import json
from pathlib import Path
import sys

# ensure repo root on sys.path
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

try:
    from scripts.migrate_data import parse_migrate_file
except Exception as e:
    print("Failed to import parse_migrate_file:", e)
    raise

p = Path(_repo_root) / "migrate data.txt"
if not p.exists():
    print("Source file not found:", p)
    raise SystemExit(1)

with p.open("r", encoding="utf-8") as fh:
    lines = [ln.rstrip("\n") for ln in fh]

# naive markdown link finder
link_pattern = re.compile(r"\[.*?\]\(.*?\)")
link_lines = [(i+1, ln) for i, ln in enumerate(lines) if link_pattern.search(ln)]

entries = parse_migrate_file(str(p))
parsed_raws = [t[4].strip() for t in entries]

missing = []
for lnno, ln in link_lines:
    if ln.strip() not in parsed_raws:
        missing.append((lnno, ln))

print("Total raw markdown link-like lines found:", len(link_lines))
print("Total parsed entries:", len(entries))
print("Missing count:", len(missing))

with open(Path(_repo_root) / "missing_link_lines.txt", "w", encoding="utf-8") as fh:
    for lnno, ln in missing:
        fh.write(f"{lnno}: {ln}\n")

with open(Path(_repo_root) / "parsed_entries.json", "w", encoding="utf-8") as fh:
    json.dump([{"parent":e[0], "coach":e[1], "course":e[2], "link":e[3], "raw":e[4]} for e in entries], fh, ensure_ascii=False, indent=2)

print("Wrote missing_link_lines.txt and parsed_entries.json")
