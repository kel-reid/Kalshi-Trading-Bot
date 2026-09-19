"""
Market Discovery and Dynamic Market Rotation Utility

This module orchestrates automated discovery and rotation of active Kalshi prediction markets.
It focuses on in-season major sports categories (NFL, NBA, MLB) with pre-flight orderbook
verification to support seamless market rotation upon settlement or expiry.

Sub-components are modularized across:
- utils.sports_router: Seasonal priority router and league configurations
- utils.horizon: Expiration horizon filtering, ISO timestamp parsing, and text helpers
- utils.market_api: Kalshi REST ingestion, orderbook quotes verification, and status querying
- utils.market_scoring: Liquidity scoring, candidate ranking, and probing budget enforcement

All symbols are re-exported here for 100% backward compatibility.
See docs/SPORTS_SEASON_ROUTER.md for full architecture and seasonal matrix.
"""

import asyncio
import datetime
import logging
from typing import Any, Dict, List, Optional

from config import MAX_EXPIRATION_DAYS
from utils.horizon import (
    _is_within_horizon,
    _parse_float,
    _parse_iso_timestamp,
    _text_for_market,
)
from utils.market_api import (
    check_market_status,
    check_orderbook_has_quotes,
    fetch_eligible_markets,
)
from utils.market_scoring import (
    DEFAULT_MAX_PROBES_PER_SERIES,
    DEFAULT_MAX_TARGETED_SERIES_FALLBACKS,
    DEFAULT_MAX_TOTAL_PROBES,
    _liquidity_key,
    _markets_for_series,
    _select_best_market,
)
from utils.sports_router import (
    SPORTS_KEYWORDS,
    SportsSeasonRouter,
)

logger = logging.getLogger("MarketDiscovery")

__all__ = [
    "SPORTS_KEYWORDS",
    "SportsSeasonRouter",
    "_parse_iso_timestamp",
    "_is_within_horizon",
    "_parse_float",
    "_text_for_market",
    "fetch_eligible_markets",
    "check_orderbook_has_quotes",
    "check_market_status",
    "is_market_active",
    "DEFAULT_MAX_TOTAL_PROBES",
    "DEFAULT_MAX_PROBES_PER_SERIES",
    "DEFAULT_MAX_TARGETED_SERIES_FALLBACKS",
    "_liquidity_key",
    "_select_best_market",
    "_markets_for_series",
    "discover_active_market",
    "discover_active_market_async",
    "is_market_active_async",
    "check_market_status_async",
]


# ============================================================================
# Market Discovery Orchestration & Seasonal Cascade
# ============================================================================

