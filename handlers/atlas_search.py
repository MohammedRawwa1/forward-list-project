"""
MongoDB Atlas Search integration with graceful fallback to regex-based search.

This module provides:
- A runtime check to detect whether Atlas Search is available (env var + Atlas URI detection)
- Parallel implementations of the 3 search types (categories, courses, category-courses)
- Graceful fallback to the original $regex-based approach when Atlas Search is unavailable
- Shared helpers for building Atlas Search pipelines with fuzzy matching, scoring, and pagination

Prerequisites (Atlas Search):
  1. Your MongoDB connection string must point to an Atlas cluster (starts with mongodb+srv://).
  2. Create an Atlas Search index on the `categories` collection:
     - Index name: "default" (or set ATLAS_SEARCH_INDEX_NAME env var)
     - Dynamic mapping: true (or specific field mapping for name, courses.name)
  3. Set `USE_ATLAS_SEARCH=true` in your .env file.

Atlas Search Index JSON definition (create in Atlas UI → Search → Create Index):
  {
    "mappings": {
      "dynamic": false,
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
"""

import logging
import os
import re as re_module

from handlers.base_handlers import _get_total_count

logger = logging.getLogger(__name__)

# ---------------  Configuration  ---------------

def is_atlas_search_enabled() -> bool:
    """Check if Atlas Search should be used.

    Returns True when:
      1. USE_ATLAS_SEARCH env var is 'true'/'1'/'yes' (case-insensitive)
      2. The MONGODB_URL points to an Atlas cluster (mongodb+srv://)

    If USE_ATLAS_SEARCH is explicitly set but the URI is not an Atlas URI,
    a warning is logged because $search only works on Atlas.
    """
    flag = os.getenv("USE_ATLAS_SEARCH", "").strip().lower()
    if flag not in ("true", "1", "yes"):
        return False

    uri = os.getenv("MONGODB_URL", "")
    if "mongodb+srv://" not in uri:
        logger.warning(
            "USE_ATLAS_SEARCH=true but MONGODB_URL does not look like an Atlas URI "
            "(expected mongodb+srv://). Atlas Search will be disabled."
        )
        return False
    return True


def get_search_index_name() -> str:
    """Return the Atlas Search index name (default: 'default')."""
    return os.getenv("ATLAS_SEARCH_INDEX_NAME", "default")



# ---------------  Pipeline Builders  ---------------

def build_category_search_pipeline(
    query_text: str, 
    page: int = 1, 
    page_size: int = 50,
    index_name: str = "default",
    fuzzy: bool = True,
) -> dict:
    """Build an Atlas Search pipeline for category name search.

    Returns a dict with keys:
      - count_pipeline: list of stages for getting total count
      - data_pipeline: list of stages for fetching the page
      - use_atlas: True
    """
    # Build the $search stage
    search_stage = _make_text_search_stage(query_text, "name", index_name, fuzzy)

    # TOP_LEVEL_FILTER: parent does not exist, is null, or is empty
    top_level_filter = {"$or": [{"parent": {"$exists": False}}, {"parent": None}, {"parent": ""}]}

    start = (page - 1) * page_size

    # Count pipeline (no skip/limit)
    count_pipeline = [
        search_stage,
        {"$match": top_level_filter},
        {"$count": "total"},
    ]

    # Data pipeline with pagination
    data_pipeline = [
        search_stage,
        {"$addFields": {"_search_score": {"$meta": "searchScore"}}},
        {"$match": top_level_filter},
        {"$sort": {"_search_score": -1, "name": 1}},
        {"$skip": start},
        {"$limit": page_size + 1},
    ]

    return {
        "count_pipeline": count_pipeline,
        "data_pipeline": data_pipeline,
        "use_atlas": True,
    }


def build_course_search_pipeline(
    query_text: str,
    page: int = 1,
    page_size: int = 50,
    index_name: str = "default",
    fuzzy: bool = True,
) -> dict:
    """Build an Atlas Search pipeline for global course name search.

    Returns dict with count_pipeline, data_pipeline, use_atlas.
    The data_pipeline returns individual course docs with name/link/category/coach/id.
    """
    pattern = re_module.escape(query_text)
    search_stage = _make_text_search_stage(query_text, "courses.name", index_name, fuzzy)
    start = (page - 1) * page_size

    # Count pipeline
    count_pipeline = [
        search_stage,
        {"$unwind": "$courses"},
        # Still need $match after unwind to filter to matching courses only
        {"$match": {"courses.name": {"$regex": pattern, "$options": "i"}}},
        {"$count": "total"},
    ]

    # Data pipeline
    data_pipeline = [
        search_stage,
        {"$addFields": {"_search_score": {"$meta": "searchScore"}}},
        {"$unwind": "$courses"},
        {"$match": {"courses.name": {"$regex": pattern, "$options": "i"}}},
        {"$sort": {"_search_score": -1, "courses.name": 1}},
        {"$project": {
            "name": "$courses.name",
            "link": "$courses.link",
            "category": "$name",
            "coach": "$courses.coach",
            "id": "$courses.id",
        }},
        {"$skip": start},
        {"$limit": page_size + 1},
    ]

    return {
        "count_pipeline": count_pipeline,
        "data_pipeline": data_pipeline,
        "use_atlas": True,
    }


