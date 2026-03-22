import logging
import os
from motor.motor_asyncio import AsyncIOMotorClient
import pymongo
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

class MongoConnectionError(Exception):
    """Custom exception for MongoDB connection errors."""
    pass

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
            cls._client = AsyncIOMotorClient(mongo_uri)
            cls._db = cls._client[db_name]
            logger.info(f"MongoDB initialized successfully with database: {db_name}")
            # NOTE: index creation was disabled (rollback to previous behavior)
        except Exception as e:
            logger.error(f"Failed to initialize MongoDB: {e}")
            raise MongoConnectionError(f"Failed to initialize MongoDB: {e}")

    @classmethod
    async def get_db(cls):
        """Get the MongoDB database instance."""
        if cls._db is None:
            raise MongoConnectionError("MongoDB instance is not initialized.")
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
            logger.error(f"Error occurred while deleting all data: {e}")
            raise MongoConnectionError(f"Failed to delete all data: {e}")

    @classmethod
    async def count_categories(cls):
        """Count the total number of categories in the database."""
        try:
            db = await cls.get_db()
            collection = db['categories']
            count = await collection.count_documents({})
            logger.info(f"Total categories count: {count}")
            return count
        except Exception as e:
            logger.error(f"Error occurred while counting categories: {e}")
            raise MongoConnectionError(f"Failed to count categories: {e}")

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
            cls._sync_client = pymongo.MongoClient(mongo_uri)
            cls._sync_db = cls._sync_client[db_name]
            logger.info(f"Sync MongoDB client initialized for database: {db_name}")
        except Exception as e:
            logger.exception("Failed to initialize sync pymongo client: %s", e)
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
            raise MongoConnectionError("MONGODB_URL and MONGODB_NAME must be set for sync client")
        try:
            cls.initialize_sync(mongo_uri, db_name)
            if cls._sync_db is None:
                raise MongoConnectionError("Failed to initialize sync MongoDB client")
            return cls._sync_db
        except Exception as e:
            raise MongoConnectionError(f"Failed to get sync DB: {e}")

    @classmethod
    async def get_all_categories(cls, page: int = 1, page_size: int = 20):
        """Get all categories from the database with pagination."""
        try:
            db = await cls.get_db()
            collection = db['categories']
            skip = (page - 1) * page_size
            categories_cursor = collection.find({}, {'_id': 0, 'name': 1}).skip(skip).limit(page_size)
            categories = await categories_cursor.to_list(length=page_size)

            if categories:
                logger.info(f"Fetched {len(categories)} categories from the database on page {page}.")
                return categories
            else:
                logger.info("No categories found on the requested page.")
                return []

        except Exception as e:
            logger.error(f"Error occurred while retrieving categories: {e}")
            raise MongoConnectionError(f"Failed to retrieve categories: {e}")

    # mongo_handler.py  (add inside ensure_indexes)
# ------------------------------------------------------------------
    @classmethod
    async def ensure_indexes(cls, collection_name='categories', indexes=None):
        # Conditional index creation: only run when AUTO_CREATE_INDEXES env var is set.
        auto = os.getenv('AUTO_CREATE_INDEXES', '0')
        if str(auto) not in ('1', 'true', 'yes', 'on'):
            logger.info("ensure_indexes skipped (AUTO_CREATE_INDEXES not enabled)")
            return

        db = await cls.get_db()
        coll = db[collection_name]
        try:
            info = await coll.index_information()
            if 'name_1' not in info:
                await coll.create_index('name', unique=True)
                logger.info("Unique index on categories.name created")
            if 'parent_1' not in info:
                await coll.create_index('parent')
                logger.info("Index on categories.parent created")
            if 'path_1' not in info:
                await coll.create_index('path')
                logger.info("Index on categories.path created")
            if 'courses.coach_1' not in info:
                await coll.create_index('courses.coach')
                logger.info("Index on categories.courses.coach created")
            if 'courses.name_1' not in info:
                await coll.create_index('courses.name')
                logger.info("Index on categories.courses.name created")
            if 'courses.id_1' not in info:
                try:
                    await coll.create_index('courses.id')
                    logger.info("Index on categories.courses.id created")
                except Exception:
                    logger.exception("Failed to create index on categories.courses.id")
        except Exception as e:
            logger.exception("ensure_indexes failed: %s", e)

    @classmethod
    async def delete_all_categories(cls):
        """Delete all categories from the database."""
        try:
            db = await cls.get_db()
            collection = db['categories']
            result = await collection.delete_many({})
            if result.deleted_count > 0:
                logger.info(f"Successfully deleted {result.deleted_count} categories.")
            else:
                logger.info("No categories found to delete.")

        except Exception as e:
            logger.error(f"Error occurred while deleting all categories: {e}")
            raise MongoConnectionError(f"Failed to delete categories: {e}")
