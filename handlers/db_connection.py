import logging

from database.mongo_handler import MongoConnectionError, MongoDB

logger = logging.getLogger(__name__)


# ----------  db connection  ----------
async def get_db():
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
