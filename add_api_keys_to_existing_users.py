# ============================================================================
# ADD API KEYS FOR EXISTING USERS - ONE-TIME MIGRATION
# ============================================================================
# File: backend/add_api_keys_to_existing_users.py
# Purpose: Create API keys for users who registered before the API key system
# Run once: python add_api_keys_to_existing_users.py
# ============================================================================

import sys
import os

# Add parent directory to path to import our modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import get_db, User
from api_key_system import create_api_key_for_user


def add_api_keys_to_existing_users():
    """
    Create API keys for all users who don't have one yet.
    Safe to run multiple times - only creates keys for users without them.
    """
    print("=" * 60)
    print("🔑 ADDING API KEYS TO EXISTING USERS")
    print("=" * 60)
    
    db = next(get_db())
    
    try:
        # Get all users
        users = db.query(User).all()
        print(f"\n📊 Found {len(users)} total users")
        
        created_count = 0
        skipped_count = 0
        error_count = 0
        
        for user in users:
            try:
                # Check if user already has an API key in the api_keys table
                from models import ApiKey
                existing_key = db.query(ApiKey).filter(ApiKey.user_id == user.id).first()
                
                if existing_key:
                    print(f"⏭️  SKIP: {user.username} (already has API key)")
                    skipped_count += 1
                    continue
                
                # Create API key for this user
                api_key, db_key = create_api_key_for_user(
                    db=db,
                    user_id=user.id,
                    name="Default API Key (Migrated)"
                )
                
                print(f"✅ CREATED: {user.username} → {db_key.key_prefix}...")
                created_count += 1
                
            except Exception as e:
                print(f"❌ ERROR: {user.username} → {str(e)}")
                error_count += 1
                continue
        
        print("\n" + "=" * 60)
        print("📊 MIGRATION SUMMARY")
        print("=" * 60)
        print(f"✅ Created:  {created_count}")
        print(f"⏭️  Skipped:  {skipped_count}")
        print(f"❌ Errors:   {error_count}")
        print(f"📊 Total:    {len(users)}")
        print("=" * 60)
        
        if created_count > 0:
            print("\n⚠️  IMPORTANT:")
            print("Users will need to retrieve their API key from the dashboard.")
            print("The plain-text key was generated but not shown during migration.")
            print("Users can regenerate a new key from the dashboard if needed.")
        
        print("\n✅ Migration complete!")
        
    except Exception as e:
        print(f"\n❌ FATAL ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    
    finally:
        db.close()


if __name__ == "__main__":
    print("\n⚠️  WARNING: This script will create API keys for existing users.")
    print("This is safe to run multiple times - it only creates keys for users without them.\n")
    
    response = input("Continue? (yes/no): ").strip().lower()
    
    if response in ['yes', 'y']:
        add_api_keys_to_existing_users()
    else:
        print("❌ Migration cancelled.")
        sys.exit(0)