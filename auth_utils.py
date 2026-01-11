# # ========================================
# # AUTHENTICATION UTILITIES - PRODUCTION FIX
# # ========================================
# # - Solves bcrypt 72-byte limit by ALWAYS pre-hashing with SHA-256
# # - Avoids unicode/byte-length surprises
# # - Keeps passlib API so you don't have to change the rest of your code

# from __future__ import annotations

# import hashlib
# from passlib.context import CryptContext

# pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# def _normalize_and_prehash(password: str) -> str:
#     """
#     Always pre-hash the password with SHA-256 and return a hex digest.

#     Why always?
#     - bcrypt has a strict 72-byte input limit
#     - unicode passwords can exceed 72 bytes even when "short" in characters
#     - always-prehash removes edge cases completely

#     Returns:
#         64-char hex string (safe for bcrypt input)
#     """
#     if password is None:
#         password = ""
#     # Keep password exactly as user typed (no strip), but normalize encoding
#     pw_bytes = password.encode("utf-8", errors="strict")
#     return hashlib.sha256(pw_bytes).hexdigest()


# def get_password_hash(password: str) -> str:
#     """
#     Hash password using bcrypt, but feed it a SHA-256 pre-hash to avoid 72-byte limit.
#     """
#     prehashed = _normalize_and_prehash(password)
#     return pwd_context.hash(prehashed)


# def verify_password(plain_password: str, hashed_password: str) -> bool:
#     """
#     Verify password using same SHA-256 pre-hash step.
#     """
#     prehashed = _normalize_and_prehash(plain_password)
#     return pwd_context.verify(prehashed, hashed_password)

###############################################################
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