# ============================================================================
# API KEY MANAGEMENT SYSTEM - PixelPerfect
# ============================================================================
# Complete API key generation, storage, and validation
# Author: OneTechly
# Date: January 16, 2026

import secrets
import hashlib
from datetime import datetime
from typing import Optional, Tuple
from sqlalchemy import Column, String, DateTime, Integer, Boolean, ForeignKey, Index
from sqlalchemy.orm import Session
import logging

logger = logging.getLogger("pixelperfect")

# ============================================================================
# DATABASE MODEL - Add this to models.py
# ============================================================================

"""
Add this to your models.py file:

class ApiKey(Base):
    '''
    API Keys for programmatic access
    
    Unlike JWT tokens which expire, API keys are permanent until regenerated
    '''
    __tablename__ = "api_keys"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    
    # Store hashed version for security
    key_hash = Column(String(64), unique=True, nullable=False, index=True)
    
    # Store prefix (first 8 chars) for display: "pk_12345678..."
    key_prefix = Column(String(16), nullable=False)
    
    # Metadata
    name = Column(String(100), default="Default API Key")
    is_active = Column(Boolean, default=True, nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    # Indexes for performance
    __table_args__ = (
        Index('idx_api_key_hash', 'key_hash'),
        Index('idx_api_key_user', 'user_id'),
    )
"""

# ============================================================================
# API KEY GENERATION
# ============================================================================

def generate_api_key() -> str:
    """
    Generate a secure API key
    
    Format: pk_1234567890abcdef1234567890abcdef (32 hex chars after prefix)
    Total length: 35 characters
    
    Returns:
        API key in format: pk_{32_hex_chars}
    """
    # Generate 32 random bytes (256 bits of entropy)
    random_bytes = secrets.token_bytes(32)
    
    # Convert to hexadecimal
    hex_string = random_bytes.hex()
    
    # Add prefix
    api_key = f"pk_{hex_string}"
    
    return api_key


def hash_api_key(api_key: str) -> str:
    """
    Hash API key for secure storage
    
    We store hashed versions in the database, not plain text
    
    Args:
        api_key: Plain text API key
    
    Returns:
        SHA-256 hash of the API key
    """
    return hashlib.sha256(api_key.encode()).hexdigest()


def get_key_prefix(api_key: str) -> str:
    """
    Extract prefix for display (first 11 characters)
    
    Args:
        api_key: Full API key (e.g., pk_abc123...)
    
    Returns:
        Prefix for display (e.g., pk_abc12345...)
    """
    if len(api_key) >= 11:
        return api_key[:11] + "..."
    return api_key


# ============================================================================
# API KEY CRUD OPERATIONS
# ============================================================================

def create_api_key_for_user(
    db: Session,
    user_id: int,
    name: str = "Default API Key"
) -> Tuple[str, object]:
    """
    Create a new API key for a user
    
    Args:
        db: Database session
        user_id: User ID
        name: Friendly name for the key
    
    Returns:
        Tuple of (plain_text_key, api_key_record)
        
    Important:
        The plain text key is returned ONCE and never stored.
        Store it immediately or it's lost forever.
    """
    from models import ApiKey  # Import here to avoid circular dependency
    
    # Generate new API key
    api_key = generate_api_key()
    key_hash = hash_api_key(api_key)
    key_prefix = get_key_prefix(api_key)
    
    # Create database record
    api_key_record = ApiKey(
        user_id=user_id,
        key_hash=key_hash,
        key_prefix=key_prefix,
        name=name,
        is_active=True,
        created_at=datetime.utcnow()
    )
    
    db.add(api_key_record)
    db.commit()
    db.refresh(api_key_record)
    
    logger.info(f"✅ Created API key for user {user_id}: {key_prefix}")
    
    # Return plain text key (ONLY TIME IT'S AVAILABLE) and record
    return api_key, api_key_record


def validate_api_key(db: Session, api_key: str) -> Optional[object]:
    """
    Validate API key and return user
    
    Args:
        db: Database session
        api_key: Plain text API key from request
    
    Returns:
        User object if valid, None otherwise
    """
    from models import ApiKey, User  # Import here to avoid circular dependency
    
    # Hash the provided key
    key_hash = hash_api_key(api_key)
    
    # Find matching API key
    api_key_record = db.query(ApiKey).filter(
        ApiKey.key_hash == key_hash,
        ApiKey.is_active == True
    ).first()
    
    if not api_key_record:
        return None
    
    # Update last used timestamp
    api_key_record.last_used_at = datetime.utcnow()
    db.commit()
    
    # Get user
    user = db.query(User).filter(User.id == api_key_record.user_id).first()
    
    if user:
        logger.debug(f"✅ Valid API key for user {user.id}")
    
    return user


