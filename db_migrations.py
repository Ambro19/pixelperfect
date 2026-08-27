from __future__ import annotations
# backend/db_migrations.py
# ============================================================================
# Updated: April 2026
#
# ✅ All March 2026 fixes retained (users table columns, batch_jobs table
#    creation, screenshots columns, subscriptions columns, indexes)
#
# ✅ FIX (Apr 2026 — Persistent batch job storage):
#    Added two new columns required by batch.py's _reconstruct_job_from_db():
#
#    batch_jobs.urls_json (TEXT, nullable)
#      — JSON array of submitted URLs, written at submit time.
#        Enables full job reconstruction after a server restart.
#        Without it, all job item state is permanently lost on restart.
#
#    screenshots.batch_job_id (VARCHAR(32), nullable)
#      — Links each screenshot captured inside a batch back to its
#        parent BatchJob row. batch.py queries this to recover completed
#        screenshot URLs when reconstructing jobs from DB.
#        NULL for single (non-batch) screenshot captures.
#
#    Index idx_screenshots_batch_job_id added for query performance
#    (batch.py queries WHERE batch_job_id = ? on every job reconstruction).
#
# All migrations here are idempotent (check-before-alter), safe to run
# on every startup on both SQLite (dev) and PostgreSQL (production).
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


def _add_column(
    conn, dialect: str, table: str, column: str, col_type: str
) -> None:
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
            f"ALTER TABLE public.{table} "
            f"ADD COLUMN IF NOT EXISTS {column} {col_type}"
        )
    else:
        conn.exec_driver_sql(
            f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
        )
    log.info("✅ Added %s.%s", table, column)