def discover_active_market(
    target_preference: str = "",
    exclude_tickers: Optional[List[str]] = None,
    preflight_check: bool = True,
    max_total_probes: int = DEFAULT_MAX_TOTAL_PROBES,
    max_expiration_days: Optional[float] = None,
    max_targeted_series_fallbacks: int = DEFAULT_MAX_TARGETED_SERIES_FALLBACKS,
) -> Optional[str]:
    """
    Select an active tradeable market ticker based on a target preference or category.
    
    Priority:
    1. Exact match for target_preference (if active and not excluded)
    2. In-Season Sports Router (waterfall across NFL, NBA, MLB suites based on calendar month):
       - Tier 1A: Primary Moneylines across active in-season leagues (NFL -> NBA -> MLB)
       - Tier 1B: Secondary Game Lines (Spreads & Totals) across active in-season leagues
       - Tier 2: Player Props across active in-season leagues (NFL -> NBA -> MLB)
       - Tier 3: General League tradeable sports markets
    3. Category/keyword match across ticker, title, and subtitle
    4. Safely return None and idle if no active sports markets with two-sided quotes are found
    """
    exclude = set(exclude_tickers or [])
    pref = target_preference.strip().upper() if target_preference else ""
    budget_tracker: Dict[str, int] = {
        "remaining": max(0, max_total_probes),
        "targeted_fallbacks_remaining": (
            max(0, min(max_targeted_series_fallbacks, max_total_probes))
            if preflight_check
            else max(0, max_targeted_series_fallbacks)
        ),
    }
    if max_expiration_days is None:
        max_expiration_days = float(MAX_EXPIRATION_DAYS)

    now_utc = datetime.datetime.now(datetime.timezone.utc)

    # 1. Fetch eligible markets ONCE and filter in memory to protect REST rate limits
    eligible = fetch_eligible_markets()
    tradeable_markets = [m for m in eligible if m.get("ticker") not in exclude]

    if not tradeable_markets:
        logger.warning("No tradeable markets available matching criteria.")
        return None

    target_leagues: Optional[List[str]] = None

    # Check if target preference indicates sports or is default/unspecified
    is_sports_pref = pref in (
        "NFL", "FOOTBALL", "NBA", "BASKETBALL", "MLB", "BASEBALL",
        "SPORTS", "SPORT", "MAJOR SPORTS"
    ) or not pref

    if is_sports_pref:
        if pref in ("NFL", "FOOTBALL"):
            target_leagues = ["NFL"]
        elif pref in ("NBA", "BASKETBALL"):
            target_leagues = ["NBA"]
        elif pref in ("MLB", "BASEBALL"):
            target_leagues = ["MLB"]
        else:
            target_leagues = SportsSeasonRouter.get_in_season_leagues()
    else:
        # 2. Exact match by ticker (if explicitly targeted and not a category keyword)
        # Operators explicitly targeting a specific contract are permitted regardless of horizon.
        exact = [m for m in tradeable_markets if m.get("ticker", "").upper() == pref]
        if exact:
            exact_ticker = exact[0].get("ticker")
            if preflight_check:
                if budget_tracker is not None and budget_tracker.get("remaining", 0) <= 0:
                    logger.info(f"Orderbook probe quota exhausted; cannot screen exact matched market: {pref}")
                    return None
                if budget_tracker is not None:
                    budget_tracker["remaining"] -= 1
                if exact_ticker and check_orderbook_has_quotes(exact_ticker):
                    logger.info(f"Targeting exact matched market with active quotes: {pref}")
                    return exact_ticker
                logger.info(f"Exact matched market {pref} has no active quotes.")
                return None
            else:
                logger.info(f"Targeting exact matched market: {pref}")
                return exact_ticker

        # 3. Category or keyword match (for explicit non-sports target preference)
        # Operators explicitly targeting a non-sports category (e.g. INX, FED, CPI) are not constrained by sports horizon.
        matched = [
            m for m in tradeable_markets
            if pref in _text_for_market(m)
        ]
        if matched:
            selected = _select_best_market(
                matched,
                preflight_check=preflight_check,
                budget_tracker=budget_tracker,
            )
            if selected:
                logger.info(f"Matched active market for '{target_preference}': {selected}")
                return selected

        # 4. Unmatched or excluded exact ticker (e.g. during auto-rotation after settlement/starvation)
        # Route to its detected league or seasonal fallback so rotation can find an active replacement.
        if pref.startswith("KXNFL") or "NFL" in pref or "FOOTBALL" in pref:
            target_leagues = ["NFL"]
            logger.info(f"Routing unmatched/excluded target '{target_preference}' to NFL suite for auto-rotation.")
        elif pref.startswith("KXNBA") or "NBA" in pref or "BASKETBALL" in pref:
            target_leagues = ["NBA"]
            logger.info(f"Routing unmatched/excluded target '{target_preference}' to NBA suite for auto-rotation.")
        elif pref.startswith("KXMLB") or "MLB" in pref or "BASEBALL" in pref:
            target_leagues = ["MLB"]
            logger.info(f"Routing unmatched/excluded target '{target_preference}' to MLB suite for auto-rotation.")
        else:
            target_leagues = SportsSeasonRouter.get_in_season_leagues()
            logger.info(f"Routing unmatched/excluded target '{target_preference}' to seasonal sports fallback for auto-rotation.")

    queried_series: set[str] = set()

    # Helper: retrieve candidate markets for a specific series within the rolling weekly horizon,
    # falling back to a targeted series REST query if the global /events pagination missed it.
    def _ensure_series_markets(series: str) -> List[Dict[str, Any]]:
        nonlocal tradeable_markets
        filtered = [
            m for m in _markets_for_series(tradeable_markets, series)
            if _is_within_horizon(m, max_expiration_days, now_utc)
        ]
        if filtered:
            return filtered

        series_key = series.upper()
        if series_key in queried_series:
            return []

        # Bound targeted series queries under the shared probe budget (if probing) and fallback limit
        if budget_tracker["targeted_fallbacks_remaining"] <= 0 or (
            preflight_check and budget_tracker["remaining"] <= 0
        ):
            logger.info(f"Probe budget exhausted or targeted fallback limit reached; skipping fallback fetch for series: {series}")
            return []

        if preflight_check:
            budget_tracker["remaining"] -= 1
        budget_tracker["targeted_fallbacks_remaining"] -= 1
        queried_series.add(series_key)

        try:
            targeted = fetch_eligible_markets(
                series_ticker=series,
                max_expiration_days=max_expiration_days,
            )
            if targeted:
                known_tickers = {m.get("ticker") for m in tradeable_markets}
                new_items = [
                    m for m in targeted
                    if m.get("ticker") not in exclude
                    and m.get("ticker") not in known_tickers
                    and _is_within_horizon(m, max_expiration_days, now_utc)
                ]
                tradeable_markets.extend(new_items)
                return [
                    m for m in _markets_for_series(tradeable_markets, series)
                    if _is_within_horizon(m, max_expiration_days, now_utc)
                ]
        except Exception as e_err:
            logger.debug(f"Targeted series fetch for {series} failed: {e_err}")
        return []

    # 5. SportsSeasonRouter Waterfall for in-season leagues and full product suites
    if target_leagues:
        # Tier 1A: Primary Moneylines across all active in-season leagues (NFL -> NBA -> MLB)
        for league in target_leagues:
            if preflight_check and budget_tracker["remaining"] <= 0:
                logger.info("Probe quota exhausted during Tier 1A Primary Moneylines; stopping further probes.")
                break

            primary_series = SportsSeasonRouter.get_primary_series_for_league(league)
            if primary_series:
                series_markets = _ensure_series_markets(primary_series)
                if series_markets:
                    selected = _select_best_market(
                        series_markets,
                        preflight_check=preflight_check,
                        max_probes=DEFAULT_MAX_PROBES_PER_SERIES,
                        budget_tracker=budget_tracker,
                    )
                    if selected:
                        logger.info(f"SportsSeasonRouter selected active {league} Primary Moneyline ({primary_series}): {selected}")
                        return selected

        # Tier 1B: Secondary Game Lines (Spreads & Totals) across active in-season leagues
        for league in target_leagues:
            if preflight_check and budget_tracker["remaining"] <= 0:
                logger.info("Probe quota exhausted during Tier 1B Secondary Game Lines; stopping further probes.")
                break

            game_series = SportsSeasonRouter.get_game_lines_for_league(league)
            primary_series = SportsSeasonRouter.get_primary_series_for_league(league)
            secondary_series = [s for s in game_series if s != primary_series]

            for series_ticker in secondary_series:
                if preflight_check and budget_tracker["remaining"] <= 0:
                    break

                series_markets = _ensure_series_markets(series_ticker)
                if series_markets:
                    selected = _select_best_market(
                        series_markets,
                        preflight_check=preflight_check,
                        max_probes=DEFAULT_MAX_PROBES_PER_SERIES,
                        budget_tracker=budget_tracker,
                    )
                    if selected:
                        logger.info(f"SportsSeasonRouter selected active {league} Game Line ({series_ticker}): {selected}")
                        return selected

        # Tier 2: Cascade to Player Props across active in-season leagues
        for league in target_leagues:
            if preflight_check and budget_tracker["remaining"] <= 0:
                logger.info("Probe quota exhausted during Tier 2 Player Props; stopping further probes.")
                break

            props_series = SportsSeasonRouter.get_props_for_league(league)
            for series_ticker in props_series:
                if preflight_check and budget_tracker["remaining"] <= 0:
                    break

                series_markets = _ensure_series_markets(series_ticker)
                if series_markets:
                    selected = _select_best_market(
                        series_markets,
                        preflight_check=preflight_check,
                        max_probes=DEFAULT_MAX_PROBES_PER_SERIES,
                        budget_tracker=budget_tracker,
                    )
                    if selected:
                        logger.info(f"SportsSeasonRouter selected active {league} Player Prop ({series_ticker}): {selected}")
                        return selected

        # Tier 3: General League tradeable sports markets (strictly within weekly expiration horizon)
        all_configured_series = {
            s.upper()
            for l in target_leagues
            for s in SportsSeasonRouter.get_series_for_league(l)
        }
        for league in target_leagues:
            if preflight_check and budget_tracker["remaining"] <= 0:
                break
            league_prefix = f"KX{league}"
            league_markets = [
                m for m in tradeable_markets
                if (
                    str(m.get("ticker", "")).upper().startswith(league_prefix)
                    or league in _text_for_market(m)
                )
                and m.get("series_ticker", "").upper() not in all_configured_series
                and not any(str(m.get("ticker", "")).upper().startswith(f"{s}-") for s in all_configured_series)
                and _is_within_horizon(m, max_expiration_days, now_utc)
            ]
            if league_markets:
                selected = _select_best_market(
                    league_markets,
                    preflight_check=preflight_check,
                    max_probes=DEFAULT_MAX_PROBES_PER_SERIES,
                    budget_tracker=budget_tracker,
                )
                if selected:
                    logger.info(f"SportsSeasonRouter selected active general {league} market: {selected}")
                    return selected

    logger.warning("No active in-season sports markets with two-sided quotes found. Idling until market activity resumes.")
    return None


