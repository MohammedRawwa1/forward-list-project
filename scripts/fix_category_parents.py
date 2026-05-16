#!/usr/bin/env python3
"""Repair helper: detect and (optionally) fix category `parent`/`path` mismatches.

This inspects every document in the `categories` collection and checks for
inconsistencies between the stored `path` and `parent` fields. If a document
has a `path` like "Parent/Child" but `parent` is missing or different, the
script reports it and (with `--apply`) updates the `parent` to match the
path. It can also populate missing `path` values from `parent`+`name` when
appropriate.

Dry-run is the default. Use `--apply` to make changes and `--report` to write
a JSON summary.

Usage:
  python scripts/fix_category_parents.py --report fix_parents_report.json
  python scripts/fix_category_parents.py --apply --report fix_parents_report.json
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


def analyze_doc(doc):
    """Return (expected_parent, expected_path) based on `path` and `name`."""
    name = doc.get('name')
    parent = doc.get('parent')
    path = doc.get('path')
    expected_parent = None
    expected_path = None
    if path and isinstance(path, str) and '/' in path:
        parts = path.split('/', 1)
        expected_parent = parts[0].strip() or None
        # normalize expected_path to Parent/Name
        expected_path = path.strip()
    else:
        # if path missing but parent present, we can compute one
        if parent and name:
            expected_parent = parent
            expected_path = f"{parent}/{name}"
        else:
            expected_parent = parent
            expected_path = path
    return expected_parent, expected_path


async def main():
    parser = argparse.ArgumentParser(description="Repair category parent/path mismatches")
    parser.add_argument('--apply', action='store_true', help='Apply changes to the DB')
    parser.add_argument('--report', default='fix_parents_report.json', help='Write JSON report to this path')
    parser.add_argument('--limit', type=int, default=0, help='Limit number of docs to inspect (0 = all)')
    args = parser.parse_args()

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

    cursor = coll.find({})
    total = await coll.count_documents({})
    if args.limit and args.limit > 0:
        cursor = coll.find({}).limit(args.limit)

    report = {'inspected': 0, 'to_fix': 0, 'fixed': 0, 'details': []}

    async for doc in cursor:
        report['inspected'] += 1
        doc_id = str(doc.get('_id'))
        name = doc.get('name')
        parent = doc.get('parent')
        path = doc.get('path')
        expected_parent, expected_path = analyze_doc(doc)

        needs_parent_fix = False
        needs_path_fix = False
        fixes = {}

        # If expected_parent is derived from path and differs from stored parent
        if expected_parent and expected_parent != parent:
            needs_parent_fix = True
            fixes['parent'] = {'old': parent, 'new': expected_parent}

        # If path is missing or doesn't match expected_path, offer fixing
        if expected_path and expected_path != path:
            # Avoid overwriting intentional different paths for top-level
            # categories (where expected_path == name). Only set when parent is present.
            needs_path_fix = True
            fixes['path'] = {'old': path, 'new': expected_path}

        if needs_parent_fix or needs_path_fix:
            report['to_fix'] += 1
            entry = {'_id': doc_id, 'name': name, 'parent': parent, 'path': path, 'fixes': fixes}
            report['details'].append(entry)
            if args.apply:
                update = {}
                if needs_parent_fix:
                    update['parent'] = expected_parent
                if needs_path_fix:
                    update['path'] = expected_path
                try:
                    await coll.update_one({'_id': doc.get('_id')}, {'$set': update})
                    report['fixed'] += 1
                    entry['applied'] = update
                except Exception as e:
                    entry['error'] = str(e)

    # Optionally, ensure top-level parent docs exist for any referenced parents
    referenced_parents = {d.get('new') for d in [f['fixes'].get('parent') for f in report['details'] if 'parent' in f['fixes']]} if report['details'] else set()
    # referenced_parents is a set of dicts; normalize to names
    normalized = set()
    for p in referenced_parents:
        if isinstance(p, dict):
            normalized.add(p.get('new'))
        elif isinstance(p, str):
            normalized.add(p)
    referenced_parents = {x for x in normalized if x}

    parents_created = 0
    if args.apply and referenced_parents:
        for pname in referenced_parents:
            # Ensure a parent doc exists with parent==None
            existing = await coll.find_one({'name': pname})
            if not existing:
                try:
                    await coll.update_one({'name': pname}, {'$setOnInsert': {'name': pname, 'parent': None, 'path': pname, 'courses': []}}, upsert=True)
                    parents_created += 1
                except Exception:
                    pass

    summary = {'inspected': report['inspected'], 'to_fix': report['to_fix'], 'fixed': report['fixed'], 'parents_created': parents_created}
    out = {'summary': summary, 'details': report['details']}
    try:
        with open(args.report, 'w', encoding='utf-8') as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
        print(f"Wrote report to {args.report}")
    except Exception as e:
        print(f"Failed to write report: {e}")

    await MongoDB.close()
    if args.apply:
        print(f"Applied fixes: {report['fixed']} (inspected {report['inspected']})")
    else:
        print(f"Dry-run: would fix {report['to_fix']} of {report['inspected']} inspected documents")
    return 0


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('Cancelled')