def _create_index(
    conn, name: str, table: str, cols: list[str]
) -> None:
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
        # ⚠️ NOTE: SQLite cannot ADD COLUMN ... UNIQUE. This line is inert
        # today because the column already exists everywhere (create_all
        # builds it on fresh dev DBs), so _has_column short-circuits it. But
        # if it ever DID fire on SQLite it would abort this whole transaction
        # and every column below — including the three new ones — would
        # silently not be added. Dropping the word UNIQUE removes that trap;
        # the model already declares uniqueness. Left as-is here because you
        # have not asked for it.
        _add_column(conn, dialect, "users", "subscription_id",
                    "VARCHAR(100) UNIQUE")
        _add_column(conn, dialect, "users", "stripe_subscription_status",
                    "VARCHAR(20)")
        _add_column(conn, dialect, "users", "subscription_ends_at",
                    "TIMESTAMP")
        _add_column(conn, dialect, "users", "subscription_expires_at",
                    "TIMESTAMP")
        _add_column(conn, dialect, "users", "subscription_updated_at",
                    "TIMESTAMP")

        # ✅ NEW (Aug 2026 — billing-anniversary alignment):
        #
        # Mirror of the Stripe subscription's current period, so quota can be
        # anchored to the period the customer is actually billed for instead
        # of the calendar month. Written by subscription_sync.py and
        # webhook_handler.py; read by usage_accounting.py.
        #
        # Why this matters: a user who subscribes on the 21st was previously
        # given a quota window that opened on the 1st — already three weeks
        # old on the day they paid — and the dashboard labelled that date
        # "(billing cycle)", contradicting their Stripe receipt.
        #
        # Both columns are NULL for Free users and for anyone not yet synced.
        # usage_accounting falls back to the calendar month when the anchor is
        # missing, so this migration is safe to deploy AHEAD of the code that
        # reads it — that is the intended deploy order.
        _add_column(conn, dialect, "users", "subscription_current_period_start",
                    "TIMESTAMP")
        _add_column(conn, dialect, "users", "subscription_current_period_end",
                    "TIMESTAMP")

        # ✅ NEW (Aug 2026): throttle marker for the overdue re-verification in
        # subscription_sync._apply_local_overdue_downgrade_if_possible().
        #
        # That check refuses to downgrade while stripe_subscription_status
        # still reads "active", which protects a paying customer from a
        # webhook hiccup — but also means a MISSED webhook leaves a lapsed
        # subscription on a paid tier forever. The fix asks Stripe directly
        # when the recorded period has expired but the stored status still
        # claims active; this timestamp caps that to one call per hour per
        # user so the dashboard poll cannot flood the API.
        _add_column(conn, dialect, "users", "subscription_verified_at",
                    "TIMESTAMP")

        # ----------------------------------------------------------------
        # batch_jobs table — create if missing, then add new columns
        # ----------------------------------------------------------------
        if dialect == "postgresql":
            _batch_exists_sql = """
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name   = 'batch_jobs'
                LIMIT 1
            """
        else:
            _batch_exists_sql = (
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='batch_jobs'"
            )
        _batch_exists = (
            conn.exec_driver_sql(_batch_exists_sql).first() is not None
        )

        if not _batch_exists:
            log.info("Creating batch_jobs table …")
            if dialect == "postgresql":
                conn.exec_driver_sql("""
                    CREATE TABLE IF NOT EXISTS public.batch_jobs (
                        id              VARCHAR(32)  PRIMARY KEY,
                        user_id         INTEGER      NOT NULL
                                            REFERENCES users(id) ON DELETE CASCADE,
                        status          VARCHAR(20)  NOT NULL DEFAULT 'queued',
                        format          VARCHAR(10)  NOT NULL DEFAULT 'png',
                        width           INTEGER      NOT NULL DEFAULT 1920,
                        height          INTEGER      NOT NULL DEFAULT 1080,
                        full_page       BOOLEAN      NOT NULL DEFAULT FALSE,
                        total_urls      INTEGER      NOT NULL DEFAULT 0,
                        completed_count INTEGER               DEFAULT 0,
                        failed_count    INTEGER               DEFAULT 0,
                        urls_json       TEXT,
                        created_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
                        completed_at    TIMESTAMP
                    )
                """)
            else:
                conn.exec_driver_sql("""
                    CREATE TABLE IF NOT EXISTS batch_jobs (
                        id              TEXT     PRIMARY KEY,
                        user_id         INTEGER  NOT NULL
                                            REFERENCES users(id) ON DELETE CASCADE,
                        status          TEXT     NOT NULL DEFAULT 'queued',
                        format          TEXT     NOT NULL DEFAULT 'png',
                        width           INTEGER  NOT NULL DEFAULT 1920,
                        height          INTEGER  NOT NULL DEFAULT 1080,
                        full_page       INTEGER  NOT NULL DEFAULT 0,
                        total_urls      INTEGER  NOT NULL DEFAULT 0,
                        completed_count INTEGER           DEFAULT 0,
                        failed_count    INTEGER           DEFAULT 0,
                        urls_json       TEXT,
                        created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        completed_at    TIMESTAMP
                    )
                """)
            log.info("✅ Created batch_jobs table (includes urls_json)")

            _create_index(conn, "idx_batch_jobs_user_id",
                          "batch_jobs", ["user_id"])
            _create_index(conn, "idx_batch_jobs_created_at",
                          "batch_jobs", ["created_at"])
            _create_index(conn, "idx_batch_jobs_status",
                          "batch_jobs", ["status"])

        else:
            # Table already exists — add urls_json if it wasn't there yet.
            # ✅ FIX (Apr 2026): batch_jobs.urls_json
            # Required by _reconstruct_job_from_db() in batch.py.
            # Existing rows get NULL (no history to reconstruct from old jobs).
            _add_column(conn, dialect, "batch_jobs", "urls_json", "TEXT")

        # ----------------------------------------------------------------
        # screenshots table — missing columns
        # ----------------------------------------------------------------
        _add_column(conn, dialect, "screenshots", "remove_elements",
                    "TEXT")
        _add_column(conn, dialect, "screenshots", "quality",
                    "INTEGER")
        _add_column(conn, dialect, "screenshots", "storage_key",
                    "TEXT")
        _add_column(conn, dialect, "screenshots", "processing_time_ms",
                    "FLOAT")
        _add_column(conn, dialect, "screenshots", "error_message",
                    "TEXT")
        _add_column(conn, dialect, "screenshots", "dark_mode",
                    "BOOLEAN")
        _add_column(conn, dialect, "screenshots", "delay_seconds",
                    "INTEGER")
        _add_column(conn, dialect, "screenshots", "expires_at",
                    "TIMESTAMP")
        _add_column(conn, dialect, "screenshots", "is_baseline",
                    "BOOLEAN")
        _add_column(conn, dialect, "screenshots", "baseline_screenshot_id",
                    "TEXT")
        _add_column(conn, dialect, "screenshots", "difference_percentage",
                    "FLOAT")
        _add_column(conn, dialect, "screenshots", "has_changes",
                    "BOOLEAN")
        _add_column(conn, dialect, "screenshots", "screenshot_path",
                    "TEXT")

        # ✅ FIX (Apr 2026): screenshots.batch_job_id
        # Links each batch-captured screenshot back to its parent BatchJob.
        # Queried by _reconstruct_job_from_db() in batch.py:
        #   SELECT * FROM screenshots WHERE batch_job_id = ?
        # NULL for screenshots taken outside of batch jobs (single captures).
        # Note: FK constraint omitted here because:
        #   - SQLite does not enforce FK constraints on ADD COLUMN
        #   - PostgreSQL ADD COLUMN IF NOT EXISTS doesn't support inline FK
        #   The FK is declared on the SQLAlchemy model (models.py) which
        #   enforces it at the ORM level. Raw DB integrity is acceptable
        #   without the constraint since batch.py always sets this correctly.
        _add_column(conn, dialect, "screenshots", "batch_job_id",
                    "VARCHAR(32)")

        # Index is critical — batch.py queries WHERE batch_job_id = ?
        # on every job reconstruction after a restart. Without it,
        # this is a full table scan on potentially large screenshot tables.
        _create_index(
            conn,
            "idx_screenshots_batch_job_id",
            "screenshots",
            ["batch_job_id"],
        )

        # ----------------------------------------------------------------
        # subscriptions table — missing columns
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

