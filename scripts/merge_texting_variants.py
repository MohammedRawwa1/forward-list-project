#!/usr/bin/env python3
"""Merge texting-variant coach documents into clean 'Texting' coach docs.

This script finds category documents whose `name` ends with "(Texting)" (case-insensitive),
creates/ensures a target coach document under parent 'Texting' with the base coach name
(without the suffix), moves courses from the variant into the target, removes duplicate
occurrences of those courses elsewhere, and deletes the variant doc. By default the script
performs a dry-run and writes a JSON report. Use `--apply` to actually modify the DB.

Usage examples:
  # Dry-run (default)
  python scripts/merge_texting_variants.py --report merge_texting_report.json

  # Apply changes
  python scripts/merge_texting_variants.py --report merge_texting_report.json --apply --delete-empty

Be careful: this modifies `categories` collection. Run with `--apply` only when ready.
"""

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
try:
    # ensure repository root is on sys.path so `database` package can be imported
    _repo_root = Path(__file__).resolve().parent.parent
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))
except Exception:
    pass
from dotenv import load_dotenv
from database.mongo_handler import MongoDB


async def main():
    parser = argparse.ArgumentParser(description="Merge '(Texting)' coach variants into Texting parent")
    parser.add_argument("--apply", action="store_true", help="Apply changes to the DB (default is dry-run)")
    parser.add_argument("--delete-empty", action="store_true", help="Delete category docs that become empty after moving courses")
    parser.add_argument("--report", default="merge_texting_report.json", help="Report JSON path")
    args = parser.parse_args()

    load_dotenv()
    mongo_uri = os.getenv("MONGODB_URL")
    db_name = os.getenv("MONGODB_NAME")
    if not mongo_uri or not db_name:
        print("MONGODB_URL and MONGODB_NAME must be set in environment or .env")
        return 1

    print(f"Connecting to MongoDB (masked)/{db_name}...")
    await MongoDB.initialize(mongo_uri, db_name)
    db = await MongoDB.get_db()
    coll = db["categories"]

    regex = r"\s*\(texting\)\s*$"
    variants = await coll.find({"name": {"$regex": regex, "$options": "i"}}).to_list(length=10000)
    print(f"Found {len(variants)} variant coach docs matching '(Texting)'.")

    report = []

    for var in variants:
        name = var.get("name")
        variant_id = var.get("_id")
        base = re.sub(regex, "", name, flags=re.I).strip()
        entry = {"variant_name": name, "base_name": base, "variant_id": str(variant_id), "moved": 0, "removed_duplicates": 0, "created_target": False, "deleted_variant": False}

        # Ensure target doc under Texting exists (create if missing)
        target = await coll.find_one({"name": base, "parent": "Texting"})
        if not target:
            if args.apply:
                await coll.update_one({"name": base, "parent": "Texting"}, {"$setOnInsert": {"name": base, "parent": "Texting", "path": f"Texting/{base}", "courses": []}}, upsert=True)
            entry["created_target"] = True
            target = await coll.find_one({"name": base, "parent": "Texting"})

        target_id = target.get("_id") if target else None

        var_courses = var.get("courses") or []
        for course in var_courses:
            cname = course.get("name")

            # Remove duplicate occurrences elsewhere (exclude variant and target)
            q = {"courses.name": cname}
            exclude_ids = [variant_id]
            if target_id:
                exclude_ids.append(target_id)
            q["_id"] = {"$nin": exclude_ids}
            if args.apply:
                res = await coll.update_many(q, {"$pull": {"courses": {"name": cname}}})
                entry["removed_duplicates"] += getattr(res, "modified_count", 0)
            else:
                # estimate: count matching docs
                cnt = await coll.count_documents({"courses.name": cname, "_id": {"$nin": exclude_ids}})
                entry["removed_duplicates"] += int(cnt)

            # Add course to target using $addToSet to avoid duplicate
            if args.apply:
                await coll.update_one({"name": base, "parent": "Texting"}, {"$addToSet": {"courses": course}})
            entry["moved"] += 1

        # Delete the variant doc
        if args.apply:
            res = await coll.delete_one({"_id": variant_id})
            entry["deleted_variant"] = getattr(res, "deleted_count", 0) > 0

        report.append(entry)

    # Optionally delete empty docs
    deleted_empty_count = 0
    if args.delete_empty and args.apply:
        res = await coll.delete_many({"$or": [{"courses": {"$exists": False}}, {"courses": {"$size": 0}}]})
        deleted_empty_count = getattr(res, "deleted_count", 0)

    summary = {"variants_processed": len(variants), "deleted_empty_count": deleted_empty_count, "entries": report}
    with open(args.report, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)

    print(f"Wrote report to {args.report}")
    await MongoDB.close()
    return 0


if __name__ == "__main__":
    asyncio.run(main())
