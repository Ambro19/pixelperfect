# ========================================
# AUTHENTICATION UTILITIES - FIXED
# ========================================
# Handles password hashing with bcrypt 72-byte limit
# Uses SHA256 pre-hashing for long passwords

from passlib.context import CryptContext
import hashlib

# Configure password context with bcrypt
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def get_password_hash(password: str) -> str:
    """
    Hash a password using bcrypt with proper length handling.
    
    BCrypt has a 72-byte limit. For passwords that might exceed this,
    we pre-hash with SHA256 to ensure compatibility.
    
    Args:
        password: Plain text password
        
    Returns:
        Hashed password string
    """
    # Convert password to bytes
    password_bytes = password.encode('utf-8')
    
    # If password is longer than 72 bytes, pre-hash with SHA256
    if len(password_bytes) > 72:
        # SHA256 produces a fixed 64-character hex string (256 bits = 32 bytes * 2 for hex)
        password = hashlib.sha256(password_bytes).hexdigest()
    
    # Hash with bcrypt
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a password against its hash.
    
    Args:
        plain_password: Plain text password to verify
        hashed_password: Stored hashed password
        
    Returns:
        True if password matches, False otherwise
    """
    # Convert password to bytes
    password_bytes = plain_password.encode('utf-8')
    
    # If password is longer than 72 bytes, pre-hash with SHA256
    # (same as during hashing)
    if len(password_bytes) > 72:
        plain_password = hashlib.sha256(password_bytes).hexdigest()
    
    # Verify with bcrypt
    return pwd_context.verify(plain_password, hashed_password)

#======================================================================================
# # ========================================
# # AUTHENTICATION UTILITIES - FIXED
# # ========================================
# # Handles password hashing with bcrypt 72-byte limit
# # Uses SHA256 pre-hashing for long passwords

# from passlib.context import CryptContext
# import hashlib

# # Configure password context with bcrypt
# pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# def get_password_hash(password: str) -> str:
#     """
#     Hash a password using bcrypt with proper length handling.
    
#     BCrypt has a 72-byte limit. For passwords that might exceed this,
#     we pre-hash with SHA256 to ensure compatibility.
    
#     Args:
#         password: Plain text password
        
#     Returns:
#         Hashed password string
#     """
#     # Convert password to bytes
#     password_bytes = password.encode('utf-8')
    
#     # If password is longer than 72 bytes, pre-hash with SHA256
#     if len(password_bytes) > 72:
#         # SHA256 produces a fixed 64-character hex string (256 bits = 32 bytes * 2 for hex)
#         password = hashlib.sha256(password_bytes).hexdigest()
    
#     # Hash with bcrypt
#     return pwd_context.hash(password)


# def verify_password(plain_password: str, hashed_password: str) -> bool:
#     """
#     Verify a password against its hash.
    
#     Args:
#         plain_password: Plain text password to verify
#         hashed_password: Stored hashed password
        
#     Returns:
#         True if password matches, False otherwise
#     """
#     # Convert password to bytes
#     password_bytes = plain_password.encode('utf-8')
    
#     # If password is longer than 72 bytes, pre-hash with SHA256
#     # (same as during hashing)
#     if len(password_bytes) > 72:
#         plain_password = hashlib.sha256(password_bytes).hexdigest()
    
#     # Verify with bcrypt
#     return pwd_context.verify(plain_password, hashed_password)


