# backend/test_r2_connection.py
import boto3
import os

# These will be auto-loaded from Render environment
endpoint = os.getenv('R2_ENDPOINT_URL')
access_key = os.getenv('R2_ACCESS_KEY_ID')
secret_key = os.getenv('R2_SECRET_ACCESS_KEY')
bucket = os.getenv('R2_BUCKET_NAME')

print(f"Testing connection to: {endpoint}")

s3 = boto3.client(
    's3',
    endpoint_url=endpoint,
    aws_access_key_id=access_key,
    aws_secret_access_key=secret_key,
    region_name='auto'
)

try:
    # Test connection
    response = s3.list_buckets()
    print("✅ R2 Connection successful!")
    print(f"Available buckets: {[b['Name'] for b in response['Buckets']]}")
    
    # Check if your bucket exists
    if bucket in [b['Name'] for b in response['Buckets']]:
        print(f"✅ Bucket '{bucket}' found!")
    else:
        print(f"⚠️  Bucket '{bucket}' not found. Create it in Cloudflare R2 dashboard.")
        
except Exception as e:
    print(f"❌ Connection failed: {e}")