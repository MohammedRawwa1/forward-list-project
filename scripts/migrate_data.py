#!/usr/bin/env python3
"""Migration helper: parse a plaintext export and create categories/coaches/courses.

Usage: python scripts/migrate_data.py --file "migrate data.txt" [--dry-run]

Expected format in the file:
- Top-level sections are printed with a surrounding line of box/line characters
  (e.g. a line of `━━━━━━━━`), with the header on the line between them
  (e.g. "💆  MASSAGE"). The script treats that header as the parent category.
- Course lines look like: [Course Title — Coach Name](https://...)
  (the separator between title and coach is the last dash character inside the [])

The script will create one parent doc per section, then a category per coach
with `parent` set to the parent section name, `path` = "Parent/Coach", and
append course objects to the coach document's `courses` array.

Be careful: ensure `MONGODB_URL` and `MONGODB_NAME` are set in environment
or in a .env file before running. Use `--dry-run` to preview actions.
"""

import argparse
import asyncio
import os
import re
import sys
import json
from pathlib import Path


# Ensure repository root is on sys.path so local packages (e.g. `database`) can be imported
try:
    from pathlib import Path
    _repo_root = Path(__file__).resolve().parent.parent
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))
except Exception:
    pass

from dotenv import load_dotenv
from database.mongo_handler import MongoDB

def extract_header_from_line(line: str) -> str:
    """Try to extract an uppercase header (e.g. 'TANTRA' or 'MASSAGE') from a line.
    Returns title-cased header or None if not matched.
    """
    if not line:
        return None
    s = line.strip()
    # attempt to find an uppercase word sequence (letters, numbers, spaces, & and hyphen)
    m = re.search(r"[A-Z][A-Z0-9 &'\-]+", s)
    if m:
        return m.group(0).title()
    # fallback: strip leading non-alphanum and title-case
    fallback = re.sub(r"^[^A-Za-z0-9]+", "", s).strip()
    return fallback.title() if fallback else None


def parse_migrate_file(path: str):
    """Parse the migrate text file and yield tuples (parent, coach, course, link).
    The parser looks for header sections framed by lines containing long
    runs of box/line characters (e.g. '━' or '═'). The header is taken from the
    line between those runs. Course lines are Markdown-style links.
    """
    entries = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = [ln.rstrip("\n") for ln in fh]
    except Exception as e:
        raise RuntimeError(f"Failed to read {path}: {e}")

    current_parent = None
    i = 0
    L = len(lines)
    while i < L:
        raw_line = lines[i]
        line = raw_line.strip()
        # detect framing line of box characters (a sequence of '━' or '═' etc.)
        if line and all(ch in "━═─━━══─" or ch == ' ' for ch in line) and len(line) > 10:
            # look ahead for header on next non-empty line
            j = i + 1
            while j < L and not lines[j].strip():
                j += 1
            if j < L:
                header_line = lines[j].strip()
                header = extract_header_from_line(header_line)
                if header:
                    current_parent = header
                    # skip ahead past the following framing line if present
                    i = j + 1
                    continue
        # Additional header detection: some files have plain header lines
        # (e.g. 'DATING' or '💆 MASSAGE') without framing characters. Detect
        # a header if the current line looks like a header and the next
        # non-empty line is a link or URL.
        if not current_parent:
            candidate = extract_header_from_line(line)
            if candidate:
                k = i + 1
                while k < L and not lines[k].strip():
                    k += 1
                if k < L:
                    nxt = lines[k].strip()
                    if nxt.startswith("[") or re.search(r"https?://|t\.me/|telegram\.me/", nxt):
                        current_parent = candidate
                        # move index to the next line containing links
                        i = k
                        continue
        # If the line begins with '[' try multiple link formats (robust against
        # missing parentheses or closing paren), then fall back to plain
        # 'Course — Coach' lines without an explicit URL.
        if line.startswith("["):
            # 1) Standard markdown: [Text](url)
            m = re.match(r"^\[(?P<inside>[^\]]+)\]\((?P<link>[^)]+)\)", line)
            # 2) Bracketed text immediately followed by URL without parentheses
            if not m:
                m = re.match(r"^\[(?P<inside>[^\]]+)\]\s*(?P<link>https?://\S+)", line)
            # 3) Bracketed text with opening paren but missing closing paren
            if not m:
                m = re.match(r"^\[(?P<inside>[^\]]+)\]\((?P<link>.+)$", line)

            if m:
                inside = m.group("inside").strip()
                link = m.group("link").strip()
                # normalize link by stripping trailing unmatched ) or whitespace
                link = re.sub(r"[)\]\s]+$", "", link)

                # split inside on the LAST dash-like separator to get course and coach
                left = None
                right = None
                for sep in [" — ", "—", " – ", "–", " - ", "-"]:
                    if sep in inside:
                        left, right = inside.rsplit(sep, 1)
                        break
                if left is None:
                    # No separator found; treat entire inside as course title and coach unknown
                    course = inside
                    coach = ""
                else:
                    course = left.strip()
                    coach = right.strip()

                # Map parsed header and entry into the best parent bucket
                parent_raw = current_parent or ""
                parent = map_entry_to_parent(parent_raw, coach, course)
                # Preserve the original raw line so we can reproduce exact link text
                entries.append((parent, coach or "Unknown", course, link, raw_line))

        # Plain lines like 'Course Title — Coach Name' (no bracketed link)
        elif any(sep in line for sep in [" — ", "—", " – ", "–", " - "]) and not line.startswith("="):
            # Avoid accidentally matching framing/header lines; require a reasonable length
            if len(line) > 6:
                inside = line
                left = None
                right = None
                for sep in [" — ", "—", " – ", "–", " - ", "-"]:
                    if sep in inside:
                        left, right = inside.rsplit(sep, 1)
                        break
                if left is None:
                    course = inside
                    coach = ""
                else:
                    course = left.strip()
                    coach = right.strip()
                parent_raw = current_parent or ""
                parent = map_entry_to_parent(parent_raw, coach, course)
                entries.append((parent, coach or "Unknown", course, "", raw_line))
        i += 1

    return entries



