from __future__ import annotations
# backend/db_migrations.py
# ============================================================================
# Updated: March 2026
#
# ✅ FIX: Added migrations for all missing users table columns:
#    - subscription_id          ← was causing 500 on /register in production
#    - stripe_subscription_status
#    - subscription_ends_at
#    - subscription_expires_at
#    - subscription_updated_at
#
# Root cause: models.py User defines these columns, but PostgreSQL's
# create_all() only creates missing TABLES — it never adds missing COLUMNS
# to existing tables. Without these ALTER TABLE statements the production
# DB schema drifts from the ORM model on every deploy that adds new fields.
#
# All migrations here are idempotent (check-before-alter), so they are
# safe to run on every startup.
# ============================================================================

import logging
from sqlalchemy.engine import Engine

log = logging.getLogger("pixelperfect.migrations")


def _dialect_name(engine: Engine) -> str:
    return engine.dialect.name


def _has_column(conn, dialect: str, table: str, column: str) -> bool:
    """Cross-DB column existence check."""
    if dialect == "sqlite":
        rows = conn.exec_driver_sql(f"PRAGMA table_info('{table}')").fetchall()
        return any(r[1] == column for r in rows)

    if dialect == "postgresql":
        sql = f"""
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name   = '{table}'
              AND column_name  = '{column}'
            LIMIT 1
        """
        return conn.exec_driver_sql(sql).first() is not None

    # Fallback: ANSI information_schema
    sql = f"""
        SELECT 1
        FROM information_schema.columns
        WHERE table_name  = '{table}'
          AND column_name = '{column}'
        LIMIT 1
    """
    return conn.exec_driver_sql(sql).first() is not None


def _add_column(conn, dialect: str, table: str, column: str, col_type: str) -> None:
    """
    Add a column if it doesn't already exist.
    Uses ADD COLUMN IF NOT EXISTS on PostgreSQL (9.6+) and a check-first
    pattern on SQLite (which doesn't support IF NOT EXISTS for columns).
    """
    if _has_column(conn, dialect, table, column):
        return

    log.info("Adding %s.%s (%s) …", table, column, col_type)
    if dialect == "postgresql":
        conn.exec_driver_sql(
            f"ALTER TABLE public.{table} ADD COLUMN IF NOT EXISTS {column} {col_type}"
        )
    else:
        conn.exec_driver_sql(
            f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
        )
    log.info("✅ Added %s.%s", table, column)


def _create_index(conn, name: str, table: str, cols: list[str]) -> None:
    cols_sql = ", ".join(cols)
    conn.exec_driver_sql(
        f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols_sql})"
    )


def run_startup_migrations(engine: Engine) -> None:
    """
    Idempotent, dialect-aware schema migrations.
    Safe to run at every startup.
    """
    dialect = _dialect_name(engine)

    with engine.begin() as conn:
        # ----------------------------------------------------------------
        # Engine-specific settings
        # ----------------------------------------------------------------
        if dialect == "sqlite":
            conn.exec_driver_sql("PRAGMA foreign_keys = ON;")
            conn.exec_driver_sql("PRAGMA journal_mode = WAL;")

        # ----------------------------------------------------------------
        # users table — missing columns
        # ----------------------------------------------------------------
        # ✅ FIX: subscription_id — was causing:
        #    psycopg2.errors.UndefinedColumn: column users.subscription_id
        #    → HTTP 500 on every POST /register in production
        _add_column(conn, dialect, "users", "subscription_id",
                    "VARCHAR(100) UNIQUE")

        # ✅ FIX: stripe_subscription_status — needed by webhook_handler.py
        _add_column(conn, dialect, "users", "stripe_subscription_status",
                    "VARCHAR(20)")

        # ✅ FIX: subscription_ends_at — legacy compatibility field
        _add_column(conn, dialect, "users", "subscription_ends_at",
                    "TIMESTAMP")

        # ✅ FIX: subscription_expires_at — used by subscription_sync.py
        _add_column(conn, dialect, "users", "subscription_expires_at",
                    "TIMESTAMP")

        # ✅ FIX: subscription_updated_at — last Stripe sync timestamp
        _add_column(conn, dialect, "users", "subscription_updated_at",
                    "TIMESTAMP")

        # ----------------------------------------------------------------
        # subscriptions table — missing columns (pre-existing migrations)
        # ----------------------------------------------------------------
        _add_column(conn, dialect, "subscriptions", "stripe_customer_id",
                    "TEXT")

        # Backfill stripe_customer_id from users table if available
        if _has_column(conn, dialect, "users", "stripe_customer_id"):
            if dialect == "postgresql":
                conn.exec_driver_sql("""
                    UPDATE subscriptions
                    SET stripe_customer_id = u.stripe_customer_id
                    FROM users AS u
                    WHERE u.id = subscriptions.user_id
                      AND subscriptions.stripe_customer_id IS NULL
                """)
            else:
                conn.exec_driver_sql("""
                    UPDATE subscriptions
                    SET stripe_customer_id = (
                        SELECT u.stripe_customer_id
                        FROM users AS u
                        WHERE u.id = subscriptions.user_id
                    )
                    WHERE stripe_customer_id IS NULL
                """)

        _add_column(conn, dialect, "subscriptions", "stripe_subscription_id",
                    "TEXT")

        # ----------------------------------------------------------------
        # Helpful idempotent indexes
        # ----------------------------------------------------------------
        _create_index(conn, "idx_subscriptions_user_id",
                      "subscriptions", ["user_id"])
        _create_index(conn, "idx_subscriptions_customer_id",
                      "subscriptions", ["stripe_customer_id"])
        _create_index(conn, "idx_users_username",
                      "users", ["username"])
        _create_index(conn, "idx_users_email",
                      "users", ["email"])
        _create_index(conn, "idx_users_subscription_id",
                      "users", ["subscription_id"])

        log.info("✅ DB migrations completed (dialect: %s)", dialect)


# ============================================================================
# API KEY MIGRATION
# ============================================================================

def run_api_key_migration(engine: Engine) -> None:
    """
    Creates the api_keys table if it doesn't exist.
    Safe to call multiple times (idempotent).
    """
    try:
        from api_key_system import run_api_key_migration as _run_migration
        _run_migration(engine)
        log.info("✅ API key migration completed")
    except ImportError as e:
        log.warning("⚠️ API key system not available: %s", e)
    except Exception as e:
        log.error("❌ API key migration failed: %s", e)