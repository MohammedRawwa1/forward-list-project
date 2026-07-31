import logging

from database.mongo_handler import MongoConnectionError, MongoDB

# Logger for this module
logger = logging.getLogger(__name__)


async def get_db():
    """Retrieves the MongoDB database instance asynchronously.

    Raises:
        MongoConnectionError: If the MongoDB instance is not initialized.

    Returns:
        db: An instance of the connected MongoDB database.

    """
    try:
        db = await MongoDB.get_db()
        if db is None:
            msg = "MongoDB instance is not initialized."
            raise MongoConnectionError(msg)
        return db
    except Exception as e:
        logger.exception("Failed to connect to MongoDB")
        msg = f"Failed to connect to MongoDB: {e}"
        raise MongoConnectionError(msg)
