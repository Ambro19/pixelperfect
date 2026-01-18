# ============================================================================
# AUTHENTICATION DEPENDENCIES - DUAL AUTH (JWT + API KEYS)
# ============================================================================
# File: backend/auth_deps.py
# Updated: January 16, 2026
# Supports BOTH JWT tokens and API keys

from fastapi import Depends, HTTPException, Header
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from typing import Optional
import jwt
import os
import logging

from models import User, get_db
from api_key_system import validate_api_key

logger = logging.getLogger("pixelperfect")

# JWT Configuration
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = os.getenv("ALGORITHM", "HS256")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# ============================================================================
# JWT-ONLY AUTHENTICATION (Original - for backwards compatibility)
# ============================================================================

def get_current_user_jwt(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db)
) -> User:
    """
    Original JWT-only authentication
    
    Use this for endpoints that ONLY accept JWT tokens (like web dashboard)
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        
        if username is None:
            raise HTTPException(
                status_code=401,
                detail="Could not validate credentials"
            )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail="Token has expired"
        )
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=401,
            detail="Could not validate credentials"
        )
    
    user = db.query(User).filter(User.username == username).first()
    
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="User not found"
        )
    
    return user


# ============================================================================
# DUAL AUTHENTICATION (JWT + API KEY)
# ============================================================================

def get_current_user(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db)
) -> User:
    """
    Flexible authentication supporting BOTH JWT tokens and API keys
    
    How it works:
    1. Checks Authorization header
    2. If starts with "pk_" → API Key authentication
    3. Otherwise → JWT authentication
    
    Usage:
        @router.get("/protected")
        def protected_route(current_user: User = Depends(get_current_user)):
            return {"user": current_user.username}
    
    Valid requests:
        # JWT Token
        curl -H "Authorization: Bearer eyJhbGc..." https://api.example.com
        
        # API Key
        curl -H "Authorization: Bearer pk_abc123..." https://api.example.com
    """
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated. Provide either JWT token or API key."
        )
    
    # Remove "Bearer " prefix if present
    token_or_key = authorization.replace("Bearer ", "").strip()
    
    # Check if it's an API key (starts with "pk_")
    if token_or_key.startswith("pk_"):
        logger.debug("🔑 Authenticating with API key")
        
        # API Key authentication
        user = validate_api_key(db, token_or_key)
        
        if not user:
            raise HTTPException(
                status_code=401,
                detail="Invalid or expired API key"
            )
        
        logger.debug(f"✅ API key valid for user {user.username}")
        return user
    
    else:
        logger.debug("🎫 Authenticating with JWT token")
        
        # JWT authentication
        try:
            payload = jwt.decode(token_or_key, SECRET_KEY, algorithms=[ALGORITHM])
            username: str = payload.get("sub")
            
            if username is None:
                raise HTTPException(
                    status_code=401,
                    detail="Invalid token: no username"
                )
        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=401,
                detail="Token has expired"
            )
        except jwt.PyJWTError as e:
            logger.warning(f"JWT decode failed: {e}")
            raise HTTPException(
                status_code=401,
                detail="Invalid token"
            )
        
        user = db.query(User).filter(User.username == username).first()
        
        if user is None:
            raise HTTPException(
                status_code=401,
                detail="User not found"
            )
        
        logger.debug(f"✅ JWT valid for user {user.username}")
        return user


# ============================================================================
# OPTIONAL: API KEY ONLY AUTHENTICATION
# ============================================================================

def get_current_user_api_key_only(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db)
) -> User:
    """
    API Key only authentication (stricter - no JWT allowed)
    
    Use this for endpoints that should ONLY accept API keys
    (useful for external integrations)
    """
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="API key required"
        )
    
    # Remove "Bearer " prefix
    api_key = authorization.replace("Bearer ", "").strip()
    
    if not api_key.startswith("pk_"):
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication method. API key required (starts with pk_)"
        )
    
    user = validate_api_key(db, api_key)
    
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired API key"
        )
    
    return user


# ============================================================================
# USAGE EXAMPLES
# ============================================================================

"""
## Example 1: Most routes (accept both JWT and API key)

@router.get("/api/v1/screenshot/")
def create_screenshot(
    current_user: User = Depends(get_current_user)  # ✅ Accepts both JWT and API key
):
    return {"user": current_user.username}


## Example 2: Web dashboard only (JWT only)

@router.get("/dashboard")
def dashboard(
    current_user: User = Depends(get_current_user_jwt)  # JWT only
):
    return {"dashboard": "data"}


## Example 3: External API only (API key only)

@router.post("/webhook/external")
def external_webhook(
    current_user: User = Depends(get_current_user_api_key_only)  # API key only
):
    return {"status": "received"}


## Example 4: Public endpoint (no auth)

@router.get("/api/pricing")
def get_pricing():  # No authentication
    return {"plans": [...]}
"""


# ============================================================================
# TESTING
# ============================================================================

"""
Test both authentication methods:

# 1. JWT Token
curl -X GET https://api.pixelperfectapi.net/api/v1/screenshot/ \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."

# 2. API Key
curl -X GET https://api.pixelperfectapi.net/api/v1/screenshot/ \
  -H "Authorization: Bearer pk_1234567890abcdef1234567890abcdef"

# 3. Invalid (should fail)
curl -X GET https://api.pixelperfectapi.net/api/v1/screenshot/ \
  -H "Authorization: Bearer invalid_token"
"""

# # =========================================
# # backend/auth_deps.py
# import os, jwt
# from typing import Optional
# from fastapi import Depends, HTTPException, status
# from fastapi.security import OAuth2PasswordBearer
# from sqlalchemy.orm import Session

# # ✅ Load env here so SECRET_KEY/ALGORITHM match tokens created in main.py
# try:
#     from dotenv import load_dotenv, find_dotenv
#     # local overrides first, then base .env (local wins)
#     load_dotenv(find_dotenv(".env.local"), override=True)
#     load_dotenv(find_dotenv(".env"), override=False)
# except Exception:
#     pass

# from models import get_db, User

# SECRET_KEY = os.getenv("SECRET_KEY", "devsecret")
# ALGORITHM  = os.getenv("ALGORITHM", "HS256")

# oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# def _get_user(db: Session, username: str) -> Optional[User]:
#     return db.query(User).filter(User.username == username).first()

# def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
#     cred_exc = HTTPException(
#         status_code=status.HTTP_401_UNAUTHORIZED,
#         detail="Could not validate credentials",
#         headers={"WWW-Authenticate": "Bearer"},
#     )
#     try:
#         payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
#         username = payload.get("sub")
#         if not username:
#             raise cred_exc
#     except Exception:
#         raise cred_exc
#     user = _get_user(db, username)
#     if not user:
#         raise cred_exc
#     return user
