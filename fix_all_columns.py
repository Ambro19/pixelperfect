import sqlite3

# Connect to database
conn = sqlite3.connect('pixelperfect.db')
cursor = conn.cursor()

# Get current columns
cursor.execute('PRAGMA table_info(users)')
existing_columns = {col[1] for col in cursor.fetchall()}

print(f"📊 Found {len(existing_columns)} existing columns")

# Columns expected by new models.py
expected_columns = {
    'id': 'INTEGER PRIMARY KEY',
    'username': 'VARCHAR(50)',
    'email': 'VARCHAR(100)',
    'hashed_password': 'VARCHAR(255)',
    'stripe_customer_id': 'VARCHAR(100)',
    'subscription_tier': 'VARCHAR(20) DEFAULT "free"',
    'subscription_status': 'VARCHAR(20)',
    'subscription_id': 'VARCHAR(100)',
    'subscription_ends_at': 'DATETIME',
    'usage_screenshots': 'INTEGER DEFAULT 0',
    'usage_batch_requests': 'INTEGER DEFAULT 0',
    'usage_api_calls': 'INTEGER DEFAULT 0',
    'usage_reset_at': 'DATETIME',
    'created_at': 'DATETIME',
    'is_active': 'BOOLEAN DEFAULT 1',
}

# Find missing columns
missing = []
for col_name, col_type in expected_columns.items():
    if col_name not in existing_columns:
        missing.append((col_name, col_type))

print(f"\n🔍 Missing columns: {len(missing)}")

# Add missing columns
for col_name, col_type in missing:
    try:
        # Remove DEFAULT and UNIQUE constraints for ALTER TABLE
        clean_type = col_type.split('DEFAULT')[0].split('UNIQUE')[0].strip()
        
        sql = f'ALTER TABLE users ADD COLUMN {col_name} {clean_type}'
        cursor.execute(sql)
        conn.commit()
        print(f'✅ Added: {col_name} ({clean_type})')
    except Exception as e:
        print(f'❌ Error adding {col_name}: {e}')

# Verify all expected columns now exist
cursor.execute('PRAGMA table_info(users)')
final_columns = {col[1] for col in cursor.fetchall()}

print(f"\n📊 Final column count: {len(final_columns)}")
print(f"✅ All expected columns present: {expected_columns.keys() <= final_columns}")

# Show what's still missing (if any)
still_missing = expected_columns.keys() - final_columns
if still_missing:
    print(f"\n⚠️ Still missing: {still_missing}")
else:
    print(f"\n🎉 All columns added successfully!")

conn.close()