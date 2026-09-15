"""
Market Discovery and Dynamic Market Rotation Utility

This module provides automated discovery of active Kalshi prediction markets.
It prioritizes high-interest categories (NFL, MLB, NBA, NHL, Soccer) with 
graceful fallbacks to macro indices and liquid commodities. It also provides
status verification to support seamless market rotation upon settlement or expiry.
"""

import logging
import random
import asyncio
from typing import List, Optional, Dict, Any
import datetime
import requests
import certifi

from config import BASE_URL

logger = logging.getLogger("MarketDiscovery")

SPORTS_KEYWORDS = ("NFL", "MLB", "NBA", "NHL", "EPL", "SOCCER", "NCAA", "UEFA", "FOOTBALL", "BASKETBALL", "BASEBALL", "HOCKEY")
FALLBACK_KEYWORDS = ("INX", "SPX", "NASDAQ", "NDX", "BTC", "ETH")


def fetch_eligible_markets(limit: int = 1000) -> List[Dict[str, Any]]:
    """
    Fetch active and tradeable markets from the Kalshi REST API.
    Excludes inactive markets, expired contracts, and internal composite / shard combo markets.
    """
    try:
        resp = requests.get(
            f"{BASE_URL}/trade-api/v2/markets",
            params={"limit": limit},
            verify=certifi.where(),
            timeout=10,
        )
        resp.raise_for_status()
        markets = resp.json().get("markets", [])
        
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        eligible = []
        for m in markets:
            if m.get("status") not in ("open", "active"):
                continue
            if str(m.get("ticker", "")).upper().startswith("KXMVE"):
                continue
            # Filter out markets whose close_time or expiration_time has passed
            close_time_str = m.get("close_time") or m.get("expiration_time")
            if close_time_str:
                try:
                    close_dt = datetime.datetime.fromisoformat(str(close_time_str).replace("Z", "+00:00"))
                    if close_dt <= now_utc:
                        continue
                except Exception:
                    pass
            eligible.append(m)
        return eligible
    except Exception as e:
        logger.error(f"Failed to fetch eligible markets: {e}")
        return []


def _text_for_market(m: Dict[str, Any]) -> str:
    """Combine ticker, title, and subtitle into a single uppercase searchable string."""
    return f"{m.get('ticker', '')} {m.get('title', '')} {m.get('subtitle', '')}".upper()


def _select_best_market(candidates: List[Dict[str, Any]]) -> Optional[str]:
    """
    Prioritize markets that have existing trading volume, open interest, or two-sided quotes.
    Sorts by liquidity and picks from the most liquid candidates.
    """
    if not candidates:
        return None

    def liquidity_key(m: Dict[str, Any]) -> int:
        vol = m.get("volume") or 0
        oi = m.get("open_interest") or 0
        has_quotes = 1 if (m.get("yes_bid") is not None and m.get("yes_ask") is not None) else 0
        return (has_quotes * 1_000_000) + vol + oi

    active_pool = [
        m for m in candidates
        if (m.get("volume", 0) > 0 or m.get("open_interest", 0) > 0 or 
            (m.get("yes_bid") is not None and m.get("yes_ask") is not None))
    ]
    pool = active_pool if active_pool else candidates
    pool.sort(key=liquidity_key, reverse=True)
    top_candidates = pool[:min(3, len(pool))]
    selected = random.choice(top_candidates)
    return selected.get("ticker")


