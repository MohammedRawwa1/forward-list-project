#!/usr/bin/env python3
"""Analyze `migrate data.txt` for link formats not matched by markdown parser.
Writes `unparsed_candidates.txt` with candidate lines and a small summary.
"""
import re
from pathlib import Path

root = Path(__file__).resolve().parent.parent
p = root / "migrate data.txt"
lines = [ln.rstrip('\n') for ln in p.open('r', encoding='utf-8')]

md_link = re.compile(r"\[.*?\]\(.*?\)")
url_pattern = re.compile(r"https?://|t\.me/|telegram\.me/|www\.")

md_lines = []
url_lines = []
dash_lines = []

for i, ln in enumerate(lines):
    s = ln.strip()
    if not s:
        continue
    if md_link.search(s):
        md_lines.append((i+1, s))
        continue
    if url_pattern.search(s):
        url_lines.append((i+1, s))
        continue
    # check for dash-like separators inside square brackets or plain
    if '—' in s or '–' in s or ' - ' in s or ' -' in s or '- ' in s:
        # skip headings like "====="
        if len(s) > 20:
            dash_lines.append((i+1, s))

# try to pair dash_lines with nearby url_lines (url on same or next line)
paired = []
unpaired_dash = []
used_url_lines = set()
for lnno, dash in dash_lines:
    # check same line for url
    if url_pattern.search(dash):
        paired.append((lnno, dash, lnno, dash))
        continue
    # check next 2 lines for url
    found = False
    for offset in (1,2):
        nxt = lnno + offset
        if any(u[0] == nxt for u in url_lines):
            url_text = next(u[1] for u in url_lines if u[0] == nxt)
            paired.append((lnno, dash, nxt, url_text))
            used_url_lines.add(nxt)
            found = True
            break
    if not found:
        unpaired_dash.append((lnno, dash))

# count URL-only lines that weren't used
unused_url_lines = [u for u in url_lines if u[0] not in used_url_lines]

out = root / "unparsed_candidates.txt"
with out.open('w', encoding='utf-8') as fh:
    fh.write(f"Summary:\n")
    fh.write(f"  md_link lines: {len(md_lines)}\n")
    fh.write(f"  url-only lines: {len(url_lines)}\n")
    fh.write(f"  dash-like non-md lines: {len(dash_lines)}\n")
    fh.write(f"  paired dash+url: {len(paired)}\n")
    fh.write(f"  unpaired dash lines: {len(unpaired_dash)}\n")
    fh.write(f"  unused url lines: {len(unused_url_lines)}\n\n")

    fh.write("Paired dash -> url examples (first 200):\n")
    for item in paired[:200]:
        fh.write(str(item) + "\n")
    fh.write("\nUnpaired dash lines (first 200):\n")
    for item in unpaired_dash[:200]:
        fh.write(str(item) + "\n")
    fh.write("\nUnused URL-only lines (first 200):\n")
    for item in unused_url_lines[:200]:
        fh.write(str(item) + "\n")

print("Wrote unparsed_candidates.txt")
