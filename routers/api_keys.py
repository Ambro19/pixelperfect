# ============================================================================
# API KEY ENDPOINTS - PixelPerfect
# ============================================================================
# RESTful API for managing API keys
# File: backend/routers/api_keys.py
# Author: OneTechly
# Date: January 16, 2026

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
from sqlalchemy.orm import Session

from models import User, get_db
from auth_deps import get_current_user
from api_key_system import (
    create_api_key_for_user,
    regenerate_api_key,
    get_user_api_keys,
    revoke_api_key
)
import logging

logger = logging.getLogger("pixelperfect")

router = APIRouter(prefix="/api/keys", tags=["API Keys"])

# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class ApiKeyResponse(BaseModel):
    """API key information (without sensitive data)"""
    id: int
    key_prefix: str
    name: str
    is_active: bool
    last_used_at: Optional[datetime]
    created_at: datetime

class ApiKeyCreateResponse(BaseModel):
    """Response when creating new API key (includes full key ONCE)"""
    api_key: str
    key_prefix: str
    name: str
    created_at: datetime
    message: str

class ApiKeyCreateRequest(BaseModel):
    """Request to create new API key"""
    name: str = "Default API Key"

# ============================================================================
# API KEY ENDPOINTS
# ============================================================================

@router.get("/", response_model=List[ApiKeyResponse])
def list_api_keys(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    List all API keys for the current user
    
    Returns list of API keys with prefixes (not full keys)
    """
    keys = get_user_api_keys(db, current_user.id)
    
    return [
        ApiKeyResponse(
            id=key.id,
            key_prefix=key.key_prefix,
            name=key.name,
            is_active=key.is_active,
            last_used_at=key.last_used_at,
            created_at=key.created_at
        )
        for key in keys
    ]


@router.post("/", response_model=ApiKeyCreateResponse)
def create_api_key(
    request: ApiKeyCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Create a new API key
    
    ⚠️ **IMPORTANT**: The full API key is shown ONCE and never stored in plain text.
    Copy it immediately or regenerate.
    
    ## Example Response
    ```json
    {
      "api_key": "pk_1234567890abcdef1234567890abcdef",
      "key_prefix": "pk_12345678...",
      "name": "Production API Key",
      "created_at": "2026-01-16T10:30:00Z",
      "message": "API key created successfully. Save it now - you won't see it again!"
    }
    ```
    """
    # Create new API key
    api_key, api_key_record = create_api_key_for_user(
        db=db,
        user_id=current_user.id,
        name=request.name
    )
    
    logger.info(f"✅ Created new API key for user {current_user.username}")
    
    return ApiKeyCreateResponse(
        api_key=api_key,
        key_prefix=api_key_record.key_prefix,
        name=api_key_record.name,
        created_at=api_key_record.created_at,
        message="API key created successfully. Save it now - you won't see it again!"
    )


@router.post("/regenerate", response_model=ApiKeyCreateResponse)
def regenerate_key(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Regenerate the default API key
    
    This deactivates the old key and creates a new one.
    
    ⚠️ **WARNING**: Old API key will stop working immediately!
    """
    # Regenerate key
    api_key, api_key_record = regenerate_api_key(
        db=db,
        user_id=current_user.id
    )
    
    logger.info(f"🔄 Regenerated API key for user {current_user.username}")
    
    return ApiKeyCreateResponse(
        api_key=api_key,
        key_prefix=api_key_record.key_prefix,
        name=api_key_record.name,
        created_at=api_key_record.created_at,
        message="API key regenerated successfully. Old key is now invalid."
    )


@router.delete("/{key_id}")
def delete_api_key(
    key_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Revoke (deactivate) an API key
    
    The key will no longer work for API requests.
    """
    success = revoke_api_key(db, current_user.id, key_id)
    
    if not success:
        raise HTTPException(status_code=404, detail="API key not found")
    
    logger.info(f"🚫 Revoked API key {key_id} for user {current_user.username}")
    
    return {
        "status": "revoked",
        "key_id": key_id,
        "message": "API key has been revoked and will no longer work"
    }


@router.get("/current")
def get_current_api_key_info(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get information about the current user's primary API key
    
    Returns the most recent active API key (without full key)
    """
    from models import ApiKey
    
    # Get most recent active key
    key = db.query(ApiKey).filter(
        ApiKey.user_id == current_user.id,
        ApiKey.is_active == True
    ).order_by(ApiKey.created_at.desc()).first()
    
    if not key:
        return {
            "has_api_key": False,
            "message": "No active API key. Create one to get started."
        }
    
    return {
        "has_api_key": True,
        "key_prefix": key.key_prefix,
        "name": key.name,
        "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
        "created_at": key.created_at.isoformat()
    }


# ============================================================================
# EXAMPLE USAGE IN MAIN.PY
# ============================================================================

"""
Add to main.py:

from routers.api_keys import router as api_keys_router

app.include_router(api_keys_router)
logger.info("✅ API Keys router loaded")
"""