def _sanitize_filename(name: str) -> str:
    """Return a filesystem-safe name for use as a filename/directory."""
    if not name:
        return "unknown"
    s = str(name)
    # Remove common Markdown link artifacts like '](...') if present
    s = re.sub(r"\]\([^)]*\)", "", s)
    # Remove leftover brackets and parentheses
    s = s.replace("[", "").replace("]", "").replace("(", "").replace(")", "")
    # Replace slashes and other problematic chars with underscores
    s = re.sub(r'[\/\0\n\r<>:\"|?*]+', '_', s)
    # Collapse whitespace and trim
    s = re.sub(r"\s+", " ", s).strip()
    s = s.strip(".-_")
    if not s:
        return "unknown"
    return s


def _clean_label(name: str) -> str:
    """Clean a display label (parent/coach) by removing markdown/link artifacts and trimming."""
    if not name:
        return name
    s = str(name)
    # remove markdown link fragments like '](...)'
    s = re.sub(r"\]\([^)]*\)", "", s)
    # remove stray brackets
    s = s.replace("[", "").replace("]", "")
    # collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


# The canonical parent categories that should be used for migration.
PARENT_BUCKETS = [
    "Massage",
    "Orgasms",
    "Sex",
    "Sexual Energy Mastery",
    "Tantra",
    "Dating",
    "Texting",
]



def map_parent_to_bucket(label: str) -> str:
    """Map a parsed header label to one of the PARENT_BUCKETS.

    Heuristics are simple substring checks and fall back to 'Sex'.
    """
    if not label:
        return "Other"
    s = label.lower()
    if "massage" in s:
        return "Massage"
    if "orgasm" in s or "orgasms" in s:
        return "Orgasms"
    if "sexual energy" in s or "authentic man" in s or "energy" in s:
        return "Sexual Energy Mastery"
    if "tantra" in s:
        return "Tantra"
    # catch-all: if the text mentions 'sex' or 'body' treat as Sex
    if "sex" in s or "body" in s:
        return "Sex"

    # default: prefer the original (cleaned) header label instead of forcing
    # everything into a generic bucket. This preserves section names like 'Dating'.
    return _clean_label(label)


