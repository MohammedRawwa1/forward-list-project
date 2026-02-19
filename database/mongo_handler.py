import logging
import os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

class MongoConnectionError(Exception):
    """Custom exception for MongoDB connection errors."""
    pass

class MongoDB:
    _client = None
    _db = None

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
    async def get_all_categories(cls, page: int = 1, page_size: int = 20):
        """Get all categories from the database with pagination."""
        try:
            db = await cls.get_db()
            collection = db['categories']
            skip = (page - 1) * page_size
            categories_cursor = collection.find({}, {'_id': 0, 'name': 1}).skip(skip).limit(page_size)
            categories = await categories_cursor.to_list(length=None)

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
        indexes = indexes or [('name', 1)]
        db = await cls.get_db()
        coll = db[collection_name]

        # --- categories ---
        if 'name_1' not in await coll.index_information():
            await coll.create_index('name', unique=True)
            logger.info("Unique index on categories.name created")

        # Note: legacy global `courses` collection removed; courses are embedded in `categories` documents.

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
