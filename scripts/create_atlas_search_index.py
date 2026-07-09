"""
MongoDB Atlas Search Index Creator

This script provides:
1. The JSON index definition to paste into the Atlas UI
2. An optional automated approach using the Atlas Admin API

Prerequisites:
  - MongoDB Atlas cluster (mongodb+srv:// connection string)
  - Atlas Search is a paid feature (M10+ clusters)

Usage:
  python scripts/create_atlas_search_index.py

Then follow the printed instructions to create the index in the Atlas UI.
"""

import os
import json
from dotenv import load_dotenv

load_dotenv()

INDEX_DEFINITION = {
    "mappings": {
        "dynamic": False,
        "fields": {
            "name": [
                {"type": "string", "analyzer": "lucene.standard"}
            ],
            "courses": [
                {
                    "type": "embeddedDocuments",
                    "fields": {
                        "name": [
                            {"type": "string", "analyzer": "lucene.standard"}
                        ],
                        "coach": [
                            {"type": "string", "analyzer": "lucene.standard"}
                        ]
                    }
                }
            ],
            "parent": [
                {"type": "string"}
            ]
        }
    }
}


def print_instructions():
    """Print step-by-step instructions for creating the Atlas Search index."""
    mongo_uri = os.getenv("MONGODB_URL", "")
    db_name = os.getenv("MONGODB_NAME", "forward_list")
    index_name = os.getenv("ATLAS_SEARCH_INDEX_NAME", "default")

    print("=" * 65)
    print("  MongoDB Atlas Search Index Setup")
    print("=" * 65)
    print()

    if "mongodb+srv://" not in mongo_uri:
        print("⚠️  WARNING: Your MONGODB_URL does not use mongodb+srv:// protocol.")
        print("   Atlas Search only works on Atlas clusters.")
        print("   You need an Atlas cluster (free M0 or paid M10+) to use Atlas Search.")
        print()
        print("   Get started: https://www.mongodb.com/atlas")
        print()
        return

    print(f"Database: {db_name}")
    print(f"Collection: categories")
    print(f"Index name: {index_name}")
    print()

    print("=" * 65)
    print("  Step 1: Create the Search Index in Atlas UI")
    print("=" * 65)
    print()
    print("  1. Log into https://cloud.mongodb.com")
    print("  2. Go to your cluster")
    print(f"  3. Click the 'Search' tab")
    print("  4. Click 'Create Search Index'")
    print("  5. Choose 'JSON Editor'")
    print("  6. Paste the following index definition:")
    print()
    print(json.dumps(INDEX_DEFINITION, indent=2))
    print()
    print("  7. Click 'Next' then 'Create Search Index'")
    print("  8. Wait for the index to reach 'Active' status (may take a few minutes)")
    print()

    print("=" * 65)
    print("  Step 2: Enable in .env")
    print("=" * 65)
    print()
    print("  Add these to your .env file:")
    print()
    print(f"  USE_ATLAS_SEARCH=true")
    print(f"  ATLAS_SEARCH_INDEX_NAME={index_name}")
    print()

    print("=" * 65)
    print("  Step 3: Verify")
    print("=" * 65)
    print()
    print("  The bot will automatically detect Atlas Search and use it when:")
    print("  - USE_ATLAS_SEARCH=true is set")
    print("  - MONGODB_URL uses mongodb+srv://")
    print()
    print("  If Atlas Search fails (e.g., index not ready), it gracefully")
    print("  falls back to the original $regex-based search.")
    print()
    print("  To verify it's working, check the logs for:")
    print('    "Atlas Search failed..." (fallback to regex)')
    print()

    print("=" * 65)
    print("  Index Definition Reference")
    print("=" * 65)
    print()
    print("  Fields indexed for search:")
    print("  - name: Category name (lucene.standard analyzer)")
    print("  - courses.name: Course names within embedded documents")
    print("  - courses.coach: Coach names within embedded documents")
    print("  - parent: Exact-match for parent categories")
    print()
    print("  The fuzzy search uses: maxEdits=1, prefixLength=2")
    print("  (handles 1-character typos with 2-char prefix precision)")


if __name__ == "__main__":
    print_instructions()
