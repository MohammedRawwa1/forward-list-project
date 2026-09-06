import logging
import uuid

import pymongo
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger(__name__)


# ----------  errors  ----------
class MongoConnectionError(Exception):
    pass


# ----------  async client  ----------
class MongoDB:
    _client = None
    _db = None
    _sync_client = None
    _sync_db = None

    @classmethod
    async def initialize(cls, mongo_uri: str, db_name: str):
        if cls._client is not None:
            logger.warning("MongoDB is already initialized.")
            return

        try:
            cls._client = AsyncIOMotorClient(
                mongo_uri,
                maxPoolSize=50,
                minPoolSize=5,
                maxIdleTimeMS=30000,
                connectTimeoutMS=10000,
                serverSelectionTimeoutMS=15000,
                waitQueueTimeoutMS=5000,
            )
            cls._db = cls._client[db_name]
            logger.info("MongoDB initialized successfully with database: %s (pool=50)", db_name)
        except Exception as e:
            logger.exception("Failed to initialize MongoDB")
            msg = f"Failed to initialize MongoDB: {e}"
            raise MongoConnectionError(msg)

    @classmethod
    async def get_db(cls):
        if cls._db is None:
            msg = "MongoDB instance is not initialized."
            raise MongoConnectionError(msg)
        return cls._db

    @classmethod
    async def _backfill_course_uuids(cls, batch_size: int = 500):
        """Assign a uuid to EVERY embedded course that lacks a string one.

        The unique index on courses.id is multikey over the embedded array, so a
        category mixing uuid'd and legacy (id-less) courses would index the
        legacy entries as `courses.id: null` and the build would fail on the
        first pair of nulls. Backfilling every id-less course once at startup
        makes those null keys impossible and lets the index build succeed.

        Uses a full collection scan rather than an $elemMatch filter so exotic
        element types (regex, binary, nested arrays, nulls, non-dict entries)
        are also caught and normalized.
        """
        db = cls._db
        cursor = db["categories"].find({}, {"courses": 1}).batch_size(batch_size)
        backfilled = 0
        async for doc in cursor:
            courses = doc.get("courses") or []
            if not isinstance(courses, list):
                courses = []
            updates = {}
            for idx, course in enumerate(courses):
                if not isinstance(course, dict):
                    # Non-dict elements cannot take an `id` field; normalize them
                    # to the standard course shape so the index never sees null.
                    updates[f"courses.{idx}"] = {"name": str(course), "id": str(uuid.uuid4())}
                    continue
                cid = course.get("id")
                if not isinstance(cid, str):
                    updates[f"courses.{idx}.id"] = str(uuid.uuid4())
            if not updates:
                continue
            try:
                # Classic operator update: numeric path components address array
                # positions (courses.0.id). An aggregation-pipeline update does
                # NOT expand numeric path components to array elements — it
                # silently writes nothing — which is why the first attempt
                # reported success while changing nothing.
                res = await db["categories"].update_one(
                    {"_id": doc["_id"]},
                    {"$set": updates},
                )
                backfilled += res.modified_count or 0
            except Exception:
                logger.exception("Failed to backfill course uuids for category _id=%s", doc.get("_id"))
        if backfilled:
            logger.info("Backfilled missing course uuids: %d embedded courses updated", backfilled)

        # Second pass: catch anything the first pass could not address (deeply
        # nested or exotic shapes), using per-element updates by matched index.
        remaining = await cls._find_courses_without_uuid()
        if remaining:
            fixed = 0
            for off in remaining:
                try:
                    doc = await db["categories"].find_one(
                        {"_id": ObjectId(off["_id"])},
                        {"courses": 1},
                    )
                except Exception:
                    logger.exception("Second backfill pass: cannot load _id=%s", off["_id"])
                    continue
                courses = doc.get("courses") if doc else None
                if not isinstance(courses, list):
                    continue
                updates2 = {}
                for idx, course in enumerate(courses):
                    if not isinstance(course, dict):
                        updates2[f"courses.{idx}"] = {"name": str(course), "id": str(uuid.uuid4())}
                    elif not isinstance(course.get("id"), str):
                        updates2[f"courses.{idx}.id"] = str(uuid.uuid4())
                if updates2:
                    try:
                        await db["categories"].update_one(
                            {"_id": doc["_id"]},
                            {"$set": updates2},
                        )
                        fixed += len(updates2)
                    except Exception:
                        logger.exception("Second pass backfill failed for _id=%s", off["_id"])
            if fixed:
                logger.info("Second backfill pass updated %d additional courses", fixed)

    @classmethod
    async def _find_courses_without_uuid(cls) -> list[dict]:
        """Return name/_id of docs that still contain a non-string course id."""
        offenders = []
        cursor = cls._db["categories"].find(
            {"courses": {"$elemMatch": {"id": {"$not": {"$type": "string"}}}}},
            {"courses": 1, "name": 1},
        )
        async for doc in cursor:
            offenders.append({"_id": str(doc.get("_id")), "name": doc.get("name")})
        return offenders

    @classmethod
    async def ensure_uuid_indexes(cls):
        """Create unique indexes that make duplicate uuids impossible.

        - categories.id: one uuid per parent/category document.
        - categories.courses.id: one uuid per embedded course (multikey over the
          embedded array).

        The courses.id index is a partial index matching only documents that
        contain a string-valued course id; every id-less embedded course is
        backfilled to a fresh uuid first (see _backfill_course_uuids) so no
        `courses.id: null` keys can collide during the build. Creation is
        idempotent and safe to run on every startup. If duplicates still exist
        in the data, creation fails and is logged (best-effort) rather than
        crashing startup.
        """
        if cls._db is None:
            raise MongoConnectionError("MongoDB instance is not initialized.")
        try:
            await cls._backfill_course_uuids()
        except Exception:
            logger.exception("Failed to backfill missing course uuids (best-effort)")
        try:
            remaining = await cls._find_courses_without_uuid()
            if remaining:
                logger.warning(
                    "Course uuid backfill incomplete; %d categories still contain non-string "
                    "course ids (index build may fail): %s",
                    len(remaining),
                    remaining[:10],
                )
            else:
                logger.info("Verified: no non-string course ids remain in categories")
        except Exception:
            logger.debug("Could not verify course uuid backfill status")
        try:
            await cls._db["categories"].create_index(
                "id",
                unique=True,
                name="uniq_categories_id",
                partialFilterExpression={"id": {"$type": "string"}},
            )
            logger.info("Unique index on categories.id ensured")
        except Exception:
            logger.exception(
                "Failed to create unique index on categories.id "
                "(duplicate category uuids likely exist in the data)",
            )
        try:
            await cls._db["categories"].create_index(
                "courses.id",
                unique=True,
                name="uniq_categories_courses_id",
                partialFilterExpression={"courses.id": {"$type": "string"}},
            )
            logger.info("Unique index on categories.courses.id ensured")
        except Exception:
            logger.exception(
                "Failed to create unique index on categories.courses.id "
                "(duplicate course uuids likely exist in the data)",
            )

    @classmethod
    async def close(cls):
        if cls._client:
            cls._client.close()
            cls._client = None
            cls._db = None
            logger.info("MongoDB connection closed.")
        else:
            logger.warning("MongoDB connection is not initialized, nothing to close.")
        try:
            if cls._sync_client:
                try:
                    cls._sync_client.close()
                except Exception:
                    pass
                cls._sync_client = None
                cls._sync_db = None
                logger.info("Sync MongoDB client closed.")
        except Exception:
            logger.exception("Error while closing sync MongoDB client")

    # ----------  sync client  ----------
    @classmethod
    def initialize_sync(cls, mongo_uri: str, db_name: str):
        if cls._sync_client is not None:
            logger.debug("Sync MongoDB client already initialized")
            return
        try:
            cls._sync_client = pymongo.MongoClient(
                mongo_uri,
                maxPoolSize=10,
                connectTimeoutMS=10000,
                serverSelectionTimeoutMS=15000,
            )
            cls._sync_db = cls._sync_client[db_name]
            logger.info("Sync MongoDB client initialized for database: %s (pool=10)", db_name)
        except Exception:
            logger.exception("Failed to initialize sync pymongo client")
            cls._sync_client = None
            cls._sync_db = None
