"""
ESPN Daily Slate Scanner Client

Fetches live daily game schedules across major US sports leagues (NBA, NFL, NCAAF, MLB)
using ESPN's public hidden REST scoreboard endpoints.
Provides game counts for the current calendar day in US Eastern Time (ET) to dynamically
elevate leagues with active daily slates over leagues on off-days.
"""

import asyncio
import datetime
import logging
import time
from typing import Any, Dict, List, Optional
import zoneinfo

import certifi
import requests

logger = logging.getLogger("ESPNClient")

ET_TZ = zoneinfo.ZoneInfo("America/New_York")

ESPN_SCOREBOARD_URLS = {
    "NBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "NFL": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
    "NCAAF": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard",
    "MLB": "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
}

# Cache structure: { "date_str": str, "timestamp": float, "counts": Dict[str, int] }
_SLATE_CACHE: Dict[str, Any] = {
    "date_str": None,
    "timestamp": 0.0,
    "counts": {},
}
CACHE_TTL_SECONDS: float = 1800.0  # 30 minutes


def clear_slate_cache() -> None:
    """Clear in-memory slate cache (used primarily for unit testing)."""
    global _SLATE_CACHE
    _SLATE_CACHE = {
        "date_str": None,
        "timestamp": 0.0,
        "counts": {},
    }


def fetch_espn_league_games_today(league: str, target_date_et: datetime.date, timeout: float = 3.0) -> Optional[int]:
    """
    Query ESPN scoreboard for a given league and count games scheduled or live on target_date_et.
    Returns integer game count on success (>= 0), or None on error, timeout, or unknown league.
    """
    url = ESPN_SCOREBOARD_URLS.get(league.upper())
    if not url:
        return None

    try:
        # Date parameter formatted as YYYYMMDD (e.g. 20261021)
        date_str = target_date_et.strftime("%Y%m%d")
        params = {"dates": date_str}
        resp = requests.get(url, params=params, timeout=timeout, verify=certifi.where())
        if resp.status_code != 200:
            logger.debug(f"ESPN scoreboard for {league} returned HTTP {resp.status_code}")
            return None

        data = resp.json()
        events = data.get("events", [])
        if not isinstance(events, list):
            return None

        active_count = 0
        for event in events:
            # Check event date in US Eastern Time
            event_date_str = event.get("date")
            if not event_date_str:
                continue
            try:
                # ISO-8601 format e.g. "2026-10-21T23:00Z"
                dt = datetime.datetime.fromisoformat(event_date_str.replace("Z", "+00:00"))
                event_date_et = dt.astimezone(ET_TZ).date()
            except Exception:
                continue

            # Must match target date in ET
            if event_date_et != target_date_et:
                continue

            # Exclude postponed or canceled games
            status_desc = (
                event.get("status", {})
                .get("type", {})
                .get("name", "")
                .upper()
            )
            if "STATUS_POSTPONED" in status_desc or "STATUS_CANCELED" in status_desc:
                continue

            active_count += 1

        return active_count
    except Exception as e:
        logger.debug(f"Failed to fetch ESPN scoreboard for {league}: {e}")
        return None


def get_daily_slate_counts(
    dt: Optional[datetime.datetime] = None,
    ttl_seconds: float = CACHE_TTL_SECONDS,
    leagues: Optional[List[str]] = None,
) -> Dict[str, Optional[int]]:
    """
    Get the count of active/scheduled games today across requested leagues.
    Results are cached by date and TTL to prevent rate limits or redundant network calls.
    Merges missing leagues when a different league set is requested for the same date.
    Returns without caching if any requested league fails to fetch (preserving fail-open semantics).
    """
    global _SLATE_CACHE

    now_utc = datetime.datetime.now(datetime.timezone.utc) if dt is None else dt
    if now_utc.tzinfo is None:
        target_dt_et = now_utc.replace(tzinfo=datetime.timezone.utc).astimezone(ET_TZ)
    else:
        target_dt_et = now_utc.astimezone(ET_TZ)

    target_date = target_dt_et.date()
    target_date_str = target_date.isoformat()
    now_monotonic = time.monotonic()

    target_leagues = leagues or list(ESPN_SCOREBOARD_URLS.keys())

    # Check cache date & TTL validity
    cache_valid = (
        _SLATE_CACHE["date_str"] == target_date_str
        and (now_monotonic - _SLATE_CACHE["timestamp"]) < ttl_seconds
        and isinstance(_SLATE_CACHE.get("counts"), dict)
    )

    if not cache_valid:
        # Reset cache on date change or TTL expiration
        _SLATE_CACHE = {
            "date_str": target_date_str,
            "timestamp": now_monotonic,
            "counts": {},
        }

    cached_counts: Dict[str, int] = _SLATE_CACHE["counts"]
    missing_leagues = [l for l in target_leagues if l not in cached_counts]

    if not missing_leagues:
        return {l: cached_counts.get(l) for l in target_leagues}

    # Fetch missing leagues
    fetched_counts: Dict[str, Optional[int]] = {}
    for league in missing_leagues:
        count = fetch_espn_league_games_today(league, target_date)
        fetched_counts[league] = count

    # If any league failed (returned None), do not cache partial results; return uncommitted union
    if any(count is None for count in fetched_counts.values()):
        result: Dict[str, Optional[int]] = {}
        for l in target_leagues:
            if l in cached_counts:
                result[l] = cached_counts[l]
            else:
                result[l] = fetched_counts.get(l)
        return result

    # All requested leagues fetched successfully: merge into shared cache
    cached_counts.update(fetched_counts)
    _SLATE_CACHE["timestamp"] = now_monotonic

    logger.info(f"ESPN Daily Slate Counts for {target_date_str}: {cached_counts}")
    return {l: cached_counts.get(l) for l in target_leagues}


async def get_daily_slate_counts_async(
    dt: Optional[datetime.datetime] = None,
    ttl_seconds: float = CACHE_TTL_SECONDS,
    leagues: Optional[List[str]] = None,
) -> Dict[str, Optional[int]]:
    """Non-blocking async wrapper for get_daily_slate_counts."""
    return await asyncio.to_thread(get_daily_slate_counts, dt, ttl_seconds, leagues)