def is_market_active(ticker: str) -> Optional[bool]:
    """Return True/False for confirmed market status, or None if the status could not be determined."""
    status = check_market_status(ticker)
    if status is None:
        return None
    return status in ("open", "active")


# ============================================================================
# Async Wrappers
# ============================================================================


async def discover_active_market_async(
    target_preference: str = "",
    exclude_tickers: Optional[List[str]] = None,
    preflight_check: bool = True,
    max_total_probes: int = DEFAULT_MAX_TOTAL_PROBES,
    max_expiration_days: Optional[float] = None,
    max_targeted_series_fallbacks: int = DEFAULT_MAX_TARGETED_SERIES_FALLBACKS,
) -> Optional[str]:
    """Non-blocking async wrapper for discover_active_market."""
    return await asyncio.to_thread(
        discover_active_market,
        target_preference,
        exclude_tickers,
        preflight_check,
        max_total_probes,
        max_expiration_days,
        max_targeted_series_fallbacks,
    )


async def is_market_active_async(ticker: str) -> Optional[bool]:
    """Non-blocking async wrapper for is_market_active."""
    return await asyncio.to_thread(is_market_active, ticker)


async def check_market_status_async(ticker: str) -> Optional[str]:
    """Non-blocking async wrapper for check_market_status."""
    return await asyncio.to_thread(check_market_status, ticker)
