# backend/services/storage_service.py
# PixelPerfect Storage Service - Production Ready
# Handles Cloudflare R2 with local fallback

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
import os
from typing import Optional
from pathlib import Path
import logging

logger = logging.getLogger("pixelperfect")

class StorageService:
    """Handle screenshot storage (Cloudflare R2 or local fallback)"""
    
    def __init__(self):
        self.use_r2 = False
        self.s3_client = None
        self.bucket_name = None
        self.public_url_base = None
        
        # Try to initialize R2
        self._initialize_r2()
    
    def _initialize_r2(self):
        """Initialize Cloudflare R2 if credentials are available"""
        endpoint = os.getenv('R2_ENDPOINT_URL')
        access_key = os.getenv('R2_ACCESS_KEY_ID')
        secret_key = os.getenv('R2_SECRET_ACCESS_KEY')
        
        if not all([endpoint, access_key, secret_key]):
            logger.info("📁 R2 not configured, using local file storage")
            return
        
        if endpoint == "https://[account-id].r2.cloudflarestorage.com":
            logger.info("📁 R2 endpoint not configured (placeholder value), using local storage")
            return
        
        try:
            self.s3_client = boto3.client(
                's3',
                endpoint_url=endpoint,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name='auto'  # R2 requires 'auto'
            )
            self.bucket_name = os.getenv('R2_BUCKET_NAME', 'pixelperfect-screenshots')
            self.public_url_base = os.getenv('R2_PUBLIC_URL')
            
            # Test connection
            try:
                self.s3_client.list_buckets()
                self.use_r2 = True
                logger.info(f"✅ R2 storage initialized: {self.bucket_name}")
            except Exception as e:
                logger.warning(f"⚠️ R2 connection test failed: {e}. Using local storage.")
                self.s3_client = None
                
        except Exception as e:
            logger.warning(f"⚠️ R2 initialization failed: {e}. Using local storage.")
            self.s3_client = None
    
    async def upload_screenshot(
        self,
        file_data: bytes,
        filename: str,
        content_type: str = "image/png"
    ) -> str:
        """
        Upload screenshot to R2/S3 or local storage
        
        Returns: Public URL or local path
        """
        if self.use_r2 and self.s3_client:
            return await self._upload_to_r2(file_data, filename, content_type)
        else:
            return await self._upload_to_local(file_data, filename)
    
    async def _upload_to_r2(
        self, 
        file_data: bytes, 
        filename: str, 
        content_type: str
    ) -> str:
        """Upload to Cloudflare R2"""
        try:
            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=filename,
                Body=file_data,
                ContentType=content_type,
                CacheControl='public, max-age=31536000'  # 1 year cache
            )
            
            # Return public URL
            if self.public_url_base:
                url = f"{self.public_url_base}/{filename}"
                logger.debug(f"📤 Uploaded to R2: {url}")
                return url
            else:
                # Generate presigned URL (valid for 7 days)
                url = self.s3_client.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': self.bucket_name, 'Key': filename},
                    ExpiresIn=604800  # 7 days
                )
                logger.debug(f"📤 Uploaded to R2 (presigned): {filename}")
                return url
        
        except (ClientError, NoCredentialsError) as e:
            logger.warning(f"R2 upload failed, falling back to local: {e}")
            return await self._upload_to_local(file_data, filename)
    
    async def _upload_to_local(self, file_data: bytes, filename: str) -> str:
        """Upload to local filesystem"""
        # Create screenshots directory
        base_dir = Path("screenshots")
        base_dir.mkdir(exist_ok=True)
        
        # Create user subdirectory if filename includes it
        file_path = base_dir / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Write file
        file_path.write_bytes(file_data)
        
        # Return relative URL
        url = f"/screenshots/{filename}"
        logger.debug(f"💾 Saved locally: {url}")
        return url
    
    async def delete_screenshot(self, filename: str) -> bool:
        """Delete screenshot from storage"""
        if self.use_r2 and self.s3_client:
            return await self._delete_from_r2(filename)
        else:
            return await self._delete_from_local(filename)
    
    async def _delete_from_r2(self, filename: str) -> bool:
        """Delete from R2"""
        try:
            self.s3_client.delete_object(
                Bucket=self.bucket_name,
                Key=filename
            )
            logger.debug(f"🗑️ Deleted from R2: {filename}")
            return True
        except ClientError as e:
            logger.warning(f"R2 delete failed: {e}")
            return False
    
    async def _delete_from_local(self, filename: str) -> bool:
        """Delete from local filesystem"""
        try:
            file_path = Path("screenshots") / filename
            if file_path.exists():
                file_path.unlink()
                logger.debug(f"🗑️ Deleted locally: {filename}")
                return True
            return False
        except Exception as e:
            logger.warning(f"Local delete failed: {e}")
            return False

# Global instance
storage_service = StorageService()