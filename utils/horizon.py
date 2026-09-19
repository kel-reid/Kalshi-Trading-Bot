"""
Timestamp, Horizon, and String Helpers for Market Discovery

Pure helper functions for parsing timestamps, evaluating expiration horizons,
and formatting market text and numeric fields.
"""

import datetime
from typing import Any, Dict, Optional


def _parse_iso_timestamp(ts: Any) -> Optional[datetime.datetime]:
    """
    Safely parse an ISO-8601 timestamp string into a timezone-aware UTC datetime.
    Normalizes 'Z' to '+00:00', converts explicit offsets to UTC, and attaches UTC to offset-naive timestamps.
    Returns None if parsing fails, input is missing, or string is empty.
    """
    if ts is None:
        return None
    s = str(ts).strip()
    if not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        return None


def _is_within_horizon(
    m: Dict[str, Any],
    max_days: Optional[float],
    now_utc: Optional[datetime.datetime] = None,
) -> bool:
    """
    Return True if market close_time / expiration_time is within max_days from now.
    Returns True if max_days is None.
    If close_time is missing, returns True for backward-compatibility with untimed mock markets.
    If close_time is present, parses into UTC and enforces 0.0 <= days_remaining <= max_days.
    Fails closed (returns False) on any parse error, invalid timestamp, or out-of-horizon date.
    """
    if max_days is None:
        return True
    close_time_str = m.get("close_time") or m.get("expiration_time")
    if not close_time_str or not str(close_time_str).strip():
        return True

    close_dt = _parse_iso_timestamp(close_time_str)
    if close_dt is None:
        return False

    if now_utc is None:
        import sys
        md = sys.modules.get("utils.market_discovery")
        dt_module = getattr(md, "datetime", datetime) if md else datetime
        now_utc = dt_module.datetime.now(datetime.timezone.utc)

    days_remaining = (close_dt - now_utc).total_seconds() / 86400.0
    return 0.0 <= days_remaining <= max_days


def _parse_float(val: Any) -> float:
    """Safely parse string or numeric value to float, defaulting to 0.0 on error or None."""
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _text_for_market(m: Dict[str, Any]) -> str:
    """Combine ticker, title, and subtitle into a single uppercase searchable string."""
    return f"{m.get('ticker', '')} {m.get('title', '')} {m.get('subtitle', '')}".upper()
