#!/usr/bin/env python3
# ============================================================================
# PRODUCTION FIX SCRIPT — Force sync user subscription from Stripe
# ============================================================================
# File: backend/force_sync_user.py
# Author: OneTechly
# Created: May 2026
#
# PURPOSE:
#   Immediately fix a user whose subscription_tier is wrong in the DB
#   by pulling their real subscription data from Stripe and writing
#   the correct tier to the database.
#
#   Run this ONCE on the production server (Render shell or local if
#   your DATABASE_URL points to the production Postgres).
#
# USAGE (Render Shell or any env where DATABASE_URL + STRIPE_SECRET_KEY are set):
#
#   # Fix by username:
#   python force_sync_user.py --username UserProdTest_003
#
#   # Fix by email:
#   python force_sync_user.py --email onetechly@gmail.com
#
#   # Fix by user ID:
#   python force_sync_user.py --user-id 7
#
#   # Dry run (print what would happen, don't write):
#   python force_sync_user.py --username UserProdTest_003 --dry-run
#
#   # Force-set tier directly without calling Stripe (emergency bypass):
#   python force_sync_user.py --username UserProdTest_003 --force-tier business
#
# SAFE TO RUN MULTIPLE TIMES — idempotent.
# ============================================================================

import os
import sys
import argparse
import logging
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("force_sync")


def main():
    parser = argparse.ArgumentParser(
        description="Force-sync a user's subscription tier from Stripe to the database."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--username",  help="Username to fix")
    group.add_argument("--email",     help="Email address to fix")
    group.add_argument("--user-id",   type=int, help="User ID to fix")

    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would change without writing to DB",
    )
    parser.add_argument(
        "--force-tier",
        choices=["free", "pro", "business", "premium"],
        help="Bypass Stripe and set this tier directly (emergency use only)",
    )
    args = parser.parse_args()

    # ── Database connection ───────────────────────────────────────────────────
    try:
        from database import SessionLocal
        from models import User
    except ImportError as e:
        logger.error("Cannot import database/models: %s", e)
        logger.error("Run this script from the backend/ directory.")
        sys.exit(1)

    db = SessionLocal()

    try:
        # ── Find user ─────────────────────────────────────────────────────────
        if args.username:
            user = db.query(User).filter(User.username == args.username).first()
        elif args.email:
            user = db.query(User).filter(User.email == args.email.strip().lower()).first()
        else:
            user = db.query(User).filter(User.id == args.user_id).first()

        if not user:
            logger.error("No user found matching the provided identifier.")
            sys.exit(1)

        logger.info("Found user: id=%s  username=%s  email=%s  current_tier=%s",
                    user.id, user.username, user.email, user.subscription_tier)
        logger.info("stripe_customer_id: %s", getattr(user, "stripe_customer_id", None))

        # ── Emergency direct-set path ─────────────────────────────────────────
        if args.force_tier:
            if args.dry_run:
                logger.info("[DRY RUN] Would set subscription_tier = '%s'", args.force_tier)
            else:
                old = user.subscription_tier
                user.subscription_tier = args.force_tier
                if hasattr(user, "subscription_updated_at"):
                    user.subscription_updated_at = datetime.now(timezone.utc)
                db.commit()
                db.refresh(user)
                logger.info("✅ Force-set tier: %s → %s", old, user.subscription_tier)
                logger.info("Dashboard will reflect the new tier on next refresh.")
            return

        # ── Stripe sync path ──────────────────────────────────────────────────
        stripe_key = os.getenv("STRIPE_SECRET_KEY", "").strip()
        if not stripe_key:
            logger.error(
                "STRIPE_SECRET_KEY is not set. "
                "Either set it in your environment or use --force-tier as an emergency bypass."
            )
            sys.exit(1)

        try:
            import stripe as _stripe
            _stripe.api_key = stripe_key
        except ImportError:
            logger.error("stripe package not installed. Run: pip install stripe")
            sys.exit(1)

        customer_id = getattr(user, "stripe_customer_id", None)

        # If no customer_id stored, try to find by email
        if not customer_id:
            logger.warning("No stripe_customer_id in DB — searching Stripe by email...")
            try:
                customers = _stripe.Customer.list(email=user.email, limit=5)
                if customers.data:
                    customer_id = customers.data[0].id
                    logger.info("Found Stripe customer by email: %s", customer_id)
                    if not args.dry_run:
                        user.stripe_customer_id = customer_id
                        db.commit()
                else:
                    logger.error(
                        "No Stripe customer found for email '%s'. "
                        "Use --force-tier to set the tier directly.", user.email
                    )
                    sys.exit(1)
            except Exception as e:
                logger.error("Stripe customer search failed: %s", e)
                sys.exit(1)

        # Fetch active subscriptions
        logger.info("Fetching subscriptions for customer %s...", customer_id)
        try:
            # Only expand 4 levels (Stripe maximum). price.product is not
            # expanded — lookup_key/nickname resolution is sufficient.
            subs = _stripe.Subscription.list(
                customer=customer_id,
                status="active",
                limit=1,
                expand=["data.items.data.price"],
            )
        except Exception as e:
            logger.error("Stripe subscription fetch failed: %s", e)
            sys.exit(1)

        if not subs.data:
            logger.warning(
                "No active subscription found in Stripe for customer %s. "
                "If the payment just completed, wait 30 seconds and retry. "
                "Otherwise use --force-tier business to set manually.",
                customer_id,
            )
            sys.exit(1)

        sub = subs.data[0]
        logger.info("Found active subscription: id=%s  status=%s", sub.id, sub.status)

        # Resolve tier using the same 5-layer chain as subscription_sync.py
        from subscription_sync import _resolve_tier_from_subscription
        resolved_tier = _resolve_tier_from_subscription(sub, user.id)
        logger.info("Resolved tier from Stripe: %s", resolved_tier)

        # Show what would change
        if user.subscription_tier == resolved_tier:
            logger.info("DB already correct — subscription_tier is already '%s'. No change needed.", resolved_tier)
        else:
            logger.info("Will update: %s → %s", user.subscription_tier, resolved_tier)

        if args.dry_run:
            logger.info("[DRY RUN] No changes written to DB.")
            return

        # Apply the sync
        old_tier = user.subscription_tier
        user.subscription_tier = resolved_tier
        if hasattr(user, "stripe_subscription_status"):
            user.stripe_subscription_status = sub.status
        if hasattr(user, "subscription_status"):
            user.subscription_status = sub.status
        if hasattr(user, "subscription_updated_at"):
            user.subscription_updated_at = datetime.now(timezone.utc)

        period_end = getattr(sub, "current_period_end", None)
        if period_end:
            expires_dt = datetime.fromtimestamp(int(period_end), tz=timezone.utc)
            if hasattr(user, "subscription_expires_at"):
                user.subscription_expires_at = expires_dt
            if hasattr(user, "subscription_ends_at"):
                user.subscription_ends_at = expires_dt

        db.commit()
        db.refresh(user)

        logger.info("✅ SUCCESS")
        logger.info("   user_id         : %s", user.id)
        logger.info("   username        : %s", user.username)
        logger.info("   email           : %s", user.email)
        logger.info("   tier (before)   : %s", old_tier)
        logger.info("   tier (after)    : %s", user.subscription_tier)
        logger.info("   customer_id     : %s", user.stripe_customer_id)
        logger.info("")
        logger.info("The dashboard will reflect the new tier on next page load.")

    except Exception as e:
        logger.error("Unexpected error: %s", e, exc_info=True)
        db.rollback()
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()

# ============================================================================
# END OF force_sync_user.py
# ============================================================================