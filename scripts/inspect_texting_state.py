#!/usr/bin/env python3
"""Inspect Texting merge state and surface discrepancies.

This connects to MongoDB, reads `merge_texting_report.json` (if present)
for the list of processed base coach names, and reports:
  - number of variant docs remaining (name ends with '(Texting)')
  - number of coach docs under parent 'Texting'
  - per-base target existence and course counts
  - any duplicate occurrences of course names outside the target doc

Writes a JSON report (default: `inspect_texting_report.json`).

Usage:
  python scripts/inspect_texting_state.py --report inspect_texting_report.json

Requires MONGODB_URL and MONGODB_NAME in environment or .env.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

try:
    _repo_root = Path(__file__).resolve().parent.parent
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))
except Exception:
    pass

from dotenv import load_dotenv
from database.mongo_handler import MongoDB


async def run(report_path, merge_report_path):
    load_dotenv()
    mongo_uri = os.getenv('MONGODB_URL')
    db_name = os.getenv('MONGODB_NAME')
    if not mongo_uri or not db_name:
        print('MONGODB_URL and MONGODB_NAME must be set in environment or .env')
        return 2

    print(f"Connecting to MongoDB (masked)/{db_name}...")
    await MongoDB.initialize(mongo_uri, db_name)
    db = await MongoDB.get_db()
    coll = db['categories']

    # global counts
    variants_count = await coll.count_documents({"name": {"$regex": r"\(Texting\)\s*$", "$options": "i"}})
    texting_targets = await coll.find({"parent": "Texting"}, {"name": 1, "courses": 1}).to_list(length=10000)
    texting_target_names = [d.get('name') for d in texting_targets]

    merge_bases = []
    merge_expected = {}
    if Path(merge_report_path).exists():
        try:
            with open(merge_report_path, 'r', encoding='utf-8') as fh:
                mr = json.load(fh)
            for e in mr.get('entries', []):
                base = e.get('base_name')
                if base:
                    merge_bases.append(base)
                    merge_expected[base] = {'moved': int(e.get('moved', 0)), 'removed_duplicates': int(e.get('removed_duplicates', 0))}
        except Exception:
            merge_bases = []

    per_base = {}
    # If we have merge_bases, inspect those; otherwise inspect all Texting targets
    targets_to_check = merge_bases or texting_target_names
    for base in targets_to_check:
        rec = {"base": base, "target_found": False, "target_courses_count": 0, "duplicate_courses": []}
        target_doc = await coll.find_one({"name": base, "parent": "Texting"})
        if not target_doc:
            rec['target_found'] = False
            # maybe the target exists but with different path; search by regex name exact
            alt = await coll.find_one({"name": {"$regex": f"^{base}$", "$options": "i"}, "parent": "Texting"})
            if alt:
                target_doc = alt
        if target_doc:
            rec['target_found'] = True
            t_id = target_doc.get('_id')
            courses = target_doc.get('courses') or []
            rec['target_courses_count'] = len(courses)
            # check duplicates for each course name
            dup_list = []
            for c in courses:
                cname = c.get('name')
                if not cname:
                    continue
                cnt = await coll.count_documents({"courses.name": cname})
                other_cnt = await coll.count_documents({"courses.name": cname, "_id": {"$ne": t_id}})
                if other_cnt > 0:
                    # gather other doc names for context (limit to 20)
                    others = await coll.find({"courses.name": cname, "_id": {"$ne": t_id}}, {"name": 1}).to_list(length=50)
                    other_names = [o.get('name') for o in others]
                    dup_list.append({"course": cname, "total_occurrences": int(cnt), "other_occurrences": int(other_cnt), "other_docs": other_names})
            rec['duplicate_courses'] = dup_list
        else:
            rec['note'] = 'target_not_found'
        per_base[base] = rec

    # summary: count of Texting targets and some totals
    total_target_count = len(texting_targets)
    total_moved_expected = sum(v.get('moved', 0) for v in merge_expected.values()) if merge_expected else None
    total_removed_expected = sum(v.get('removed_duplicates', 0) for v in merge_expected.values()) if merge_expected else None

    out = {
        'variants_count_remaining': int(variants_count),
        'texting_targets_count': int(total_target_count),
        'texting_target_names': texting_target_names,
        'merge_report_bases': merge_bases,
        'merge_expected_summary': {'total_moved_expected': total_moved_expected, 'total_removed_expected': total_removed_expected},
        'per_base': per_base,
    }

    try:
        with open(report_path, 'w', encoding='utf-8') as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
        print(f"Wrote report to {report_path}")
    except Exception as e:
        print(f"Failed to write report: {e}")

    await MongoDB.close()
    return 0


def cli():
    parser = argparse.ArgumentParser(description='Inspect Texting merge state')
    parser.add_argument('--report', default='inspect_texting_report.json', help='Output JSON report path')
    parser.add_argument('--merge-report', default='merge_texting_report.json', help='Path to merge_texting_report.json (optional)')
    args = parser.parse_args()
    try:
        return asyncio.run(run(args.report, args.merge_report))
    except KeyboardInterrupt:
        print('Cancelled')
        return 2


if __name__ == '__main__':
    sys.exit(cli())