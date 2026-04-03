# backend/services/storage_service.py
# PixelPerfect Storage Service - Production Ready
# Handles Cloudflare R2 with local fallback
#
# ✅ FIX (Apr 2026): Removed list_buckets() connection test.
#
#    Root cause of R2 always being disabled in production:
#      The old _initialize_r2() called self.s3_client.list_buckets() as a
#      connectivity test. Cloudflare R2 requires a special account-level
#      token permission ("List Buckets") to call that operation — a standard
#      R2 API token scoped to a specific bucket does NOT have this permission.
#      Result: every startup produced "SignatureDoesNotMatch" or
#      "AccessDenied", storage_service set use_r2 = False, and all screenshots
#      went to Render's ephemeral disk (wiped on restart → 404s forever).
#
#    Fix:
#      Replace list_buckets() with head_bucket() on the specific bucket.
#      head_bucket() only requires read access to THAT bucket, which any
#      properly scoped R2 API token already has. If even that fails, we
#      still fall back to local storage gracefully.
#
#    Additionally:
#      Added a _validate_r2_public_url() guard that detects the common
#      misconfiguration of setting R2_PUBLIC_URL to the API domain
#      (e.g. api.pixelperfectapi.net) instead of the R2 CDN URL
#      (e.g. pub-xxx.r2.dev or a custom Cloudflare domain).
#      If misconfigured, R2 uploads are skipped to avoid generating broken
#      URLs, and a clear warning is logged at startup.

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
import os
from typing import Optional
from pathlib import Path
import logging

logger = logging.getLogger("pixelperfect")


def _validate_r2_public_url(url: str, backend_url: str) -> bool:
    """
    Return True only if url looks like a valid R2/CDN public URL.

    Common misconfiguration: setting R2_PUBLIC_URL to the API domain
    (e.g. https://api.pixelperfectapi.net) instead of the R2 CDN URL.
    Uploading to R2 but generating links that point to the API server
    creates URLs that will always 404 — the file is in R2, not on the API.

    A valid R2_PUBLIC_URL must NOT match the API backend domain.
    """
    if not url:
        return False

    url_clean = url.strip().rstrip("/").lower()

    # Reject placeholder values
    if "[account-id]" in url_clean or "example.com" in url_clean:
        return False

    # Reject if it matches the BACKEND_URL (the API server domain)
    if backend_url:
        backend_clean = backend_url.strip().rstrip("/").lower()
        if url_clean == backend_clean or url_clean in backend_clean or backend_clean in url_clean:
            logger.warning(
                "⚠️  R2_PUBLIC_URL is set to '%s' which looks like the API backend domain.\n"
                "   This will generate screenshot URLs that point to the API server, not R2 — "
                "all View links will 404.\n"
                "   Fix: set R2_PUBLIC_URL to your Cloudflare R2 public URL:\n"
                "     Option A (quickest): Cloudflare R2 → bucket → Settings → "
                "Public Development URL → Enable → copy the pub-xxx.r2.dev URL\n"
                "     Option B (branded):  Cloudflare R2 → bucket → Settings → "
                "Custom Domains → connect api-cdn.pixelperfectapi.net",
                url,
            )
            return False

    return True