# Keyword -> parent mapping for improved heuristics. Order matters (specific -> general).
KEYWORD_TO_PARENT = {
    # Dating / relationship
    "dating": "Dating",
    "pickup": "Dating",
    "pickup artist": "Dating",
    "romance": "Dating",
    "relationship": "Dating",
    "dating advice": "Dating",

    # Texting / phone / SMS
    "texting": "Texting",
    "text messages": "Texting",
    "text message": "Texting",
    "text machine": "Texting",
    "texts": "Texting",
    "text": "Texting",
    "tinder": "Texting",
    "phone game": "Texting",
    "phone": "Texting",
    "sms": "Texting",

    # Seduction / attraction
    "seduction": "Seduction",
    "seduce": "Seduction",
    "seducer": "Seduction",
    "attraction": "Attraction",
    "attract": "Attraction",
    "flirt": "Flirting",
    "flirting": "Flirting",

    # Dark psychology / persuasion / manipulation / NLP
    "dark psychology": "Dark Psychology",
    "manipulation": "Dark Psychology",
    "manipulative": "Dark Psychology",
    "psychology": "Psychology",
    "persuasion": "Persuasion",
    "nlp": "NLP",
    "neuro linguistic": "NLP",
    "neuro-linguistic": "NLP",

    # Personal development / charisma / confidence
    "charisma": "Charisma",
    "confidence": "Confidence",
    "self confidence": "Confidence",

    # Body language / nonverbal
    "body language": "Body Language",
    "nonverbal": "Body Language",

    # Tantra / massage / sexual skills
    "tantra": "Tantra",
    "tantric": "Tantra",
    "massage": "Massage",
    "prostate": "Sex Health",
    "ejaculation": "Sex Health",
    "orgasm": "Orgasms",
    "squirting": "Orgasms",
    "foreplay": "Sex",
    "sexual energy": "Sexual Energy Mastery",

    # Kink / BDSM
    "kink": "Kink",
    "bdsm": "Kink",
    "shibari": "Kink",

    # Note: removed overly-generic 'sex' / 'sexual' keyword mappings to avoid
    # collapsing many entries into a generic 'Sex' parent. More specific
    # keywords (e.g., 'orgasm', 'foreplay') remain mapped where appropriate.
}


def map_entry_to_parent(parent_raw: str, coach: str, course: str) -> str:
    """Determine the best parent bucket for an entry using header, coach and course text.

    Preference order:
    1. Keyword match in course or coach.
    2. Keyword match in parent header.
    3. Fallback to map_parent_to_bucket(parent_raw).
    """
    text = " ".join([str(parent_raw or ""), str(coach or ""), str(course or "")]).lower()

    # Match multi-word and longer keywords first for specificity.
    # Use word-boundary regex to avoid accidental partial matches.
    ordered_keywords = sorted(KEYWORD_TO_PARENT.keys(), key=lambda k: -len(k))
    for kw in ordered_keywords:
        pattern = r"\b" + re.escape(kw) + r"\b"
        if re.search(pattern, text):
            return KEYWORD_TO_PARENT[kw]

    # If no keyword matched, try mapping from parent header heuristics
    bucket = map_parent_to_bucket(parent_raw)
    if bucket:
        return bucket

    return "Other"


def write_grouped_file(entries, out_path: str):
    """Write a text file grouping the original raw lines under parent headers.

    entries: list of (parent, coach, course, link, raw_line)
    """
    grouped = {}
    for parent, coach, course, link, raw in entries:
        p = parent or "Uncategorized"
        grouped.setdefault(p, []).append(raw)

    def format_parent_name(name: str) -> str:
        """Format parent header: if the name is ALL UPPER keep it,
        otherwise capitalize words (Title Case) so first letters are uppercase.
        """
        if not name:
            return "Uncategorized"
        s = str(name).strip()
        # If the string is already all uppercase, preserve it (e.g., NLP)
        if s == s.upper():
            return s
        # For typical names, use title case so each word's first letter is uppercase
        return s.title()

    outp = Path(out_path)
    outp.parent.mkdir(parents=True, exist_ok=True)
    with open(outp, "w", encoding="utf-8") as fh:
        # write canonical parents first (if present), then others alphabetically
        seen = set()
        for p in PARENT_BUCKETS:
            if p in grouped:
                fh.write(f"\n===== {p} =====\n\n")
                for line in grouped[p]:
                    fh.write(line.rstrip("\n") + "\n")
                seen.add(p)

        # remaining parents
        for p in sorted(k for k in grouped.keys() if k not in seen):
            fh.write(f"\n===== {p} =====\n\n")
            for line in grouped[p]:
                fh.write(line.rstrip("\n") + "\n")


