# backend/db_migrations.py
from __future__ import annotations

import logging
from sqlalchemy.engine import Engine

log = logging.getLogger("pixelperfect.migrations")


def _dialect_name(engine: Engine) -> str:
    # "sqlite", "postgresql", "mysql", ...
    return engine.dialect.name


def _has_column(conn, dialect: str, table: str, column: str) -> bool:
    """
    Cross-DB column existence check.
    """
    if dialect == "sqlite":
        rows = conn.exec_driver_sql(f"PRAGMA table_info('{table}')").fetchall()
        # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk)
        return any(r[1] == column for r in rows)

    if dialect == "postgresql":
        # Default schema is "public". Adjust if you use custom schemas.
        sql = f"""
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name   = '{table}'
              AND column_name  = '{column}'
            LIMIT 1
        """
        return conn.exec_driver_sql(sql).first() is not None

    # Fallback: try ANSI information_schema
    sql = f"""
        SELECT 1
        FROM information_schema.columns
        WHERE table_name  = '{table}'
          AND column_name = '{column}'
        LIMIT 1
    """
    return conn.exec_driver_sql(sql).first() is not None


def _create_index(conn, name: str, table: str, cols: list[str]) -> None:
    cols_sql = ", ".join(cols)
    conn.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols_sql})")


def run_startup_migrations(engine: Engine) -> None:
    """
    Idempotent, dialect-aware tweaks/migrations.
    Safe to run at every startup.

    - SQLite: set PRAGMAs; add missing cols & helpful indexes
    - Postgres: skip PRAGMAs; add missing cols & helpful indexes
    """
    dialect = _dialect_name(engine)

    with engine.begin() as conn:
        # -----------------------------
        # Engine-specific settings
        # -----------------------------
        if dialect == "sqlite":
            # Enforce FK constraints and set WAL mode on SQLite only
            conn.exec_driver_sql("PRAGMA foreign_keys = ON;")
            conn.exec_driver_sql("PRAGMA journal_mode = WAL;")

        # -----------------------------
        # Schema drift fixes (both DBs)
        # -----------------------------
        # subscriptions.stripe_customer_id
        if not _has_column(conn, dialect, "subscriptions", "stripe_customer_id"):
            log.info("Adding subscriptions.stripe_customer_id …")
            if dialect == "postgresql":
                conn.exec_driver_sql(
                    "ALTER TABLE public.subscriptions "
                    "ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT"
                )
            else:
                conn.exec_driver_sql(
                    "ALTER TABLE subscriptions ADD COLUMN stripe_customer_id TEXT"
                )

            # Backfill from users.stripe_customer_id where available
            if _has_column(conn, dialect, "users", "stripe_customer_id"):
                conn.exec_driver_sql("""
                    UPDATE subscriptions
                    SET stripe_customer_id = u.stripe_customer_id
                    FROM users AS u
                    WHERE u.id = subscriptions.user_id
                """ if dialect == "postgresql" else """
                    UPDATE subscriptions
                    SET stripe_customer_id = (
                        SELECT u.stripe_customer_id
                        FROM users AS u
                        WHERE u.id = subscriptions.user_id
                    )
                """)

        # subscriptions.stripe_subscription_id
        if not _has_column(conn, dialect, "subscriptions", "stripe_subscription_id"):
            log.info("Adding subscriptions.stripe_subscription_id …")
            if dialect == "postgresql":
                conn.exec_driver_sql(
                    "ALTER TABLE public.subscriptions "
                    "ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT"
                )
            else:
                conn.exec_driver_sql(
                    "ALTER TABLE subscriptions ADD COLUMN stripe_subscription_id TEXT"
                )

        # Helpful idempotent indexes
        _create_index(conn, "idx_subscriptions_user_id",        "subscriptions", ["user_id"])
        _create_index(conn, "idx_subscriptions_customer_id",    "subscriptions", ["stripe_customer_id"])
        _create_index(conn, "idx_users_username",               "users",         ["username"])
        _create_index(conn, "idx_users_email",                  "users",         ["email"])

        log.info("✅ DB migrations completed (dialect: %s)", dialect)


# ============================================================================
# API KEY MIGRATION
# ============================================================================

def run_api_key_migration(engine: Engine) -> None:
    """
    Import and run the API key system migration.
    This creates the api_keys table if it doesn't exist.
    Safe to call multiple times (idempotent).
    """
    try:
        from api_key_system import run_api_key_migration as _run_migration
        _run_migration(engine)
        log.info("✅ API key migration completed")
    except ImportError as e:
        log.warning(f"⚠️ API key system not available: {e}")
    except Exception as e:
        log.error(f"❌ API key migration failed: {e}")

# # ==================================================

# # backend/db_migrations.py
# from __future__ import annotations

# import logging
# from sqlalchemy.engine import Engine

# log = logging.getLogger("pixelperfect.migrations")


