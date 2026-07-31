import logging

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