class StorageService:
    """Handle screenshot storage (Cloudflare R2 or local fallback)"""

    def __init__(self):
        self.use_r2 = False
        self.s3_client = None
        self.bucket_name = None
        self.public_url_base = None

        self._initialize_r2()

    def _initialize_r2(self):
        """
        Initialize Cloudflare R2 if credentials are available.

        ✅ FIX: Uses head_bucket() instead of list_buckets() for the
        connection test. head_bucket() only needs read access to the
        specific bucket — a standard R2 API token already has this.
        list_buckets() requires an account-level "List Buckets" permission
        that is NOT included in a standard bucket-scoped R2 token.
        """
        endpoint   = os.getenv("R2_ENDPOINT_URL",     "").strip()
        access_key = os.getenv("R2_ACCESS_KEY_ID",    "").strip()
        secret_key = os.getenv("R2_SECRET_ACCESS_KEY","").strip()
        bucket     = os.getenv("R2_BUCKET_NAME",      "").strip()
        public_url = os.getenv("R2_PUBLIC_URL",       "").strip()
        backend_url= os.getenv("BACKEND_URL",         "").strip()

        # ── Guard: all five vars must be present ──────────────────────────
        if not all([endpoint, access_key, secret_key, bucket]):
            logger.info("📁 R2 not fully configured — using local file storage")
            return

        # ── Guard: reject placeholder endpoint ───────────────────────────
        if "[account-id]" in endpoint:
            logger.info("📁 R2 endpoint not configured (placeholder value) — using local storage")
            return

        # ── Guard: validate R2_PUBLIC_URL is actually an R2 CDN URL ──────
        if not _validate_r2_public_url(public_url, backend_url):
            logger.warning(
                "📁 R2_PUBLIC_URL is missing or misconfigured — "
                "R2 upload disabled until a valid CDN URL is set."
            )
            return

        # ── Initialise boto3 client ───────────────────────────────────────
        try:
            from botocore.config import Config
            self.s3_client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name="auto",          # required by Cloudflare R2
                config=Config(signature_version="s3v4"),
            )
            self.bucket_name    = bucket
            self.public_url_base= public_url.rstrip("/")

        except Exception as e:
            logger.warning("⚠️  R2 client creation failed: %s — using local storage.", e)
            self.s3_client = None
            return

        # ── Connection test: head_bucket() ────────────────────────────────
        # head_bucket() only requires read access to THIS bucket.
        # It is supported by all standard R2 API tokens scoped to the bucket.
        # (list_buckets() requires account-level permission — do NOT use it.)
        try:
            self.s3_client.head_bucket(Bucket=self.bucket_name)
            self.use_r2 = True
            logger.info("✅ R2 storage initialised: bucket=%s public_url=%s",
                        self.bucket_name, self.public_url_base)

        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "unknown")
            if code in ("404", "NoSuchBucket"):
                logger.warning(
                    "⚠️  R2 bucket '%s' not found (404). "
                    "Check R2_BUCKET_NAME env var. Using local storage.",
                    self.bucket_name,
                )
            elif code in ("403", "AccessDenied"):
                logger.warning(
                    "⚠️  R2 access denied for bucket '%s' (403). "
                    "Check R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY. Using local storage.",
                    self.bucket_name,
                )
            else:
                logger.warning(
                    "⚠️  R2 connection test failed (code=%s): %s — using local storage.",
                    code, e,
                )
            self.s3_client = None

        except Exception as e:
            logger.warning("⚠️  R2 connection test failed: %s — using local storage.", e)
            self.s3_client = None

    async def upload_screenshot(
        self,
        file_data: bytes,
        filename: str,
        content_type: str = "image/png",
    ) -> str:
        """
        Upload screenshot to R2 or local storage.
        Returns: permanent public URL (R2) or local relative path.
        """
        if self.use_r2 and self.s3_client:
            return await self._upload_to_r2(file_data, filename, content_type)
        return await self._upload_to_local(file_data, filename)

    async def _upload_to_r2(
        self,
        file_data: bytes,
        filename: str,
        content_type: str,
    ) -> str:
        """Upload to Cloudflare R2."""
        try:
            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=filename,
                Body=file_data,
                ContentType=content_type,
                CacheControl="public, max-age=31536000",  # 1-year CDN cache
            )

            if self.public_url_base:
                url = f"{self.public_url_base}/{filename}"
                logger.debug("📤 Uploaded to R2: %s", url)
                return url

            # Fallback: presigned URL (7 days) — only reached if public_url_base
            # is somehow empty despite the guard in _initialize_r2
            url = self.s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket_name, "Key": filename},
                ExpiresIn=604800,
            )
            logger.debug("📤 Uploaded to R2 (presigned): %s", filename)
            return url

        except (ClientError, NoCredentialsError) as e:
            logger.warning("R2 upload failed, falling back to local: %s", e)
            return await self._upload_to_local(file_data, filename)

    async def _upload_to_local(self, file_data: bytes, filename: str) -> str:
        """Save to local filesystem (development / R2 fallback)."""
        base_dir = Path("screenshots")
        base_dir.mkdir(exist_ok=True)

        file_path = base_dir / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(file_data)

        url = f"/screenshots/{filename}"
        logger.debug("💾 Saved locally: %s", url)
        return url

    async def delete_screenshot(self, filename: str) -> bool:
        """Delete screenshot from R2 or local storage."""
        if self.use_r2 and self.s3_client:
            return await self._delete_from_r2(filename)
        return await self._delete_from_local(filename)

    async def _delete_from_r2(self, filename: str) -> bool:
        try:
            self.s3_client.delete_object(Bucket=self.bucket_name, Key=filename)
            logger.debug("🗑️  Deleted from R2: %s", filename)
            return True
        except ClientError as e:
            logger.warning("R2 delete failed: %s", e)
            return False

    async def _delete_from_local(self, filename: str) -> bool:
        try:
            file_path = Path("screenshots") / filename
            if file_path.exists():
                file_path.unlink()
                logger.debug("🗑️  Deleted locally: %s", filename)
                return True
            return False
        except Exception as e:
            logger.warning("Local delete failed: %s", e)
            return False


# Global singleton
storage_service = StorageService()


# # ==========================================================================================================================
# # backend/services/storage_service.py
# # PixelPerfect Storage Service - Production Ready
# # Handles Cloudflare R2 with local fallback

# import boto3
# from botocore.exceptions import ClientError, NoCredentialsError
# import os
# from typing import Optional
# from pathlib import Path
# import logging

# logger = logging.getLogger("pixelperfect")

# class StorageService:
#     """Handle screenshot storage (Cloudflare R2 or local fallback)"""
    
#     def __init__(self):
#         self.use_r2 = False
#         self.s3_client = None
#         self.bucket_name = None
#         self.public_url_base = None
        
