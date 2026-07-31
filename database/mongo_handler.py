import logging
import os

import pymongo
from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger(__name__)


class MongoConnectionError(Exception):
    """Custom exception for MongoDB connection errors."""


class MongoDB:
    _client = None
    _db = None
    _sync_client = None
    _sync_db = None

    @classmethod
    async def initialize(cls, mongo_uri: str, db_name: str):
        """Initialize the MongoDB client."""
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
            # NOTE: index creation was disabled (rollback to previous behavior)
        except Exception as e:
            logger.exception("Failed to initialize MongoDB")
            msg = f"Failed to initialize MongoDB: {e}"
            raise MongoConnectionError(msg)

    @classmethod
    async def get_db(cls):
        """Get the MongoDB database instance."""
        if cls._db is None:
            msg = "MongoDB instance is not initialized."
            raise MongoConnectionError(msg)
        return cls._db

    @classmethod
    async def close(cls):
        """Properly close the MongoDB connection."""
        if cls._client:
            cls._client.close()
            cls._client = None
            cls._db = None
            logger.info("MongoDB connection closed.")
        else:
            logger.warning("MongoDB connection is not initialized, nothing to close.")
        # Close synchronous client if present
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

    @classmethod
    async def delete_all_data(cls):
        """Delete all data from the database."""
        try:
            db = await cls.get_db()
            collections = await db.list_collection_names()
            for collection_name in collections:
                collection = db[collection_name]
                await collection.delete_many({})
            logger.info("All data has been deleted from the database.")
        except Exception as e:
            logger.exception("Error occurred while deleting all data")
            msg = f"Failed to delete all data: {e}"
            raise MongoConnectionError(msg)

    @classmethod
    async def count_categories(cls):
        """Count the total number of categories in the database with Redis-backed caching."""
        try:
            from handlers.base_handlers import _get_total_count

            db = await cls.get_db()
            count = await _get_total_count(db, "categories", {}, ttl=60)
            logger.info("Total categories count: %s", count)
            return count
        except Exception as e:
            logger.exception("Error occurred while counting categories")
            msg = f"Failed to count categories: {e}"
            raise MongoConnectionError(msg)

    @classmethod
    def initialize_sync(cls, mongo_uri: str, db_name: str):
        """Initialize a synchronous pymongo client for blocking writes.

        This is useful for performing strong-durability writes from
        synchronous code paths when Redis isn't configured.
        """
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

    @classmethod
    def get_sync_db(cls):
        """Return the synchronous pymongo database instance, initializing lazily.

        Raises MongoConnectionError if the sync client cannot be initialized.
        """
        if cls._sync_db is not None:
            return cls._sync_db
        # Attempt to lazily initialize using environment variables
        mongo_uri = os.getenv("MONGODB_URL")
        db_name = os.getenv("MONGODB_NAME")
        if not mongo_uri or not db_name:
            msg = "MONGODB_URL and MONGODB_NAME must be set for sync client"
            raise MongoConnectionError(msg)
        try:
            cls.initialize_sync(mongo_uri, db_name)
            if cls._sync_db is None:
                msg = "Failed to initialize sync MongoDB client"
                raise MongoConnectionError(msg)
            return cls._sync_db
        except Exception as e:
            msg = f"Failed to get sync DB: {e}"
            raise MongoConnectionError(msg)

    @classmethod
    async def get_all_categories(cls, page: int = 1, page_size: int = 20):
        """Get all categories from the database with pagination."""
        try:
            db = await cls.get_db()
            collection = db["categories"]
            skip = (page - 1) * page_size
            categories_cursor = collection.find({}, {"_id": 0, "name": 1}).skip(skip).limit(page_size)
            categories = await categories_cursor.to_list(length=page_size)

            if categories:
                logger.info("Fetched %d categories from the database on page %d.", len(categories), page)
                return categories
            logger.info("No categories found on the requested page.")
            return []

        except Exception as e:
            logger.exception("Error occurred while retrieving categories")
            msg = f"Failed to retrieve categories: {e}"
            raise MongoConnectionError(msg)

    # mongo_handler.py  (add inside ensure_indexes)
    # ------------------------------------------------------------------
    @classmethod
    async def ensure_indexes(cls, collection_name="categories", indexes=None):
        # Conditional index creation: only run when AUTO_CREATE_INDEXES env var is set.
        auto = os.getenv("AUTO_CREATE_INDEXES", "0")
        if str(auto) not in ("1", "true", "yes", "on"):
            logger.info("ensure_indexes skipped (AUTO_CREATE_INDEXES not enabled)")
            return

        db = await cls.get_db()
        coll = db[collection_name]
        try:
            info = await coll.index_information()
            if "name_1" not in info:
                await coll.create_index("name", unique=True)
                logger.info("Unique index on categories.name created")
            if "parent_1" not in info:
                await coll.create_index("parent")
                logger.info("Index on categories.parent created")
            if "path_1" not in info:
                await coll.create_index("path")
                logger.info("Index on categories.path created")
            if "courses.coach_1" not in info:
                await coll.create_index("courses.coach")
                logger.info("Index on categories.courses.coach created")
            if "courses.name_1" not in info:
                await coll.create_index("courses.name")
                logger.info("Index on categories.courses.name created")
            if "courses.id_1" not in info:
                try:
                    await coll.create_index("courses.id")
                    logger.info("Index on categories.courses.id created")
                except Exception:
                    logger.exception("Failed to create index on categories.courses.id")
            # Index for coaches collection (queried by topics field)
            try:
                coaches_coll = db["coaches"]
                coaches_info = await coaches_coll.index_information()
                if "topics_1" not in coaches_info:
                    await coaches_coll.create_index("topics")
                    logger.info("Index on coaches.topics created")
            except Exception:
                logger.debug("Could not create coaches.topics index (collection may not exist yet)")
        except Exception:
            logger.exception("ensure_indexes failed")

    @classmethod
    async def delete_all_categories(cls):
        """Delete all categories from the database."""
        try:
            db = await cls.get_db()
            collection = db["categories"]
            result = await collection.delete_many({})
            if result.deleted_count > 0:
                logger.info("Successfully deleted %d categories.", result.deleted_count)
            else:
                logger.info("No categories found to delete.")

        except Exception as e:
            logger.exception("Error occurred while deleting all categories")
            msg = f"Failed to delete categories: {e}"
            raise MongoConnectionError(msg)