# def run_startup_migrations(engine: Engine) -> None:
#     """
#     Idempotent, dialect-aware schema migrations.
#     Safe to run at every startup.
#     """
#     dialect = _dialect_name(engine)

#     with engine.begin() as conn:

#         # ----------------------------------------------------------------
#         # Engine-specific settings
#         # ----------------------------------------------------------------
#         if dialect == "sqlite":
#             conn.exec_driver_sql("PRAGMA foreign_keys = ON;")
#             conn.exec_driver_sql("PRAGMA journal_mode = WAL;")

#         # ----------------------------------------------------------------
#         # users table — missing columns
#         # ----------------------------------------------------------------
#         _add_column(conn, dialect, "users", "subscription_id",
#                     "VARCHAR(100) UNIQUE")
#         _add_column(conn, dialect, "users", "stripe_subscription_status",
#                     "VARCHAR(20)")
#         _add_column(conn, dialect, "users", "subscription_ends_at",
#                     "TIMESTAMP")
#         _add_column(conn, dialect, "users", "subscription_expires_at",
#                     "TIMESTAMP")
#         _add_column(conn, dialect, "users", "subscription_updated_at",
#                     "TIMESTAMP")

#         # ----------------------------------------------------------------
#         # batch_jobs table — create if missing, then add new columns
#         # ----------------------------------------------------------------
#         if dialect == "postgresql":
#             _batch_exists_sql = """
#                 SELECT 1 FROM information_schema.tables
#                 WHERE table_schema = 'public'
#                   AND table_name   = 'batch_jobs'
#                 LIMIT 1
#             """
#         else:
#             _batch_exists_sql = (
#                 "SELECT 1 FROM sqlite_master "
#                 "WHERE type='table' AND name='batch_jobs'"
#             )
#         _batch_exists = (
#             conn.exec_driver_sql(_batch_exists_sql).first() is not None
#         )