def regenerate_api_key(
    db: Session,
    user_id: int,
    old_key_id: Optional[int] = None
) -> Tuple[str, object]:
    """
    Regenerate API key for a user
    
    Args:
        db: Database session
        user_id: User ID
        old_key_id: Optional - specific key to regenerate
    
    Returns:
        Tuple of (new_plain_text_key, new_api_key_record)
    """
    from models import ApiKey  # Import here to avoid circular dependency
    
    # Find existing key
    if old_key_id:
        old_key = db.query(ApiKey).filter(
            ApiKey.id == old_key_id,
            ApiKey.user_id == user_id
        ).first()
    else:
        # Get first active key
        old_key = db.query(ApiKey).filter(
            ApiKey.user_id == user_id,
            ApiKey.is_active == True
        ).first()
    
    # Deactivate old key
    if old_key:
        old_key.is_active = False
        logger.info(f"🔄 Deactivated old API key {old_key.key_prefix}")
    
    # Create new key
    new_key, new_record = create_api_key_for_user(
        db=db,
        user_id=user_id,
        name=old_key.name if old_key else "Default API Key"
    )
    
    db.commit()
    
    return new_key, new_record


def get_user_api_keys(db: Session, user_id: int) -> list:
    """
    Get all API keys for a user
    
    Args:
        db: Database session
        user_id: User ID
    
    Returns:
        List of API key records (without plain text keys)
    """
    from models import ApiKey  # Import here to avoid circular dependency
    
    keys = db.query(ApiKey).filter(
        ApiKey.user_id == user_id
    ).order_by(ApiKey.created_at.desc()).all()
    
    return keys


def revoke_api_key(db: Session, user_id: int, key_id: int) -> bool:
    """
    Revoke (deactivate) an API key
    
    Args:
        db: Database session
        user_id: User ID (for authorization)
        key_id: API key ID to revoke
    
    Returns:
        True if revoked, False if not found
    """
    from models import ApiKey  # Import here to avoid circular dependency
    
    key = db.query(ApiKey).filter(
        ApiKey.id == key_id,
        ApiKey.user_id == user_id
    ).first()
    
    if not key:
        return False
    
    key.is_active = False
    db.commit()
    
    logger.info(f"🚫 Revoked API key {key.key_prefix} for user {user_id}")
    return True


# ============================================================================
# AUTHENTICATION HELPER - DUAL AUTH (JWT + API KEY)
# ============================================================================

def get_current_user_flexible(
    db: Session,
    authorization: Optional[str] = None
) -> Optional[object]:
    """
    Flexible authentication supporting both JWT and API keys
    
    Args:
        db: Database session
        authorization: Authorization header value
    
    Returns:
        User object if authenticated, None otherwise
    
    Usage in FastAPI:
        current_user = get_current_user_flexible(db, request.headers.get("authorization"))
    """
    if not authorization:
        return None
    
    # Remove "Bearer " prefix if present
    token_or_key = authorization.replace("Bearer ", "").strip()
    
    # Check if it's an API key (starts with "pk_")
    if token_or_key.startswith("pk_"):
        # API Key authentication
        user = validate_api_key(db, token_or_key)
        return user
    else:
        # JWT Token authentication
        from auth_deps import get_current_user
        # This will need to be adapted based on your JWT validation
        # For now, returning None - you'll need to integrate JWT validation here
        return None


# ============================================================================
# DATABASE MIGRATION
# ============================================================================

def run_api_key_migration(engine):
    """
    Create api_keys table if it doesn't exist
    
    Add this to your db_migrations.py or startup code
    """
    from models import Base, ApiKey
    
    logger.info("🔄 Running API key table migration...")
    
    # Create table if it doesn't exist
    Base.metadata.create_all(bind=engine, tables=[ApiKey.__table__])
    
    logger.info("✅ API key table migration complete")


def ensure_user_has_api_key(db: Session, user_id: int) -> str:
    """
    Ensure user has at least one API key (create if missing)
    
    Call this after user registration or on first login
    
    Args:
        db: Database session
        user_id: User ID
    
    Returns:
        Plain text API key (if newly created) or None (if already exists)
    """
    from models import ApiKey
    
    # Check if user already has an active API key
    existing_key = db.query(ApiKey).filter(
        ApiKey.user_id == user_id,
        ApiKey.is_active == True
    ).first()
    
    if existing_key:
        logger.debug(f"User {user_id} already has API key")
        return None
    
    # Create new API key
    api_key, _ = create_api_key_for_user(db, user_id, "Default API Key")
    
    logger.info(f"✅ Created initial API key for user {user_id}")
    
    return api_key


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

"""
## In your main.py - Add to registration endpoint:

@app.post("/register")
def register(user: UserCreate, db: Session = Depends(get_db)):
    # ... existing registration code ...
    
    # Create API key for new user
    from api_key_system import create_api_key_for_user
    api_key, _ = create_api_key_for_user(db, obj.id, "Default API Key")
    
    return {
        "message": "User registered successfully.",
        "account": canonical_account(obj),
        "api_key": api_key  # Show ONCE on registration
    }


## In your main.py - Add to auth_deps.py:

from typing import Optional
from fastapi import Header, HTTPException, Depends
from sqlalchemy.orm import Session
from models import get_db
from api_key_system import validate_api_key
import jwt

def get_current_user_dual_auth(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db)
) -> User:
    '''
    Support both JWT tokens and API keys
    '''
    if not authorization:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    # Remove "Bearer " prefix
    token_or_key = authorization.replace("Bearer ", "").strip()
    
    # Check if it's an API key
    if token_or_key.startswith("pk_"):
        user = validate_api_key(db, token_or_key)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid API key")
        return user
    
    # Otherwise, validate as JWT
    try:
        payload = jwt.decode(token_or_key, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        user = db.query(User).filter(User.username == username).first()
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        return user
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
"""