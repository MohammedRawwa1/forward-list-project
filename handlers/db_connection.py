import logging
from database.mongo_handler import MongoDB, MongoConnectionError

async def get_db():
    """
    Retrieves the MongoDB database instance asynchronously.
    
    Raises:
        MongoConnectionError: If the MongoDB instance is not initialized.
        
    Returns:
        db: An instance of the connected MongoDB database.
    """
    try:
        db = await MongoDB.get_db()
        if db is None:
            raise MongoConnectionError("MongoDB instance is not initialized.")
        return db
    except Exception as e:
        logger.error(f"Failed to connect to MongoDB: {e}")
        raise MongoConnectionError(f"Failed to connect to MongoDB: {e}")