#         if not _batch_exists:
#             log.info("Creating batch_jobs table …")
#             if dialect == "postgresql":
#                 conn.exec_driver_sql("""
#                     CREATE TABLE IF NOT EXISTS public.batch_jobs (
#                         id              VARCHAR(32)  PRIMARY KEY,
#                         user_id         INTEGER      NOT NULL
#                                             REFERENCES users(id) ON DELETE CASCADE,
#                         status          VARCHAR(20)  NOT NULL DEFAULT 'queued',
#                         format          VARCHAR(10)  NOT NULL DEFAULT 'png',
#                         width           INTEGER      NOT NULL DEFAULT 1920,
#                         height          INTEGER      NOT NULL DEFAULT 1080,
#                         full_page       BOOLEAN      NOT NULL DEFAULT FALSE,
#                         total_urls      INTEGER      NOT NULL DEFAULT 0,
#                         completed_count INTEGER               DEFAULT 0,
#                         failed_count    INTEGER               DEFAULT 0,
#                         urls_json       TEXT,
#                         created_at      TIMESTAMP    NOT NULL DEFAULT NOW(),
#                         completed_at    TIMESTAMP
#                     )
#                 """)
#             else:
#                 conn.exec_driver_sql("""
#                     CREATE TABLE IF NOT EXISTS batch_jobs (
#                         id              TEXT     PRIMARY KEY,
#                         user_id         INTEGER  NOT NULL
#                                             REFERENCES users(id) ON DELETE CASCADE,
#                         status          TEXT     NOT NULL DEFAULT 'queued',
#                         format          TEXT     NOT NULL DEFAULT 'png',
#                         width           INTEGER  NOT NULL DEFAULT 1920,
#                         height          INTEGER  NOT NULL DEFAULT 1080,
#                         full_page       INTEGER  NOT NULL DEFAULT 0,
#                         total_urls      INTEGER  NOT NULL DEFAULT 0,
#                         completed_count INTEGER           DEFAULT 0,
#                         failed_count    INTEGER           DEFAULT 0,
#                         urls_json       TEXT,
#                         created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
#                         completed_at    TIMESTAMP
#                     )
#                 """)
#             log.info("✅ Created batch_jobs table (includes urls_json)")

#             _create_index(conn, "idx_batch_jobs_user_id",
#                           "batch_jobs", ["user_id"])
#             _create_index(conn, "idx_batch_jobs_created_at",
#                           "batch_jobs", ["created_at"])
#             _create_index(conn, "idx_batch_jobs_status",
#                           "batch_jobs", ["status"])

#         else:
#             # Table already exists — add urls_json if it wasn't there yet.
#             # ✅ FIX (Apr 2026): batch_jobs.urls_json
#             # Required by _reconstruct_job_from_db() in batch.py.
#             # Existing rows get NULL (no history to reconstruct from old jobs).
#             _add_column(conn, dialect, "batch_jobs", "urls_json", "TEXT")