def discover_active_market(
    target_preference: str = "",
    exclude_tickers: Optional[List[str]] = None,
) -> Optional[str]:
    """
    Select an active tradeable market ticker based on a target preference or category.
    
    Priority:
    1. Exact match for target_preference (if active and not excluded)
    2. Category/keyword match across ticker, title, and subtitle
    3. Major Sports market (NFL, MLB, NBA, NHL, Soccer, etc.)
    4. Liquid macro/crypto fallback markets (INX, SPX, BTC, ETH)
    5. Any active tradeable market with best liquidity
    """
    exclude = set(exclude_tickers or [])
    eligible = fetch_eligible_markets()
    tradeable_markets = [m for m in eligible if m.get("ticker") not in exclude]

    if not tradeable_markets:
        logger.warning("No tradeable markets available matching criteria.")
        return None

    pref = target_preference.strip().upper() if target_preference else ""

    # 1. Exact match by ticker
    if pref:
        exact = [m for m in tradeable_markets if m.get("ticker", "").upper() == pref]
        if exact:
            logger.info(f"Targeting exact matched market: {pref}")
            return exact[0].get("ticker")

    # 2. Category or keyword match (checking ticker, title, and subtitle)
    if pref:
        if pref in ("SPORTS", "SPORT", "MAJOR SPORTS"):
            matched = [m for m in tradeable_markets if any(k in _text_for_market(m) for k in SPORTS_KEYWORDS)]
        else:
            matched = [m for m in tradeable_markets if pref in _text_for_market(m)]

        if matched:
            selected = _select_best_market(matched)
            if selected:
                logger.info(f"Matched active market for '{target_preference}': {selected}")
                return selected
        logger.warning(f"No active markets found matching '{target_preference}'. Falling back to available sports...")

    # 3. Default: Major Sports
    sports_markets = [m for m in tradeable_markets if any(k in _text_for_market(m) for k in SPORTS_KEYWORDS)]
    if sports_markets:
        selected = _select_best_market(sports_markets)
        if selected:
            logger.info(f"Selected active Major Sports market: {selected}")
            return selected

    # 4. Fallback to liquid macro / crypto
    liquid_markets = [m for m in tradeable_markets if any(k in _text_for_market(m) for k in FALLBACK_KEYWORDS)]
    if liquid_markets:
        selected = _select_best_market(liquid_markets)
        if selected:
            logger.info(f"Selected liquid fallback market: {selected}")
            return selected

    # 5. Any active tradeable market (selecting best liquidity first)
    selected = _select_best_market(tradeable_markets)
    if selected:
        logger.info(f"Selected alternative tradeable market: {selected}")
        return selected
    return None


def check_market_status(ticker: str) -> Optional[str]:
    """
    Query the status of a specific market from Kalshi REST API.
    Returns status string (e.g., 'active', 'open', 'closed', 'settled') or None if error.
    Also verifies close_time to catch expired hourly contracts before batch settlement.
    """
    try:
        resp = requests.get(
            f"{BASE_URL}/trade-api/v2/markets/{ticker}",
            verify=certifi.where(),
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            market_info = data.get("market", data)
            status = market_info.get("status")

            # Check close_time / expiration_time
            close_time_str = market_info.get("close_time") or market_info.get("expiration_time")
            if close_time_str:
                try:
                    now_utc = datetime.datetime.now(datetime.timezone.utc)
                    close_dt = datetime.datetime.fromisoformat(str(close_time_str).replace("Z", "+00:00"))
                    if close_dt <= now_utc:
                        logger.info(f"Market {ticker} has passed its close_time ({close_time_str}); treating as closed.")
                        return "closed"
                except Exception:
                    pass

            return status
        logger.warning(f"Market status lookup for {ticker} returned HTTP {resp.status_code}")
        return None
    except Exception as e:
        logger.error(f"Error checking market status for {ticker}: {e}")
        return None


def is_market_active(ticker: str) -> Optional[bool]:
    """Return True/False for confirmed market status, or None if the status could not be determined."""
    status = check_market_status(ticker)
    if status is None:
        return None
    return status in ("open", "active")


async def discover_active_market_async(
    target_preference: str = "",
    exclude_tickers: Optional[List[str]] = None,
) -> Optional[str]:
    """Non-blocking async wrapper for discover_active_market."""
    return await asyncio.to_thread(discover_active_market, target_preference, exclude_tickers)


async def is_market_active_async(ticker: str) -> Optional[bool]:
    """Non-blocking async wrapper for is_market_active."""
    return await asyncio.to_thread(is_market_active, ticker)


async def check_market_status_async(ticker: str) -> Optional[str]:
    """Non-blocking async wrapper for check_market_status."""
    return await asyncio.to_thread(check_market_status, ticker)