def build_category_course_search_pipeline(
    query_text: str,
    category: str,
    page: int = 1,
    page_size: int = 50,
    index_name: str = "default",
    fuzzy: bool = True,
) -> dict:
    """Build an Atlas Search pipeline for course search within a specific category.

    Uses compound $search with must + filter to restrict to the given category.
    Returns dict with count_pipeline, data_pipeline, use_atlas.
    """
    pattern = re_module.escape(query_text)
    start = (page - 1) * page_size

    # Use compound operator: must (text search) + filter (category equality)
    search_stage = {
        "$search": {
            "index": index_name,
            "compound": {
                "must": [{
                    "text": {
                        "query": query_text,
                        "path": "courses.name",
                        "fuzzy": {"maxEdits": 1} if fuzzy else {},
                    }
                }],
                "filter": [{
                    "phrase": {
                        "query": category,
                        "path": "name",
                    }
                }],
            },
        }
    }

    if not fuzzy:
        search_stage["$search"]["compound"]["must"][0]["text"].pop("fuzzy", None)

    # Count pipeline
    count_pipeline = [
        search_stage,
        {"$unwind": "$courses"},
        {"$match": {"courses.name": {"$regex": pattern, "$options": "i"}}},
        {"$count": "total"},
    ]

    # Data pipeline
    data_pipeline = [
        search_stage,
        {"$addFields": {"_search_score": {"$meta": "searchScore"}}},
        {"$unwind": "$courses"},
        {"$match": {"courses.name": {"$regex": pattern, "$options": "i"}}},
        {"$sort": {"_search_score": -1, "courses.name": 1}},
        {"$project": {
            "name": "$courses.name",
            "link": "$courses.link",
            "category": "$name",
            "coach": "$courses.coach",
            "id": "$courses.id",
        }},
        {"$skip": start},
        {"$limit": page_size + 1},
    ]

    return {
        "count_pipeline": count_pipeline,
        "data_pipeline": data_pipeline,
        "use_atlas": True,
}


# ---------------  Regex Fallback Pipeline Builders  ---------------