async def migrate(entries, dry_run=False, out="db", out_dir=None, report_path=None, global_dedupe=False):
    """Migrate entries to either MongoDB (`out=='db'`) or filesystem (`out=='fs'`).

    For filesystem mode, `out_dir` must be provided and will be used as the
    root directory where parent directories are created and coach JSON files
    stored (`<out_dir>/<Parent>/<Coach>.json`).
    """
    created = {"parents": 0, "coaches": 0, "courses": 0, "skipped": 0}
    report_entries = []

    if out == "db":
        db = await MongoDB.get_db()
        cat_coll = db["categories"]

        # Determine all parents from the parsed entries (plus canonical buckets)
        parents_set = set(_clean_label(p) for p, _, _, _, _ in entries) | set(PARENT_BUCKETS)
        for parent_name in parents_set:
            doc = await cat_coll.find_one({"name": parent_name})
            if not doc:
                print(f"Create parent bucket: {parent_name}")
                if not dry_run:
                    await cat_coll.update_one(
                        {"name": parent_name},
                        {"$setOnInsert": {"name": parent_name, "parent": None, "path": parent_name, "courses": []}},
                        upsert=True,
                    )
                created["parents"] += 1

        for parent, coach, course, link, _raw in entries:
            p_clean = _clean_label(parent)
            c_clean = _clean_label(coach)
            entry_report = {"parent": p_clean, "coach": c_clean, "course": course, "link": link, "raw": _raw, "result": None, "reason": None}

            # create parent if missing
            parent_doc = await cat_coll.find_one({"name": p_clean})
            if not parent_doc:
                print(f"Create parent category: {p_clean}")
                entry_report["parent_created"] = True
                if not dry_run:
                    await cat_coll.update_one(
                        {"name": p_clean},
                        {"$setOnInsert": {"name": p_clean, "parent": None, "path": p_clean, "courses": []}},
                        upsert=True,
                    )
                created["parents"] += 1

            # create/ensure coach category exists under parent
            coach_doc = await cat_coll.find_one({"name": c_clean})
            coach_target_name = c_clean
            if not coach_doc:
                print(f"  Create coach category: {c_clean} (parent={p_clean})")
                entry_report["coach_created"] = True
                if not dry_run:
                    await cat_coll.update_one(
                        {"name": c_clean},
                        {"$setOnInsert": {"name": c_clean, "parent": p_clean, "path": f"{p_clean}/{c_clean}", "courses": []}},
                        upsert=True,
                    )
                created["coaches"] += 1
            else:
                # if coach exists but parent not set or different, do NOT overwrite the existing coach's parent.
                # Instead create a disambiguated coach document for the new parent (e.g., 'Coach Name (Texting)').
                existing_parent = coach_doc.get("parent")
                if existing_parent and existing_parent != p_clean:
                    # create a variant coach name under the new parent to avoid moving existing coach's courses
                    variant_name = f"{c_clean} ({p_clean})"
                    variant_doc = await cat_coll.find_one({"name": variant_name})
                    if not variant_doc:
                        print(f"  Coach '{c_clean}' already under parent='{existing_parent}'. Creating '{variant_name}' for parent='{p_clean}'")
                        entry_report["coach_created_variant"] = variant_name
                        if not dry_run:
                            await cat_coll.update_one(
                                {"name": variant_name},
                                {"$setOnInsert": {"name": variant_name, "parent": p_clean, "path": f"{p_clean}/{variant_name}", "courses": []}},
                                upsert=True,
                            )
                        created["coaches"] += 1
                    coach_target_name = variant_name
                else:
                    # if existing parent is empty or matches, ensure parent/path are set if missing
                    if not existing_parent or existing_parent != p_clean:
                        print(f"  Note: existing coach '{c_clean}' has parent='{existing_parent}', setting parent='{p_clean}'")
                        if not dry_run:
                            try:
                                await cat_coll.update_one({"name": c_clean}, {"$set": {"parent": p_clean, "path": f"{p_clean}/{c_clean}"}})
                            except Exception as e:
                                print(f"    Warning: failed to update parent for '{c_clean}': {e}")

            # add course under coach if not exists
            # If global_dedupe is enabled, check for the course anywhere in the collection
            if global_dedupe:
                exists = await cat_coll.find_one({"courses.name": course})
                entry_report["checked_global_dedupe"] = True
            else:
                exists = await cat_coll.find_one({"name": coach_target_name, "courses.name": course})
            if exists:
                print(f"    Skip existing course: {course} (coach={coach_target_name})")
                entry_report["result"] = "skipped"
                entry_report["reason"] = "already_exists"
                created["skipped"] += 1
            else:
                print(f"    Add course: {course} -> {link} (coach={coach_target_name})")
                if not dry_run:
                    try:
                        await cat_coll.update_one({"name": coach_target_name}, {"$push": {"courses": {"name": course, "link": link}}})
                        entry_report["result"] = "added"
                    except Exception as e:
                        print(f"    Error adding course '{course}' to '{coach_target_name}': {e}")
                        entry_report["result"] = "error"
                        entry_report["reason"] = str(e)
                        created["skipped"] += 1
                        report_entries.append(entry_report)
                        continue
                else:
                    entry_report["result"] = "would_add"
                created["courses"] += 1

            report_entries.append(entry_report)

        # write report if requested
        if report_path:
            try:
                with open(report_path, "w", encoding="utf-8") as fh:
                    json.dump(report_entries, fh, ensure_ascii=False, indent=2)
                print(f"Wrote report to {report_path}")
            except Exception as e:
                print(f"Failed to write report to {report_path}: {e}")

        return created

    elif out == "fs":
        if not out_dir:
            raise ValueError("out_dir must be provided for filesystem output mode")
        root = Path(out_dir)
        root.mkdir(parents=True, exist_ok=True)

        # Pre-create directories for all parents discovered in entries (plus canonical buckets)
        parents_set = set(_clean_label(p) for p, _, _, _, _ in entries) | set(PARENT_BUCKETS)
        for parent_name in parents_set:
            (root / _sanitize_filename(parent_name)).mkdir(parents=True, exist_ok=True)

        for parent, coach, course, link, _raw in entries:
            p_clean = _clean_label(parent)
            c_clean = _clean_label(coach)
            entry_report = {"parent": p_clean, "coach": c_clean, "course": course, "link": link, "raw": _raw, "result": None, "reason": None}

            pdir = root / _sanitize_filename(p_clean)
            pdir.mkdir(parents=True, exist_ok=True)

            coach_file = pdir / (_sanitize_filename(c_clean) + ".json")
            data = {"name": c_clean, "parent": p_clean, "path": f"{p_clean}/{c_clean}", "courses": []}
            existing = None
            original_meta = (None, None, None)
            if coach_file.exists():
                try:
                    with open(coach_file, "r", encoding="utf-8") as fh:
                        loaded = json.load(fh)
                        if isinstance(loaded, dict):
                            existing = loaded
                            original_meta = (loaded.get("parent"), loaded.get("name"), loaded.get("path"))
                except Exception:
                    # ignore parse errors and overwrite
                    existing = None

            # normalize stored metadata
            data["name"] = c_clean
            data["parent"] = p_clean
            data["path"] = f"{p_clean}/{c_clean}"

            # ensure courses list
            courses = data.get("courses") or []
            if any((c.get("name") or "").strip().lower() == course.strip().lower() for c in courses):
                print(f"    Skip existing course: {course} (coach={c_clean})")
                entry_report["result"] = "skipped"
                entry_report["reason"] = "already_exists"
                created["skipped"] += 1
                # If metadata changed compared to file on disk, update it even when no course changed
                if not dry_run and original_meta != (data.get("parent"), data.get("name"), data.get("path")):
                    try:
                        with open(coach_file, "w", encoding="utf-8") as fh:
                            json.dump(data, fh, ensure_ascii=False, indent=2)
                    except Exception as e:
                        print(f"    Error writing metadata to file {coach_file}: {e}")
                        entry_report["result"] = "error"
                        entry_report["reason"] = str(e)
                report_entries.append(entry_report)
                continue
            else:
                print(f"    Add course: {course} -> {link} (fs)")
                courses.append({"name": course, "link": link})
                data["courses"] = courses
                if not dry_run:
                    try:
                        with open(coach_file, "w", encoding="utf-8") as fh:
                            json.dump(data, fh, ensure_ascii=False, indent=2)
                        entry_report["result"] = "added"
                    except Exception as e:
                        print(f"    Error writing file {coach_file}: {e}")
                        created["skipped"] += 1
                        entry_report["result"] = "error"
                        entry_report["reason"] = str(e)
                        report_entries.append(entry_report)
                        continue
                else:
                    entry_report["result"] = "would_add"
                created["courses"] += 1
                report_entries.append(entry_report)

        # write report if requested
        if report_path:
            try:
                with open(report_path, "w", encoding="utf-8") as fh:
                    json.dump(report_entries, fh, ensure_ascii=False, indent=2)
                print(f"Wrote report to {report_path}")
            except Exception as e:
                print(f"Failed to write report to {report_path}: {e}")

        return created

    else:
        raise ValueError(f"Unknown output mode: {out}")


