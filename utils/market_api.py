"""
Kalshi Market API & Orderbook Verification Client

Handles REST API interactions with Kalshi for fetching eligible markets,
probing live orderbook quotes, and querying market lifecycle status.
"""

import datetime
import logging
from typing import Any, Dict, List, Optional
import certifi
import requests

from config import BASE_URL
from utils.horizon import _is_within_horizon, _parse_iso_timestamp

logger = logging.getLogger("MarketDiscovery")


def _get_base_url() -> str:
    """Retrieve BASE_URL, honoring any mock on utils.market_discovery."""
    import sys
    md = sys.modules.get("utils.market_discovery")
    if md is not None and hasattr(md, "BASE_URL"):
        return getattr(md, "BASE_URL")
    return BASE_URL


def _get_requests() -> Any:
    """Retrieve requests module, honoring any mock on utils.market_discovery."""
    import sys
    md = sys.modules.get("utils.market_discovery")
    if md is not None and hasattr(md, "requests"):
        return getattr(md, "requests")
    return requests


def _get_certifi() -> Any:
    """Retrieve certifi module, honoring any mock on utils.market_discovery."""
    import sys
    md = sys.modules.get("utils.market_discovery")
    if md is not None and hasattr(md, "certifi"):
        return getattr(md, "certifi")
    return certifi


