# ============================================================================
# DATETIME FIX UTILITY - PixelPerfect API
# ============================================================================
# File: backend/datetime_fix.py
# Author: OneTechly
# Created: February 2026
# Purpose: Fix datetime comparison issues across the codebase
#
# ✅ PRODUCTION READY
# ✅ Ensures all datetime objects are timezone-aware using UTC
# ✅ Prevents "can't compare offset-naive and offset-aware" errors
# ============================================================================

from datetime import datetime, timezone
from typing import Optional
import logging

logger = logging.getLogger("pixelperfect")


def make_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """
    Convert naive datetime to UTC-aware datetime.
    
    Args:
        dt: Datetime object (can be naive or aware)
        
    Returns:
        UTC-aware datetime or None if input is None
        
    Examples:
        >>> naive_dt = datetime(2026, 2, 3, 12, 0, 0)
        >>> aware_dt = make_aware(naive_dt)
        >>> aware_dt.tzinfo
        datetime.timezone.utc
    """
    if dt is None:
        return None
    
    # Check if already aware
    if dt.tzinfo is not None and dt.tzinfo.utcoffset(dt) is not None:
        return dt
    
    # Make aware in UTC
    return dt.replace(tzinfo=timezone.utc)


def make_naive(dt: Optional[datetime]) -> Optional[datetime]:
    """
    Convert timezone-aware datetime to naive datetime (UTC).
    
    Args:
        dt: Datetime object (can be naive or aware)
        
    Returns:
        Naive datetime or None if input is None
        
    Warning:
        Only use this if you're certain all datetimes in your system are UTC.
        Prefer make_aware() for safer comparisons.
    """
    if dt is None:
        return None
    
    # If already naive, return as-is
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return dt
    
    # Convert to UTC and strip timezone
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def utc_now() -> datetime:
    """
    Return current UTC time as timezone-aware datetime.
    
    Returns:
        Current UTC time with timezone info
        
    Examples:
        >>> now = utc_now()
        >>> now.tzinfo
        datetime.timezone.utc
    """
    return datetime.now(timezone.utc)


def utc_now_naive() -> datetime:
    """
    Return current UTC time as naive datetime (no timezone).
    
    Returns:
        Current UTC time without timezone info
        
    Warning:
        Prefer utc_now() for timezone-aware operations.
    """
    return datetime.utcnow()


def compare_datetimes(dt1: Optional[datetime], dt2: Optional[datetime]) -> bool:
    """
    Safely compare two datetimes (makes both timezone-aware before comparison).
    
    Args:
        dt1: First datetime
        dt2: Second datetime
        
    Returns:
        True if dt1 < dt2, False otherwise
        
    Examples:
        >>> dt1 = datetime(2026, 1, 1)  # naive
        >>> dt2 = datetime(2026, 2, 1, tzinfo=timezone.utc)  # aware
        >>> compare_datetimes(dt1, dt2)
        True
    """
    if dt1 is None or dt2 is None:
        return False
    
    dt1_aware = make_aware(dt1)
    dt2_aware = make_aware(dt2)
    
    return dt1_aware < dt2_aware


def is_expired(expires_at: Optional[datetime], now: Optional[datetime] = None) -> bool:
    """
    Check if a datetime has passed (expired).
    
    Args:
        expires_at: Expiration datetime
        now: Current time (defaults to utc_now() if not provided)
        
    Returns:
        True if expired, False otherwise
        
    Examples:
        >>> past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        >>> is_expired(past)
        True
        
        >>> future = datetime(2030, 1, 1, tzinfo=timezone.utc)
        >>> is_expired(future)
        False
    """
    if expires_at is None:
        return False
    
    if now is None:
        now = utc_now()
    
    return compare_datetimes(expires_at, now)


def safe_parse_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    """
    Safely parse datetime string to timezone-aware datetime.
    
    Args:
        dt_str: ISO format datetime string
        
    Returns:
        Timezone-aware datetime or None if parsing fails
        
    Examples:
        >>> dt = safe_parse_datetime("2026-02-03T12:00:00Z")
        >>> dt.tzinfo
        datetime.timezone.utc
    """
    if not dt_str:
        return None
    
    try:
        # Try parsing with timezone
        dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        return make_aware(dt)
    except Exception as e:
        logger.warning(f"Failed to parse datetime '{dt_str}': {e}")
        return None


def format_datetime(dt: Optional[datetime], include_tz: bool = True) -> Optional[str]:
    """
    Format datetime to ISO string.
    
    Args:
        dt: Datetime to format
        include_tz: Include timezone in output
        
    Returns:
        ISO format string or None if input is None
        
    Examples:
        >>> dt = datetime(2026, 2, 3, 12, 0, 0, tzinfo=timezone.utc)
        >>> format_datetime(dt)
        '2026-02-03T12:00:00+00:00'
    """
    if dt is None:
        return None
    
    dt_aware = make_aware(dt)
    
    if include_tz:
        return dt_aware.isoformat()
    else:
        return dt_aware.replace(tzinfo=None).isoformat()


def days_until(dt: Optional[datetime]) -> Optional[int]:
    """
    Calculate days until a datetime.
    
    Args:
        dt: Target datetime
        
    Returns:
        Number of days (negative if in the past) or None if input is None
        
    Examples:
        >>> from datetime import timedelta
        >>> future = utc_now() + timedelta(days=5)
        >>> days_until(future)
        5
    """
    if dt is None:
        return None
    
    now = utc_now()
    dt_aware = make_aware(dt)
    
    delta = dt_aware - now
    return delta.days


def seconds_until(dt: Optional[datetime]) -> Optional[int]:
    """
    Calculate seconds until a datetime.
    
    Args:
        dt: Target datetime
        
    Returns:
        Number of seconds (negative if in the past) or None if input is None
    """
    if dt is None:
        return None
    
    now = utc_now()
    dt_aware = make_aware(dt)
    
    delta = dt_aware - now
    return int(delta.total_seconds())


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    "make_aware",
    "make_naive",
    "utc_now",
    "utc_now_naive",
    "compare_datetimes",
    "is_expired",
    "safe_parse_datetime",
    "format_datetime",
    "days_until",
    "seconds_until",
]

# ============================================================================
# END OF datetime_fix.py
# ============================================================================