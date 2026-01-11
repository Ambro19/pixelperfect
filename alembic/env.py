"""
.env.py
PixelPerfect Alembic Environment Configuration
Handles database migrations for the PixelPerfect Screenshot API
"""

from logging.config import fileConfig
import sys
import os

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# ============================================================================
# SETUP: Import models from parent directory
# ============================================================================

# Add parent directory (backend/) to Python path so we can import models
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Import Base metadata from models
from models import Base

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✅ Loaded .env file")
except ImportError:
    print("⚠️  python-dotenv not installed, using system environment only")
except Exception as e:
    print(f"⚠️  Could not load .env: {e}")

# ============================================================================
# ALEMBIC CONFIGURATION
# ============================================================================

# This is the Alembic Config object, which provides access to values in alembic.ini
config = context.config

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set target metadata from our models
# This is what enables autogenerate to detect schema changes!
target_metadata = Base.metadata

# ============================================================================
# MIGRATION FUNCTIONS
# ============================================================================

def get_url():
    """
    Get database URL from environment variable or alembic.ini
    
    Priority:
    1. DATABASE_URL environment variable (production/Render)
    2. sqlalchemy.url from alembic.ini (development)
    """
    # Try environment variable first (production)
    url = os.getenv("DATABASE_URL")
    if url:
        print(f"📊 Using DATABASE_URL from environment")
        return url
    
    # Fall back to alembic.ini (development)
    url = config.get_main_option("sqlalchemy.url")
    if url:
        print(f"📊 Using sqlalchemy.url from alembic.ini")
        return url
    
    # Default fallback
    default_url = "sqlite:///./pixelperfect.db"
    print(f"⚠️  No DATABASE_URL found, using default: {default_url}")
    return default_url


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine,
    though an Engine is acceptable here as well. By skipping the Engine
    creation we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.
    
    Usage:
        alembic upgrade head --sql > migration.sql
    """
    url = get_url()
    
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,  # Detect column type changes
        compare_server_default=True,  # Detect default value changes
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode.

    In this scenario we need to create an Engine and associate a
    connection with the context.
    
    This is the normal mode when running:
        alembic upgrade head
    """
    # Get configuration section from alembic.ini
    configuration = config.get_section(config.config_ini_section)
    if configuration is None:
        configuration = {}
    
    # Override with DATABASE_URL from environment if available
    db_url = get_url()
    if db_url:
        configuration["sqlalchemy.url"] = db_url
    
    # Create engine
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,  # Detect column type changes
            compare_server_default=True,  # Detect default value changes
            
            # Render schema for SQLite (needed for some operations)
            render_as_batch=True if "sqlite" in str(db_url) else False,
        )

        with context.begin_transaction():
            context.run_migrations()


# ============================================================================
# MAIN EXECUTION
# ============================================================================

if context.is_offline_mode():
    print("🔄 Running migrations in OFFLINE mode...")
    run_migrations_offline()
else:
    print("🔄 Running migrations in ONLINE mode...")
    run_migrations_online()

print("✅ Migration complete!")