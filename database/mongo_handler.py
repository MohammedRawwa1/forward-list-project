import logging

import pymongo
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
    async def ensure_uuid_indexes(cls):
        """Create unique indexes that make duplicate uuids impossible.

        - categories.id: one uuid per parent/category document.
        - categories.courses.id: one uuid per embedded course (multikey over the
          embedded array).

        Both are partial indexes matching only existing string values, so legacy
        docs without uuids (or with other types) never collide on null/missing.
        Creation is idempotent and safe to run on every startup. If duplicates
        already exist in the data, creation fails and is logged (best-effort)
        rather than crashing startup.
        """
        if cls._db is None:
            raise MongoConnectionError("MongoDB instance is not initialized.")
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