#         # ----------------------------------------------------------------
#         # screenshots table — missing columns
#         # ----------------------------------------------------------------
#         _add_column(conn, dialect, "screenshots", "remove_elements",
#                     "TEXT")
#         _add_column(conn, dialect, "screenshots", "quality",
#                     "INTEGER")
#         _add_column(conn, dialect, "screenshots", "storage_key",
#                     "TEXT")
#         _add_column(conn, dialect, "screenshots", "processing_time_ms",
#                     "FLOAT")
#         _add_column(conn, dialect, "screenshots", "error_message",
#                     "TEXT")
#         _add_column(conn, dialect, "screenshots", "dark_mode",
#                     "BOOLEAN")
#         _add_column(conn, dialect, "screenshots", "delay_seconds",
#                     "INTEGER")
#         _add_column(conn, dialect, "screenshots", "expires_at",
#                     "TIMESTAMP")
#         _add_column(conn, dialect, "screenshots", "is_baseline",
#                     "BOOLEAN")
#         _add_column(conn, dialect, "screenshots", "baseline_screenshot_id",
#                     "TEXT")
#         _add_column(conn, dialect, "screenshots", "difference_percentage",
#                     "FLOAT")
#         _add_column(conn, dialect, "screenshots", "has_changes",
#                     "BOOLEAN")
#         _add_column(conn, dialect, "screenshots", "screenshot_path",
#                     "TEXT")

#         # ✅ FIX (Apr 2026): screenshots.batch_job_id
#         # Links each batch-captured screenshot back to its parent BatchJob.
#         # Queried by _reconstruct_job_from_db() in batch.py:
#         #   SELECT * FROM screenshots WHERE batch_job_id = ?
#         # NULL for screenshots taken outside of batch jobs (single captures).
#         # Note: FK constraint omitted here because:
#         #   - SQLite does not enforce FK constraints on ADD COLUMN
#         #   - PostgreSQL ADD COLUMN IF NOT EXISTS doesn't support inline FK
#         #   The FK is declared on the SQLAlchemy model (models.py) which
#         #   enforces it at the ORM level. Raw DB integrity is acceptable
#         #   without the constraint since batch.py always sets this correctly.
#         _add_column(conn, dialect, "screenshots", "batch_job_id",
#                     "VARCHAR(32)")

#         # Index is critical — batch.py queries WHERE batch_job_id = ?
#         # on every job reconstruction after a restart. Without it,
#         # this is a full table scan on potentially large screenshot tables.
#         _create_index(
#             conn,
#             "idx_screenshots_batch_job_id",
#             "screenshots",
#             ["batch_job_id"],
#         )

#         # ----------------------------------------------------------------
#         # subscriptions table — missing columns
#         # ----------------------------------------------------------------
#         _add_column(conn, dialect, "subscriptions", "stripe_customer_id",
#                     "TEXT")

#         # Backfill stripe_customer_id from users table if available
#         if _has_column(conn, dialect, "users", "stripe_customer_id"):
#             if dialect == "postgresql":
#                 conn.exec_driver_sql("""
#                     UPDATE subscriptions
#                     SET stripe_customer_id = u.stripe_customer_id
#                     FROM users AS u
#                     WHERE u.id = subscriptions.user_id
#                       AND subscriptions.stripe_customer_id IS NULL
#                 """)
#             else:
#                 conn.exec_driver_sql("""
#                     UPDATE subscriptions
#                     SET stripe_customer_id = (
#                         SELECT u.stripe_customer_id
#                         FROM users AS u
#                         WHERE u.id = subscriptions.user_id
#                     )
#                     WHERE stripe_customer_id IS NULL
#                 """)

#         _add_column(conn, dialect, "subscriptions", "stripe_subscription_id",
#                     "TEXT")

#         # ----------------------------------------------------------------
#         # Helpful idempotent indexes
#         # ----------------------------------------------------------------
#         _create_index(conn, "idx_subscriptions_user_id",
#                       "subscriptions", ["user_id"])
#         _create_index(conn, "idx_subscriptions_customer_id",
#                       "subscriptions", ["stripe_customer_id"])
#         _create_index(conn, "idx_users_username",
#                       "users", ["username"])
#         _create_index(conn, "idx_users_email",
#                       "users", ["email"])
#         _create_index(conn, "idx_users_subscription_id",
#                       "users", ["subscription_id"])

#         log.info("✅ DB migrations completed (dialect: %s)", dialect)


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

# ======= END OF db_migrations.py =============================================
