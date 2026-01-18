# backend/scripts/backfill_api_keys.py

from models import User, ApiKey, get_db
from api_key_system import create_api_key_for_user
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def backfill_api_keys():
    '''Create API keys for existing users who don't have one'''
    db = next(get_db())
    
    # Get all users
    users = db.query(User).all()
    
    for user in users:
        # Check if user already has an API key
        existing_key = db.query(ApiKey).filter(
            ApiKey.user_id == user.id,
            ApiKey.is_active == True
        ).first()
        
        if existing_key:
            logger.info(f"User {user.username} already has API key")
            continue
        
        # Create API key
        api_key, _ = create_api_key_for_user(db, user.id, "Default API Key")
        logger.info(f"✅ Created API key for user {user.username}")
    
    logger.info("✅ Backfill complete!")

if __name__ == "__main__":
    backfill_api_keys()