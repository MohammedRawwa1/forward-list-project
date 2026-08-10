import logging
import os
import re as re_module

from handlers.base_handlers import _get_total_count

logger = logging.getLogger(__name__)

# ---------------  Configuration  ---------------


def is_atlas_search_enabled() -> bool:
    flag = os.getenv("USE_ATLAS_SEARCH", "").strip().lower()
    if flag not in ("true", "1", "yes"):
        return False

    uri = os.getenv("MONGODB_URL", "")
    if "mongodb+srv://" not in uri:
        logger.warning(
            "USE_ATLAS_SEARCH=true but MONGODB_URL does not look like an Atlas URI "
            "(expected mongodb+srv://). Atlas Search will be disabled.",
        )
        return False
    return True


def get_search_index_name() -> str:
    return os.getenv("ATLAS_SEARCH_INDEX_NAME", "default")


# ---------------  Pipeline Builders  ---------------


def build_category_search_pipeline(
    query_text: str,
    page: int = 1,
    page_size: int = 50,
    index_name: str = "default",
    fuzzy: bool = True,
    parent: str = None,
) -> dict:
    search_stage = _make_text_search_stage(query_text, "name", index_name, fuzzy)

    if parent:
        scope_filter = {"parent": parent}
    else:
        scope_filter = None

    start = (page - 1) * page_size

    if scope_filter:
        count_pipeline = [
            search_stage,
            {"$match": scope_filter},
            {"$count": "total"},
        ]
        data_pipeline = [
            search_stage,
            {"$addFields": {"_search_score": {"$meta": "searchScore"}}},
            {"$match": scope_filter},
            {"$sort": {"_search_score": -1, "name": 1}},
            {"$skip": start},
            {"$limit": page_size + 1},
        ]
    else:
        count_pipeline = [
            search_stage,
            {"$count": "total"},
        ]
        data_pipeline = [
            search_stage,
            {"$addFields": {"_search_score": {"$meta": "searchScore"}}},
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
    pattern = re_module.escape(query_text)
    search_stage = _make_text_search_stage(query_text, "courses.name", index_name, fuzzy)
    start = (page - 1) * page_size

    count_pipeline = [
        search_stage,
        {"$unwind": "$courses"},
        {"$match": {"courses.name": {"$regex": pattern, "$options": "i"}}},
        {"$count": "total"},
    ]

    data_pipeline = [
        search_stage,
        {"$addFields": {"_search_score": {"$meta": "searchScore"}}},
        {"$unwind": "$courses"},
        {"$match": {"courses.name": {"$regex": pattern, "$options": "i"}}},
        {"$sort": {"_search_score": -1, "courses.name": 1}},
        {
            "$project": {
                "name": "$courses.name",
                "link": "$courses.link",
                "category": "$name",
                "coach": "$courses.coach",
                "id": "$courses.id",
            },
        },
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
    include_children: bool = True,
) -> dict:
    pattern = re_module.escape(query_text)
    start = (page - 1) * page_size

    if include_children:
        filter_clause = {
            "should": [
                {"phrase": {"query": category, "path": "name"}},
                {"phrase": {"query": category, "path": "parent"}},
            ],
            "minimumShouldMatch": 1,
        }
        search_stage = {
            "$search": {
                "index": index_name,
                "compound": {
                    "must": [
                        {
                            "text": {
                                "query": query_text,
                                "path": "courses.name",
                                "fuzzy": {"maxEdits": 1, "prefixLength": _fuzzy_prefix_length(query_text)} if fuzzy else {},
                            },
                        },
                    ],
                    "filter": [{"compound": filter_clause}],
                },
            },
        }
    else:
        search_stage = {
            "$search": {
                "index": index_name,
                "compound": {
                    "must": [
                        {
                            "text": {
                                "query": query_text,
                                "path": "courses.name",
                                "fuzzy": {"maxEdits": 1, "prefixLength": _fuzzy_prefix_length(query_text)} if fuzzy else {},
                            },
                        },
                    ],
                    "filter": [
                        {
                            "phrase": {
                                "query": category,
                                "path": "name",
                            },
                        },
                    ],
                },
            },
        }

    if not fuzzy:
        search_stage["$search"]["compound"]["must"][0]["text"].pop("fuzzy", None)

    count_pipeline = [
        search_stage,
        {"$unwind": "$courses"},
        {"$match": {"courses.name": {"$regex": pattern, "$options": "i"}}},
        {"$count": "total"},
    ]

    data_pipeline = [
        search_stage,
        {"$addFields": {"_search_score": {"$meta": "searchScore"}}},
        {"$unwind": "$courses"},
        {"$match": {"courses.name": {"$regex": pattern, "$options": "i"}}},
        {"$sort": {"_search_score": -1, "courses.name": 1}},
        {
            "$project": {
                "name": "$courses.name",
                "link": "$courses.link",
                "category": "$name",
                "coach": "$courses.coach",
                "id": "$courses.id",
            },
        },
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
    parent: str = None,
) -> dict:
    pattern = re_module.escape(query_text)

    if parent:
        scope_filter = {"parent": parent}
        filter_q = {"$and": [scope_filter, {"name": {"$regex": pattern, "$options": "i"}}]}
    else:
        filter_q = {"name": {"$regex": pattern, "$options": "i"}}

    start = (page - 1) * page_size

    return {
        "filter_q": filter_q,
        "data_fn": lambda db: (
            db.categories.find(filter_q).sort("name", 1).skip(start).limit(page_size + 1).to_list(length=page_size + 1)
        ),
        "use_atlas": False,
    }


def build_regex_course_search_pipeline(
    query_text: str,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    pattern = re_module.escape(query_text)

    pipeline = [
        {"$unwind": "$courses"},
        {"$match": {"courses.name": {"$regex": pattern, "$options": "i"}}},
        {
            "$project": {
                "name": "$courses.name",
                "link": "$courses.link",
                "category": "$name",
                "coach": "$courses.coach",
                "id": "$courses.id",
            },
        },
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
    include_children: bool = True,
) -> dict:
    pattern = re_module.escape(query_text)

    if include_children:
        pipeline = [
            {"$match": {"$or": [{"name": category}, {"parent": category}]}},
            {"$unwind": "$courses"},
            {"$match": {"courses.name": {"$regex": pattern, "$options": "i"}}},
            {
                "$project": {
                    "name": "$courses.name",
                    "link": "$courses.link",
                    "category": "$name",
                    "coach": "$courses.coach",
                    "id": "$courses.id",
                },
            },
            {"$sort": {"name": 1}},
        ]
    else:
        pipeline = [
            {"$match": {"$or": [{"name": category}, {"path": category}]}},
            {"$unwind": "$courses"},
            {"$match": {"courses.name": {"$regex": pattern, "$options": "i"}}},
            {
                "$project": {
                    "name": "$courses.name",
                    "link": "$courses.link",
                    "category": "$name",
                    "coach": "$courses.coach",
                    "id": "$courses.id",
                },
            },
            {"$sort": {"name": 1}},
        ]

    return {
        "pipeline_base": pipeline,
        "use_atlas": False,
    }


def build_regex_coach_course_search_pipeline(
    query_text: str,
    category: str = None,
    page: int = 1,
    page_size: int = 50,
    include_children: bool = True,
) -> dict:
    pattern = re_module.escape(query_text)

    if category:
        if include_children:
            scope = {"$match": {"$or": [{"name": category}, {"parent": category}]}}
        else:
            scope = {"$match": {"$or": [{"name": category}, {"path": category}]}}
        pipeline = [
            scope,
            {"$unwind": "$courses"},
            {"$match": {"courses.coach": {"$regex": pattern, "$options": "i"}}},
            {
                "$project": {
                    "name": "$courses.name",
                    "link": "$courses.link",
                    "category": "$name",
                    "coach": "$courses.coach",
                    "id": "$courses.id",
                },
            },
            {"$sort": {"name": 1}},
        ]
    else:
        pipeline = [
            {"$unwind": "$courses"},
            {"$match": {"courses.coach": {"$regex": pattern, "$options": "i"}}},
            {
                "$project": {
                    "name": "$courses.name",
                    "link": "$courses.link",
                    "category": "$name",
                    "coach": "$courses.coach",
                    "id": "$courses.id",
                },
            },
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
    parent: str = None,
):
    if is_atlas_search_enabled():
        index_name = get_search_index_name()
        try:
            pipes = build_category_search_pipeline(query_text, page, page_size, index_name, parent=parent)
            cnt_res = await db.categories.aggregate(pipes["count_pipeline"]).to_list(length=1)
            total = cnt_res[0]["total"] if cnt_res else 0

            if total > 0:
                docs = await db.categories.aggregate(pipes["data_pipeline"]).to_list(length=page_size + 1)
                have_more = len(docs) > page_size
                page_cats = docs[:page_size]
                for c in page_cats:
                    c.pop("_search_score", None)
                return page_cats, total, have_more

            logger.debug("Atlas Search returned 0 categories for query %r; falling back to regex", query_text)
        except Exception as e:
            logger.warning("Atlas Search failed for categories, falling back to regex: %s", e)

    pipes = build_regex_category_search_pipeline(query_text, page, page_size, parent=parent)
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
    if is_atlas_search_enabled():
        index_name = get_search_index_name()
        try:
            pipes = build_course_search_pipeline(query_text, page, page_size, index_name)

            cnt_res = await db.categories.aggregate(pipes["count_pipeline"]).to_list(length=1)
            total = cnt_res[0]["total"] if cnt_res else 0

            if total > 0:
                items = await db.categories.aggregate(pipes["data_pipeline"]).to_list(length=page_size + 1)
                have_more = len(items) > page_size
                course_items = items[:page_size]
                return course_items, total, have_more

            logger.debug("Atlas Search returned 0 courses for query %r; falling back to regex", query_text)
        except Exception as e:
            logger.warning("Atlas Search failed for courses, falling back to regex: %s", e)

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
    include_children: bool = True,
):
    if is_atlas_search_enabled():
        index_name = get_search_index_name()
        try:
            pipes = build_category_course_search_pipeline(
                query_text,
                category,
                page,
                page_size,
                index_name,
                include_children=include_children,
            )

            cnt_res = await db.categories.aggregate(pipes["count_pipeline"]).to_list(length=1)
            total = cnt_res[0]["total"] if cnt_res else 0

            if total > 0:
                items = await db.categories.aggregate(pipes["data_pipeline"]).to_list(length=page_size + 1)
                have_more = len(items) > page_size
                course_items = items[:page_size]
                return course_items, total, have_more

            logger.debug(
                "Atlas Search returned 0 category-courses for query %r in %r; falling back to regex",
                query_text,
                category,
            )
        except Exception as e:
            logger.warning("Atlas Search failed for category courses, falling back to regex: %s", e)

    pipes = build_regex_category_course_search_pipeline(
        query_text,
        category,
        page,
        page_size,
        include_children=include_children,
    )
    pipeline = pipes["pipeline_base"]
    cnt_res = await db.categories.aggregate(pipeline + [{"$count": "total"}]).to_list(length=1)
    total = cnt_res[0]["total"] if cnt_res else 0
    start = (page - 1) * page_size
    paged_pipeline = pipeline + [{"$skip": start}, {"$limit": page_size + 1}]
    items = await db.categories.aggregate(paged_pipeline).to_list(length=page_size + 1)
    have_more = len(items) > page_size
    course_items = items[:page_size]
    return course_items, total, have_more


async def execute_coach_course_search(
    db,
    query_text: str,
    category: str = None,
    page: int = 1,
    page_size: int = 50,
    include_children: bool = True,
):
    pipes = build_regex_coach_course_search_pipeline(
        query_text,
        category,
        page,
        page_size,
        include_children=include_children,
    )
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


def _fuzzy_prefix_length(query_text: str) -> int:
    n = len((query_text or "").strip())
    if n <= 1:
        return 0
    if n == 2:
        return 1
    return 2


def _make_text_search_stage(query_text: str, path: str, index_name: str, fuzzy: bool = True) -> dict:
    stage = {
        "$search": {
            "index": index_name,
            "text": {
                "query": query_text,
                "path": path,
            },
        },
    }
    if fuzzy:
        stage["$search"]["text"]["fuzzy"] = {
            "maxEdits": 1,
            "prefixLength": _fuzzy_prefix_length(query_text),
        }
    return stage