# def _dialect_name(engine: Engine) -> str:
#     # "sqlite", "postgresql", "mysql", ...
#     return engine.dialect.name


# def _has_column(conn, dialect: str, table: str, column: str) -> bool:
#     """
#     Cross-DB column existence check.
#     """
#     if dialect == "sqlite":
#         rows = conn.exec_driver_sql(f"PRAGMA table_info('{table}')").fetchall()
#         # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk)
#         return any(r[1] == column for r in rows)

#     if dialect == "postgresql":
#         # Default schema is "public". Adjust if you use custom schemas.
#         sql = f"""
#             SELECT 1
#             FROM information_schema.columns
#             WHERE table_schema = 'public'
#               AND table_name   = '{table}'
#               AND column_name  = '{column}'
#             LIMIT 1
#         """
#         return conn.exec_driver_sql(sql).first() is not None

#     # Fallback: try ANSI information_schema
#     sql = f"""
#         SELECT 1
#         FROM information_schema.columns
#         WHERE table_name  = '{table}'
#           AND column_name = '{column}'
#         LIMIT 1
#     """
#     return conn.exec_driver_sql(sql).first() is not None


# def _create_index(conn, name: str, table: str, cols: list[str]) -> None:
#     cols_sql = ", ".join(cols)
#     conn.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols_sql})")


# def run_startup_migrations(engine: Engine) -> None:
#     """
#     Idempotent, dialect-aware tweaks/migrations.
#     Safe to run at every startup.

#     - SQLite: set PRAGMAs; add missing cols & helpful indexes
#     - Postgres: skip PRAGMAs; add missing cols & helpful indexes
#     """
#     dialect = _dialect_name(engine)

#     with engine.begin() as conn:
#         # -----------------------------
#         # Engine-specific settings
#         # -----------------------------
#         if dialect == "sqlite":
#             # Enforce FK constraints and set WAL mode on SQLite only
#             conn.exec_driver_sql("PRAGMA foreign_keys = ON;")
#             conn.exec_driver_sql("PRAGMA journal_mode = WAL;")

#         # -----------------------------
#         # Schema drift fixes (both DBs)
#         # -----------------------------
#         # subscriptions.stripe_customer_id
#         if not _has_column(conn, dialect, "subscriptions", "stripe_customer_id"):
#             log.info("Adding subscriptions.stripe_customer_id …")
#             if dialect == "postgresql":
#                 conn.exec_driver_sql(
#                     "ALTER TABLE public.subscriptions "
#                     "ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT"
#                 )
#             else:
#                 conn.exec_driver_sql(
#                     "ALTER TABLE subscriptions ADD COLUMN stripe_customer_id TEXT"
#                 )

#             # Backfill from users.stripe_customer_id where available
#             if _has_column(conn, dialect, "users", "stripe_customer_id"):
#                 conn.exec_driver_sql("""
#                     UPDATE subscriptions
#                     SET stripe_customer_id = u.stripe_customer_id
#                     FROM users AS u
#                     WHERE u.id = subscriptions.user_id
#                 """ if dialect == "postgresql" else """
#                     UPDATE subscriptions
#                     SET stripe_customer_id = (
#                         SELECT u.stripe_customer_id
#                         FROM users AS u
#                         WHERE u.id = subscriptions.user_id
#                     )
#                 """)

#         # subscriptions.stripe_subscription_id
#         if not _has_column(conn, dialect, "subscriptions", "stripe_subscription_id"):
#             log.info("Adding subscriptions.stripe_subscription_id …")
#             if dialect == "postgresql":
#                 conn.exec_driver_sql(
#                     "ALTER TABLE public.subscriptions "
#                     "ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT"
#                 )
#             else:
#                 conn.exec_driver_sql(
#                     "ALTER TABLE subscriptions ADD COLUMN stripe_subscription_id TEXT"
#                 )

#         # Helpful idempotent indexes
#         _create_index(conn, "idx_subscriptions_user_id",        "subscriptions", ["user_id"])
#         _create_index(conn, "idx_subscriptions_customer_id",    "subscriptions", ["stripe_customer_id"])
#         _create_index(conn, "idx_users_username",               "users",         ["username"])
#         _create_index(conn, "idx_users_email",                  "users",         ["email"])

#         log.info("✅ DB migrations completed (dialect: %s)", dialect)


# # ============================================================================
# # API KEY MIGRATION
# # ============================================================================

# def run_api_key_migration(engine: Engine) -> None:
#     """
#     Import and run the API key system migration.
#     This creates the api_keys table if it doesn't exist.
#     Safe to call multiple times (idempotent).
#     """
#     try:
#         from api_key_system import run_api_key_migration as _run_migration
#         _run_migration(engine)
#         log.info("✅ API key migration completed")
#     except ImportError as e:
#         log.warning(f"⚠️ API key system not available: {e}")
#     except Exception as e:
#         log.error(f"❌ API key migration failed: {e}")
# # # ============================================
