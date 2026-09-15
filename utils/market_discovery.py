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
import requests
import certifi

from config import BASE_URL

logger = logging.getLogger("MarketDiscovery")

SPORTS_KEYWORDS = ("NFL", "MLB", "NBA", "NHL", "EPL", "SOCCER", "NCAA", "UEFA")
FALLBACK_KEYWORDS = ("INX", "SPX", "NASDAQ", "NDX", "BTC", "ETH")


def fetch_eligible_markets(limit: int = 1000) -> List[Dict[str, Any]]:
    """
    Fetch active and tradeable markets from the Kalshi REST API.
    Excludes inactive markets and internal composite / shard combo markets.
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
        
        eligible = [
            m for m in markets
            if m.get("status") in ("open", "active")
            and not str(m.get("ticker", "")).upper().startswith("KXMVE")
        ]
        return eligible
    except Exception as e:
        logger.error(f"Failed to fetch eligible markets: {e}")
        return []


def discover_active_market(
    target_preference: str = "",
    exclude_tickers: Optional[List[str]] = None,
) -> Optional[str]:
    """
    Select an active tradeable market ticker based on a target preference or category.
    
    Priority:
    1. Exact match for target_preference (if active and not excluded)
    2. Category/keyword match for target_preference (e.g. 'NFL', 'SPORTS')
    3. Major Sports market (NFL, MLB, NBA, NHL, Soccer, etc.)
    4. Liquid macro/crypto fallback markets (INX, SPX, BTC, ETH)
    5. Any active tradeable market
    """
    exclude = set(exclude_tickers or [])
    eligible = fetch_eligible_markets()
    tradeable_tickers = [m["ticker"] for m in eligible if m.get("ticker") not in exclude]

    if not tradeable_tickers:
        logger.warning("No tradeable markets available matching criteria.")
        return None

    pref = target_preference.strip().upper() if target_preference else ""

    # 1. Exact match
    if pref and pref in tradeable_tickers:
        logger.info(f"Targeting exact matched market: {pref}")
        return pref

    # 2. Category or keyword match
    if pref:
        if pref in ("SPORTS", "SPORT", "MAJOR SPORTS"):
            matched = [m for m in tradeable_tickers if any(k in m.upper() for k in SPORTS_KEYWORDS)]
        else:
            matched = [m for m in tradeable_tickers if pref in m.upper()]

        if matched:
            selected = random.choice(matched)
            logger.info(f"Matched active market for '{target_preference}': {selected}")
            return selected
        logger.warning(f"No active markets found matching '{target_preference}'. Falling back to available sports...")

    # 3. Default: Major Sports
    sports_markets = [m for m in tradeable_tickers if any(k in m.upper() for k in SPORTS_KEYWORDS)]
    if sports_markets:
        selected = random.choice(sports_markets)
        logger.info(f"Selected active Major Sports market: {selected}")
        return selected

    # 4. Fallback to liquid macro / crypto
    liquid_markets = [m for m in tradeable_tickers if any(k in m.upper() for k in FALLBACK_KEYWORDS)]
    if liquid_markets:
        selected = random.choice(liquid_markets)
        logger.info(f"Selected liquid fallback market: {selected}")
        return selected

    # 5. Any active tradeable market
    selected = random.choice(tradeable_tickers)
    logger.info(f"Selected alternative tradeable market: {selected}")
    return selected


def check_market_status(ticker: str) -> Optional[str]:
    """
    Query the status of a specific market from Kalshi REST API.
    Returns status string (e.g., 'active', 'open', 'closed', 'settled') or None if error.
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
            return market_info.get("status")
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