#         # Try to initialize R2
#         self._initialize_r2()
    
#     def _initialize_r2(self):
#         """Initialize Cloudflare R2 if credentials are available"""
#         endpoint = os.getenv('R2_ENDPOINT_URL')
#         access_key = os.getenv('R2_ACCESS_KEY_ID')
#         secret_key = os.getenv('R2_SECRET_ACCESS_KEY')
        
#         if not all([endpoint, access_key, secret_key]):
#             logger.info("📁 R2 not configured, using local file storage")
#             return
        
#         if endpoint == "https://[account-id].r2.cloudflarestorage.com":
#             logger.info("📁 R2 endpoint not configured (placeholder value), using local storage")
#             return
        
#         try:
#             self.s3_client = boto3.client(
#                 's3',
#                 endpoint_url=endpoint,
#                 aws_access_key_id=access_key,
#                 aws_secret_access_key=secret_key,
#                 region_name='auto'  # R2 requires 'auto'
#             )
#             self.bucket_name = os.getenv('R2_BUCKET_NAME', 'pixelperfect-screenshots')
#             self.public_url_base = os.getenv('R2_PUBLIC_URL')
            
#             # Test connection
#             try:
#                 self.s3_client.list_buckets()
#                 self.use_r2 = True
#                 logger.info(f"✅ R2 storage initialized: {self.bucket_name}")
#             except Exception as e:
#                 logger.warning(f"⚠️ R2 connection test failed: {e}. Using local storage.")
#                 self.s3_client = None
                
#         except Exception as e:
#             logger.warning(f"⚠️ R2 initialization failed: {e}. Using local storage.")
#             self.s3_client = None
    
#     async def upload_screenshot(
#         self,
#         file_data: bytes,
#         filename: str,
#         content_type: str = "image/png"
#     ) -> str:
#         """
#         Upload screenshot to R2/S3 or local storage
        
#         Returns: Public URL or local path
#         """
#         if self.use_r2 and self.s3_client:
#             return await self._upload_to_r2(file_data, filename, content_type)
#         else:
#             return await self._upload_to_local(file_data, filename)
    
#     async def _upload_to_r2(
#         self, 
#         file_data: bytes, 
#         filename: str, 
#         content_type: str
#     ) -> str:
#         """Upload to Cloudflare R2"""
#         try:
#             self.s3_client.put_object(
#                 Bucket=self.bucket_name,
#                 Key=filename,
#                 Body=file_data,
#                 ContentType=content_type,
#                 CacheControl='public, max-age=31536000'  # 1 year cache
#             )
            
#             # Return public URL
#             if self.public_url_base:
#                 url = f"{self.public_url_base}/{filename}"
#                 logger.debug(f"📤 Uploaded to R2: {url}")
#                 return url
#             else:
#                 # Generate presigned URL (valid for 7 days)
#                 url = self.s3_client.generate_presigned_url(
#                     'get_object',
#                     Params={'Bucket': self.bucket_name, 'Key': filename},
#                     ExpiresIn=604800  # 7 days
#                 )
#                 logger.debug(f"📤 Uploaded to R2 (presigned): {filename}")
#                 return url
        
#         except (ClientError, NoCredentialsError) as e:
#             logger.warning(f"R2 upload failed, falling back to local: {e}")
#             return await self._upload_to_local(file_data, filename)
    
#     async def _upload_to_local(self, file_data: bytes, filename: str) -> str:
#         """Upload to local filesystem"""
#         # Create screenshots directory
#         base_dir = Path("screenshots")
#         base_dir.mkdir(exist_ok=True)
        
#         # Create user subdirectory if filename includes it
#         file_path = base_dir / filename
#         file_path.parent.mkdir(parents=True, exist_ok=True)
        
#         # Write file
#         file_path.write_bytes(file_data)
        
#         # Return relative URL
#         url = f"/screenshots/{filename}"
#         logger.debug(f"💾 Saved locally: {url}")
#         return url
    
#     async def delete_screenshot(self, filename: str) -> bool:
#         """Delete screenshot from storage"""
#         if self.use_r2 and self.s3_client:
#             return await self._delete_from_r2(filename)
#         else:
#             return await self._delete_from_local(filename)
    
#     async def _delete_from_r2(self, filename: str) -> bool:
#         """Delete from R2"""
#         try:
#             self.s3_client.delete_object(
#                 Bucket=self.bucket_name,
#                 Key=filename
#             )
#             logger.debug(f"🗑️ Deleted from R2: {filename}")
#             return True
#         except ClientError as e:
#             logger.warning(f"R2 delete failed: {e}")
#             return False
    
#     async def _delete_from_local(self, filename: str) -> bool:
#         """Delete from local filesystem"""
#         try:
#             file_path = Path("screenshots") / filename
#             if file_path.exists():
#                 file_path.unlink()
#                 logger.debug(f"🗑️ Deleted locally: {filename}")
#                 return True
#             return False
#         except Exception as e:
#             logger.warning(f"Local delete failed: {e}")
#             return False

# # Global instance
# storage_service = StorageService()