async def main():
    parser = argparse.ArgumentParser(description="Migrate plaintext export into categories/coaches/courses")
    parser.add_argument("--file", "-f", default="migrate data.txt", help="Path to the migrate data file")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB; just show operations")
    parser.add_argument("--out", choices=["db", "fs"], default="db", help="Destination: 'db' for MongoDB (default), 'fs' for filesystem JSON files")
    parser.add_argument("--out-dir", default="course_bot/categories", help="Output directory when using --out fs (default: course_bot/categories)")
    parser.add_argument("--grouped-out", help="Write grouped text output (new file) instead of/alongside migration")
    parser.add_argument("--group-only", action="store_true", help="Write grouped text file and exit (no DB/FS writes)")
    parser.add_argument("--report", help="Write per-entry JSON report to this path")
    parser.add_argument("--global-dedupe", action="store_true", help="Skip adding a course if it exists anywhere in DB")
    args = parser.parse_args()

    load_dotenv()

    if args.out == "db":
        mongo_uri = os.getenv("MONGODB_URL")
        db_name = os.getenv("MONGODB_NAME")
        if not mongo_uri or not db_name:
            print("Environment variables MONGODB_URL and MONGODB_NAME must be set (or in .env) for DB output. Aborting.")
            sys.exit(1)

        # Avoid printing the full connection string to stdout/logs
        print(f"Connecting to MongoDB (masked) / {db_name}...")
        await MongoDB.initialize(mongo_uri, db_name)

    print(f"Parsing {args.file}...")
    entries = parse_migrate_file(args.file)
    # If requested, write a grouped text file that places each original link line
    # under the best-matching parent header. This does not modify the original file.
    if args.grouped_out:
        write_grouped_file(entries, args.grouped_out)
        print(f"Wrote grouped output to {args.grouped_out}")
        if args.group_only:
            return
    print(f"Found {len(entries)} course entries.")
    if not entries:
        if args.out == "db":
            await MongoDB.close()
        return

    res = await migrate(entries, dry_run=args.dry_run, out=args.out, out_dir=args.out_dir, report_path=args.report, global_dedupe=args.global_dedupe)
    print("\nMigration summary:")
    for k, v in res.items():
        print(f"  {k}: {v}")

    if args.out == "db":
        await MongoDB.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Cancelled")