def fetch_eligible_markets(
    limit: int = 1000,
    series_ticker: Optional[str] = None,
    max_expiration_days: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch active and tradeable markets from the Kalshi REST API.
    Queries /events with with_nested_markets=true to discover real underlying event markets
    without getting exhausted by synthetic multivariate shards (KXMVE), falling back to /markets if needed.
    Supports optional series_ticker filter for high-turnover series (e.g. KXNFLGAME).
    Supports optional max_expiration_days filter to restrict ingestion to rolling weekly windows.
    Excludes inactive markets, expired contracts, and internal composite / shard combo markets.
    """
    try:
        markets = []
        base_url = _get_base_url()
        req_lib = _get_requests()
        cert_path = _get_certifi().where()

        # 1. Primary: Query /events with nested markets (bypasses synthetic KXMVE shards)
        cursor: Optional[str] = None
        max_event_pages = 10  # defensive bound to prevent infinite pagination
        pages_fetched = 0

        try:
            while pages_fetched < max_event_pages:
                params: Dict[str, Any] = {
                    "status": "open",
                    "with_nested_markets": "true",
                    "limit": 200,
                }
                if series_ticker:
                    params["series_ticker"] = series_ticker
                if cursor:
                    params["cursor"] = cursor

                resp = req_lib.get(
                    f"{base_url}/trade-api/v2/events",
                    params=params,
                    verify=cert_path,
                    timeout=10,
                )
                if resp.status_code != 200:
                    logger.warning(
                        f"Non-200 response from /events ({resp.status_code}): {resp.text[:200]}"
                    )
                    break

                data = resp.json()
                events = data.get("events", [])
                if not events:
                    break

                for e in events:
                    e_title = e.get("title", "")
                    e_sub = e.get("sub_title") or e.get("subtitle", "")
                    for m in e.get("markets", []):
                        if not m.get("title") and e_title:
                            m["title"] = e_title
                        if not m.get("subtitle") and e_sub:
                            m["subtitle"] = e_sub
                        markets.append(m)
                        if len(markets) >= limit:
                            break
                    if len(markets) >= limit:
                        break

                pages_fetched += 1
                cursor = data.get("cursor")
                if not cursor or len(markets) >= limit:
                    break
        except Exception as e_err:
            logger.warning(f"Failed to query /events: {e_err}; falling back to /markets if needed")

        # Guarantee markets does not exceed requested limit
        markets = markets[:limit]

        # 2. Fallback: Query /markets if /events was unavailable or returned no markets
        if not markets:
            fallback_params: Dict[str, Any] = {"limit": limit, "status": "open"}
            if series_ticker:
                fallback_params["series_ticker"] = series_ticker
            resp = req_lib.get(
                f"{base_url}/trade-api/v2/markets",
                params=fallback_params,
                verify=cert_path,
                timeout=10,
            )
            resp.raise_for_status()
            markets = resp.json().get("markets", [])

        import sys
        md = sys.modules.get("utils.market_discovery")
        dt_module = getattr(md, "datetime", datetime) if md else datetime
        now_utc = dt_module.datetime.now(datetime.timezone.utc)

        eligible = []
        for m in markets:
            # NOTE (Kalshi API Quirk): Kalshi accepts status="open" as query param,
            # but returns markets with status="active" (or "open") in the response payload.
            # Both values must be permitted.
            if m.get("status") not in ("open", "active"):
                continue
            if str(m.get("ticker", "")).upper().startswith(("KXMVE", "KXNHL")):
                continue
            # Filter out markets whose close_time or expiration_time has passed
            close_time_str = m.get("close_time") or m.get("expiration_time")
            if close_time_str:
                close_dt = _parse_iso_timestamp(close_time_str)
                if close_dt is not None and close_dt <= now_utc:
                    continue
            if not _is_within_horizon(m, max_expiration_days, now_utc):
                continue
            eligible.append(m)

        logger.info(
            f"Queried Kalshi markets: received {len(markets)} raw markets, {len(eligible)} eligible."
        )
        if not eligible and markets:
            statuses = set(m.get("status") for m in markets)
            logger.warning(
                f"All {len(markets)} returned markets were filtered out. Observed statuses: {statuses}"
            )
        return eligible
    except Exception as e:
        logger.error(f"Failed to fetch eligible markets: {e}")
        return []


def check_orderbook_has_quotes(ticker: str) -> bool:
    """
    Check if a market's live orderbook currently has active two-sided quotes (bids and asks).
    Makes a lightweight REST check to avoid selecting dormant contracts.
    """
    try:
        base_url = _get_base_url()
        req_lib = _get_requests()
        cert_path = _get_certifi().where()
        resp = req_lib.get(
            f"{base_url}/trade-api/v2/markets/{ticker}/orderbook",
            verify=cert_path,
            timeout=3,
        )
        if resp.status_code == 200:
            data = resp.json()
            ob = data.get("orderbook_fp") or data.get("orderbook") or {}
            yes_bids = (
                ob.get("yes_dollars_fp") or ob.get("yes_dollars") or ob.get("yes") or []
            )
            no_bids = (
                ob.get("no_dollars_fp") or ob.get("no_dollars") or ob.get("no") or []
            )
            return bool(len(yes_bids) > 0 and len(no_bids) > 0)
    except Exception as e:
        logger.debug(f"Pre-flight orderbook check for {ticker} failed: {e}")
    return False


def check_market_status(ticker: str) -> Optional[str]:
    """
    Query the status of a specific market from Kalshi REST API.
    Returns status string (e.g., 'active', 'open', 'closed', 'settled') or None if error.
    Also verifies close_time to catch expired hourly contracts before batch settlement.
    """
    try:
        base_url = _get_base_url()
        req_lib = _get_requests()
        cert_path = _get_certifi().where()
        resp = req_lib.get(
            f"{base_url}/trade-api/v2/markets/{ticker}",
            verify=cert_path,
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            market_info = data.get("market", data)
            status = market_info.get("status")

            # Check close_time / expiration_time
            close_time_str = market_info.get("close_time") or market_info.get("expiration_time")
            if close_time_str:
                close_dt = _parse_iso_timestamp(close_time_str)
                if close_dt is not None:
                    import sys
                    md = sys.modules.get("utils.market_discovery")
                    dt_module = getattr(md, "datetime", datetime) if md else datetime
                    now_utc = dt_module.datetime.now(datetime.timezone.utc)
                    if close_dt <= now_utc:
                        logger.info(f"Market {ticker} has passed its close_time ({close_time_str}); treating as closed.")
                        return "closed"

            return status
        logger.warning(f"Market status lookup for {ticker} returned HTTP {resp.status_code}")
        return None
    except Exception as e:
        logger.error(f"Error checking market status for {ticker}: {e}")
        return None


def _get_market_status_checker() -> Any:
    """Retrieve check_market_status function, honoring any mock in utils.market_discovery."""
    import sys
    md = sys.modules.get("utils.market_discovery")
    if md is not None and hasattr(md, "check_market_status"):
        return getattr(md, "check_market_status")
    return check_market_status


def is_market_active(ticker: str) -> Optional[bool]:
    """Return True/False for confirmed market status, or None if the status could not be determined."""
    checker = _get_market_status_checker()
    status = checker(ticker)
    if status is None:
        return None
    return status in ("open", "active")