def build_regex_category_search_pipeline(
    query_text: str,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """Build a regex-based pipeline for category search (fallback when Atlas is unavailable)."""
    pattern = re_module.escape(query_text)
    top_level_filter = {"$or": [{"parent": {"$exists": False}}, {"parent": None}, {"parent": ""}]}
    filter_q = {"$and": [top_level_filter, {"name": {"$regex": pattern, "$options": "i"}}]}
    start = (page - 1) * page_size

    return {
        "filter_q": filter_q,
        "data_fn": lambda db: db.categories.find(filter_q).sort("name", 1).skip(start).limit(page_size + 1).to_list(length=page_size + 1),
        "use_atlas": False,
    }


def build_regex_course_search_pipeline(
    query_text: str,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """Build a regex-based pipeline for global course search (fallback)."""
    pattern = re_module.escape(query_text)

    pipeline = [
        {"$unwind": "$courses"},
        {"$match": {"courses.name": {"$regex": pattern, "$options": "i"}}},
        {"$project": {
            "name": "$courses.name",
            "link": "$courses.link",
            "category": "$name",
            "coach": "$courses.coach",
            "id": "$courses.id",
        }},
        {"$sort": {"name": 1}},
    ]

    return {
        "pipeline_base": pipeline,
        "use_atlas": False,
    }


def build_regex_category_course_search_pipeline(
    query_text: str,
    category: str,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """Build a regex-based pipeline for category-specific course search (fallback)."""
    pattern = re_module.escape(query_text)

    pipeline = [
        {"$match": {"$or": [{"name": category}, {"path": category}]}},
        {"$unwind": "$courses"},
        {"$match": {"courses.name": {"$regex": pattern, "$options": "i"}}},
        {"$project": {
            "name": "$courses.name",
            "link": "$courses.link",
            "category": "$name",
            "coach": "$courses.coach",
            "id": "$courses.id",
        }},
        {"$sort": {"name": 1}},
    ]

    return {
        "pipeline_base": pipeline,
        "use_atlas": False,
    }


# ---------------  Execution Helpers  ---------------

async def execute_category_search(
    db,
    query_text: str,
    page: int = 1,
    page_size: int = 50,
):
    """Execute a category search, using Atlas Search when available.

    Returns (categories_list, total_count, have_more).
    """
    if is_atlas_search_enabled():
        index_name = get_search_index_name()
        try:
            pipes = build_category_search_pipeline(query_text, page, page_size, index_name)
            # Count
            cnt_res = await db.categories.aggregate(pipes["count_pipeline"]).to_list(length=1)
            total = cnt_res[0]["total"] if cnt_res else 0

            # Data
            docs = await db.categories.aggregate(pipes["data_pipeline"]).to_list(length=page_size + 1)
            have_more = len(docs) > page_size
            page_cats = docs[:page_size]
            # Remove _search_score field from results
            for c in page_cats:
                c.pop("_search_score", None)
            return page_cats, total, have_more
        except Exception as e:
            logger.warning("Atlas Search failed for categories, falling back to regex: %s", e)
            # Fall through to regex

    # Regex fallback
    pipes = build_regex_category_search_pipeline(query_text, page, page_size)
    total = await _get_total_count(db, "categories", pipes["filter_q"], ttl=10)
    cats = await pipes["data_fn"](db)
    have_more = len(cats) > page_size
    page_cats = cats[:page_size]
    return page_cats, total, have_more


async def execute_course_search(
    db,
    query_text: str,
    page: int = 1,
    page_size: int = 50,
):
    """Execute a global course search, using Atlas Search when available.

    Returns (course_items, total_count, have_more).
    """
    if is_atlas_search_enabled():
        index_name = get_search_index_name()
        try:
            pipes = build_course_search_pipeline(query_text, page, page_size, index_name)

            # Count
            cnt_res = await db.categories.aggregate(pipes["count_pipeline"]).to_list(length=1)
            total = cnt_res[0]["total"] if cnt_res else 0

            # Data
            items = await db.categories.aggregate(pipes["data_pipeline"]).to_list(length=page_size + 1)
            have_more = len(items) > page_size
            course_items = items[:page_size]
            return course_items, total, have_more
        except Exception as e:
            logger.warning("Atlas Search failed for courses, falling back to regex: %s", e)

    # Regex fallback
    pipes = build_regex_course_search_pipeline(query_text, page, page_size)
    pipeline = pipes["pipeline_base"]

    cnt_res = await db.categories.aggregate(pipeline + [{"$count": "total"}]).to_list(length=1)
    total = cnt_res[0]["total"] if cnt_res else 0
    start = (page - 1) * page_size
    paged_pipeline = pipeline + [{"$skip": start}, {"$limit": page_size + 1}]
    items = await db.categories.aggregate(paged_pipeline).to_list(length=page_size + 1)
    have_more = len(items) > page_size
    course_items = items[:page_size]
    return course_items, total, have_more


async def execute_category_course_search(
    db,
    query_text: str,
    category: str,
    page: int = 1,
    page_size: int = 50,
):
    """Execute a category-specific course search, using Atlas Search when available.

    Returns (course_items, total_count, have_more).
    """
    if is_atlas_search_enabled():
        index_name = get_search_index_name()
        try:
            pipes = build_category_course_search_pipeline(query_text, category, page, page_size, index_name)

            # Count
            cnt_res = await db.categories.aggregate(pipes["count_pipeline"]).to_list(length=1)
            total = cnt_res[0]["total"] if cnt_res else 0

            # Data
            items = await db.categories.aggregate(pipes["data_pipeline"]).to_list(length=page_size + 1)
            have_more = len(items) > page_size
            course_items = items[:page_size]
            return course_items, total, have_more
        except Exception as e:
            logger.warning("Atlas Search failed for category courses, falling back to regex: %s", e)

    # Regex fallback
    pipes = build_regex_category_course_search_pipeline(query_text, category, page, page_size)
    pipeline = pipes["pipeline_base"]
    cnt_res = await db.categories.aggregate(pipeline + [{"$count": "total"}]).to_list(length=1)
    total = cnt_res[0]["total"] if cnt_res else 0
    start = (page - 1) * page_size
    paged_pipeline = pipeline + [{"$skip": start}, {"$limit": page_size + 1}]
    items = await db.categories.aggregate(paged_pipeline).to_list(length=page_size + 1)
    have_more = len(items) > page_size
    course_items = items[:page_size]
    return course_items, total, have_more


# ---------------  Internal Helpers  ---------------

def _make_text_search_stage(query_text: str, path: str, index_name: str, fuzzy: bool = True) -> dict:
    """Build a $search stage with text operator."""
    stage = {
        "$search": {
            "index": index_name,
            "text": {
                "query": query_text,
                "path": path,
            },
        }
    }
    if fuzzy:
        stage["$search"]["text"]["fuzzy"] = {"maxEdits": 1, "prefixLength": 2}
    return stage



