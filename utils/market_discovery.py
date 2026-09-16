"""
Market Discovery and Dynamic Market Rotation Utility

This module provides automated discovery of active Kalshi prediction markets.
It focuses exclusively on in-season major sports categories (NFL, NBA, MLB) with
pre-flight orderbook verification to support seamless market rotation upon settlement or expiry.
Includes SportsSeasonRouter to dynamically prioritize in-season sports suites
(game lines and player props) across NFL, NBA, and MLB.
See docs/SPORTS_SEASON_ROUTER.md for full architecture and seasonal matrix.
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

SPORTS_KEYWORDS = ("NFL", "MLB", "NBA", "FOOTBALL", "BASKETBALL", "BASEBALL")


# ============================================================================
# Sports Season Router & Seasonal Priority Matrix
# ============================================================================

class SportsSeasonRouter:
    """
    Routes market discovery to the optimal in-season sports suites based on calendar dynamics.
    Defines the full suites (game lines + player props) for NFL, NBA, and MLB.
    Excludes low-liquidity leagues and penalizes distant multi-year futures.
    See docs/SPORTS_SEASON_ROUTER.md for full architecture and seasonal priority calendar.
    """
    # Game Lines & Player Props suites per league
    NFL_GAME_LINES = ("KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL")
    NFL_PROPS = (
        "KXNFLANYTD", "KXNFLPASSYDS", "KXNFLRUSHYDS", "KXNFLRECYDS", "KXNFLPASSTD"
    )
    NFL_SERIES = NFL_GAME_LINES + NFL_PROPS

    NBA_GAME_LINES = ("KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL")
    NBA_PROPS = (
        "KXNBAPTS", "KXNBAREB", "KXNBAAST", "KXNBA3PT", "KXNBAPRA"
    )
    NBA_SERIES = NBA_GAME_LINES + NBA_PROPS

    MLB_GAME_LINES = ("KXMLBGAME", "KXMLBRUNLINE", "KXMLBTOTAL")
    MLB_PROPS = (
        "KXMLBSTRIKEOUT", "KXMLBHR", "KXMLBHITS", "KXMLBTOTALBASES"
    )
    MLB_SERIES = MLB_GAME_LINES + MLB_PROPS

    ALL_IN_SEASON_PREFIXES = ("KXNFL", "KXNBA", "KXMLB")

    @classmethod
    def get_in_season_leagues(cls, dt: Optional[datetime.datetime] = None) -> List[str]:
        """
        Return ordered list of active leagues ('NFL', 'NBA', 'MLB') based on calendar month.
        - Sep: NFL primary, MLB secondary (postseason race; NBA excluded)
        - Oct: Triple overlap: NFL primary, NBA secondary, MLB tertiary (World Series)
        - Nov - Feb: NFL primary, NBA secondary (MLB season concluded)
        - Mar - Jun: NBA primary (playoffs), MLB secondary (opening/regular season)
        - Jul - Aug: MLB primary (summer lull: MLB only; NFL preseason excluded)
        """
        if dt is None:
            dt = datetime.datetime.now(datetime.timezone.utc)
        month = dt.month

        if month == 9:
            return ["NFL", "MLB"]
        elif month == 10:
            return ["NFL", "NBA", "MLB"]
        elif month in (11, 12, 1, 2):
            return ["NFL", "NBA"]
        elif month in (3, 4, 5, 6):
            return ["NBA", "MLB"]
        else:  # July, August (Summer lull: MLB only; NFL preseason excluded)
            return ["MLB"]

    @classmethod
    def get_primary_series_for_league(cls, league: str) -> str:
        """Return the primary game lines series ticker for a given league, or empty string if unsupported."""
        mapping = {
            "NFL": "KXNFLGAME",
            "NBA": "KXNBAGAME",
            "MLB": "KXMLBGAME",
        }
        return mapping.get(league.upper(), "")

    @classmethod
    def get_game_lines_for_league(cls, league: str) -> List[str]:
        """Return game lines series tickers for a given league."""
        mapping = {
            "NFL": list(cls.NFL_GAME_LINES),
            "NBA": list(cls.NBA_GAME_LINES),
            "MLB": list(cls.MLB_GAME_LINES),
        }
        return mapping.get(league.upper(), [])

    @classmethod
    def get_props_for_league(cls, league: str) -> List[str]:
        """Return player props series tickers for a given league."""
        mapping = {
            "NFL": list(cls.NFL_PROPS),
            "NBA": list(cls.NBA_PROPS),
            "MLB": list(cls.MLB_PROPS),
        }
        return mapping.get(league.upper(), [])

    @classmethod
    def get_series_for_league(cls, league: str) -> List[str]:
        """Return prioritized list of series tickers for a specific league, or empty list if unsupported."""
        mapping = {
            "NFL": list(cls.NFL_SERIES),
            "NBA": list(cls.NBA_SERIES),
            "MLB": list(cls.MLB_SERIES),
        }
        return mapping.get(league.upper(), [])

    @classmethod
    def get_in_season_series(cls, dt: Optional[datetime.datetime] = None) -> List[str]:
        """
        Return prioritized list of series tickers to query across all active in-season leagues.
        """
        leagues = cls.get_in_season_leagues(dt)
        series_list: List[str] = []
        for league in leagues:
            series_list.extend(cls.get_series_for_league(league))
        return series_list


# ============================================================================
# Market Ingestion & REST API Client Logic
# ============================================================================

def fetch_eligible_markets(limit: int = 1000, series_ticker: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Fetch active and tradeable markets from the Kalshi REST API.
    Queries /events with with_nested_markets=true to discover real underlying event markets
    without getting exhausted by synthetic multivariate shards (KXMVE), falling back to /markets if needed.
    Supports optional series_ticker filter for high-turnover series (e.g. KXNFLGAME).
    Excludes inactive markets, expired contracts, and internal composite / shard combo markets.
    """
    try:
        markets = []

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

                resp = requests.get(
                    f"{BASE_URL}/trade-api/v2/events",
                    params=params,
                    verify=certifi.where(),
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
            resp = requests.get(
                f"{BASE_URL}/trade-api/v2/markets",
                params=fallback_params,
                verify=certifi.where(),
                timeout=10,
            )
            resp.raise_for_status()
            markets = resp.json().get("markets", [])

        now_utc = datetime.datetime.now(datetime.timezone.utc)
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
                try:
                    close_dt = datetime.datetime.fromisoformat(str(close_time_str).replace("Z", "+00:00"))
                    if close_dt <= now_utc:
                        continue
                except Exception:
                    pass
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


def _text_for_market(m: Dict[str, Any]) -> str:
    """Combine ticker, title, and subtitle into a single uppercase searchable string."""
    return f"{m.get('ticker', '')} {m.get('title', '')} {m.get('subtitle', '')}".upper()


def _parse_float(val: Any) -> float:
    """Safely parse string or numeric value to float, defaulting to 0.0 on error or None."""
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


# ============================================================================
# Liquidity Scoring & Pre-Flight Orderbook Probing
# ============================================================================

def check_orderbook_has_quotes(ticker: str) -> bool:
    """
    Check if a market's live orderbook currently has active two-sided quotes (bids and asks).
    Makes a lightweight REST check to avoid selecting dormant contracts.
    """
    try:
        resp = requests.get(
            f"{BASE_URL}/trade-api/v2/markets/{ticker}/orderbook",
            verify=certifi.where(),
            timeout=3,
        )
        if resp.status_code == 200:
            data = resp.json()
            ob = data.get("orderbook_fp") or data.get("orderbook") or {}
            yes_bids = ob.get("yes_dollars") or ob.get("yes") or []
            no_bids = ob.get("no_dollars") or ob.get("no") or []
            return bool(len(yes_bids) > 0 and len(no_bids) > 0)
    except Exception as e:
        logger.debug(f"Pre-flight orderbook check for {ticker} failed: {e}")
    return False


def _liquidity_key(m: Dict[str, Any]) -> float:
    """
    Score market liquidity using Kalshi v2 floating-point schema and horizon weighting.
    Supports volume_fp, open_interest_fp, yes_bid_dollars, yes_ask_dollars alongside legacy keys.
    Heavily discounts distant multi-year props in favor of near-term weekly game lines.
    """
    vol = _parse_float(m.get("volume_fp")) or _parse_float(m.get("volume"))
    oi = _parse_float(m.get("open_interest_fp")) or _parse_float(m.get("open_interest"))

    bid = _parse_float(m.get("yes_bid_dollars") if m.get("yes_bid_dollars") is not None else m.get("yes_bid"))
    ask = _parse_float(m.get("yes_ask_dollars") if m.get("yes_ask_dollars") is not None else m.get("yes_ask"))
    has_quotes = 1.0 if (bid > 0 and ask > 0 and ask > bid) else 0.0

    # Expiration Horizon Multiplier
    # Prioritize near-term weekly contracts (<7 to 14 days) over distant future props (e.g. 2028-2030)
    horizon_multiplier = 1.0
    close_time_str = m.get("close_time") or m.get("expiration_time")
    if close_time_str:
        try:
            close_dt = datetime.datetime.fromisoformat(str(close_time_str).replace("Z", "+00:00"))
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            days_to_close = (close_dt - now_utc).total_seconds() / 86400.0
            if days_to_close <= 7:
                horizon_multiplier = 3.0
            elif days_to_close <= 14:
                horizon_multiplier = 2.0
            elif days_to_close <= 30:
                horizon_multiplier = 1.5
            elif days_to_close <= 90:
                horizon_multiplier = 1.0
            elif days_to_close <= 365:
                horizon_multiplier = 0.5
            else:
                horizon_multiplier = 0.05
        except Exception:
            pass

    # In-season sports bonus: prioritize high-velocity weekly game lines and player props
    series_bonus = 0.0
    ticker = str(m.get("ticker", "")).upper()
    if any(ticker.startswith(s) for s in SportsSeasonRouter.ALL_IN_SEASON_PREFIXES):
        series_bonus = 500_000.0

    base_score = (has_quotes * 1_000_000.0) + series_bonus + vol + (oi * 0.5)
    return base_score * horizon_multiplier


DEFAULT_MAX_TOTAL_PROBES: int = 10
DEFAULT_MAX_PROBES_PER_SERIES: int = 2


def _select_best_market(
    candidates: List[Dict[str, Any]],
    preflight_check: bool = True,
    max_probes: int = 10,
    budget_tracker: Optional[Dict[str, int]] = None,
) -> Optional[str]:
    """
    Prioritize markets that have existing trading volume, open interest, or two-sided quotes.
    Sorts by liquidity and expiration horizon, screening top candidates for live two-sided orderbooks.
    Respects max_probes and an optional discovery-wide budget_tracker to avoid REST rate limits.
    """
    if not candidates:
        return None

    active_pool = [
        m for m in candidates
        if (_parse_float(m.get("volume_fp")) > 0 or _parse_float(m.get("volume")) > 0 or
            _parse_float(m.get("open_interest_fp")) > 0 or _parse_float(m.get("open_interest")) > 0 or
            m.get("yes_bid") is not None or m.get("yes_bid_dollars") is not None)
    ]
    pool = active_pool if active_pool else candidates
    pool.sort(key=_liquidity_key, reverse=True)

    # Pre-flight orderbook check on top candidates
    if preflight_check:
        if budget_tracker is not None and budget_tracker.get("remaining", 0) <= 0:
            logger.info("Pre-flight probe quota exhausted; skipping further probes.")
            return None

        probe_limit = min(max_probes, len(pool))
        if budget_tracker is not None:
            probe_limit = min(probe_limit, budget_tracker["remaining"])

        top_candidates = pool[:probe_limit]
        for candidate in top_candidates:
            cand_ticker = candidate.get("ticker")
            if not cand_ticker:
                continue

            if budget_tracker is not None:
                budget_tracker["remaining"] -= 1

            if check_orderbook_has_quotes(cand_ticker):
                logger.info(f"Pre-flight orderbook check confirmed two-sided quotes for: {cand_ticker}")
                return cand_ticker
        logger.info("Pre-flight orderbook check found no candidates with active two-sided quotes.")
        return None

    # Fallback to the top-ranked candidate only if pre-flight check was explicitly disabled
    selected = pool[0]
    return selected.get("ticker")


def _markets_for_series(markets: List[Dict[str, Any]], series_ticker: str) -> List[Dict[str, Any]]:
    """Filter in-memory markets list for a specific series ticker."""
    st_upper = series_ticker.upper()
    prefix = f"{st_upper}-"
    return [
        m for m in markets
        if m.get("series_ticker", "").upper() == st_upper
        or str(m.get("ticker", "")).upper().startswith(prefix)
        or str(m.get("ticker", "")).upper() == st_upper
    ]


# ============================================================================
# Market Discovery Orchestration & Seasonal Cascade
# ============================================================================

def discover_active_market(
    target_preference: str = "",
    exclude_tickers: Optional[List[str]] = None,
    preflight_check: bool = True,
    max_total_probes: int = DEFAULT_MAX_TOTAL_PROBES,
) -> Optional[str]:
    """
    Select an active tradeable market ticker based on a target preference or category.
    
    Priority:
    1. Exact match for target_preference (if active and not excluded)
    2. In-Season Sports Router (waterfall across NFL, NBA, MLB suites based on calendar month):
       - Tier 1: Game Lines across active in-season leagues (NFL -> NBA -> MLB)
       - Tier 2: Player Props across active in-season leagues (NFL -> NBA -> MLB)
       - Tier 3: General League tradeable sports markets
    3. Category/keyword match across ticker, title, and subtitle
    4. Safely return None and idle if no active sports markets with two-sided quotes are found
    """
    exclude = set(exclude_tickers or [])
    pref = target_preference.strip().upper() if target_preference else ""
    budget_tracker: Optional[Dict[str, int]] = (
        {"remaining": max_total_probes} if preflight_check else None
    )

    # 1. Fetch eligible markets ONCE and filter in memory to protect REST rate limits
    eligible = fetch_eligible_markets()
    tradeable_markets = [m for m in eligible if m.get("ticker") not in exclude]

    if not tradeable_markets:
        logger.warning("No tradeable markets available matching criteria.")
        return None

    # Check if target preference indicates sports or is default/unspecified
    is_sports_pref = pref in (
        "NFL", "FOOTBALL", "NBA", "BASKETBALL", "MLB", "BASEBALL",
        "SPORTS", "SPORT", "MAJOR SPORTS"
    ) or not pref

    # 2. Exact match by ticker (if explicitly targeted and not a category keyword)
    if pref and not is_sports_pref:
        exact = [m for m in tradeable_markets if m.get("ticker", "").upper() == pref]
        if exact:
            exact_ticker = exact[0].get("ticker")
            if preflight_check:
                if budget_tracker is not None:
                    budget_tracker["remaining"] -= 1
                if exact_ticker and check_orderbook_has_quotes(exact_ticker):
                    logger.info(f"Targeting exact matched market with active quotes: {pref}")
                    return exact_ticker
                logger.info(f"Exact matched market {pref} has no active quotes.")
            else:
                logger.info(f"Targeting exact matched market: {pref}")
                return exact_ticker

    # 3. SportsSeasonRouter Waterfall for in-season leagues and full product suites
    if is_sports_pref:
        if pref in ("NFL", "FOOTBALL"):
            target_leagues = ["NFL"]
        elif pref in ("NBA", "BASKETBALL"):
            target_leagues = ["NBA"]
        elif pref in ("MLB", "BASEBALL"):
            target_leagues = ["MLB"]
        else:
            target_leagues = SportsSeasonRouter.get_in_season_leagues()

        # Tier 1: Prioritize Game Lines across active in-season leagues
        for league in target_leagues:
            if budget_tracker is not None and budget_tracker["remaining"] <= 0:
                logger.info("Orderbook probe quota exhausted during Tier 1 Game Lines; stopping further probes.")
                return None

            game_series = SportsSeasonRouter.get_game_lines_for_league(league)
            for series_ticker in game_series:
                if budget_tracker is not None and budget_tracker["remaining"] <= 0:
                    break

                series_markets = _markets_for_series(tradeable_markets, series_ticker)
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
            if budget_tracker is not None and budget_tracker["remaining"] <= 0:
                logger.info("Orderbook probe quota exhausted during Tier 2 Player Props; stopping further probes.")
                return None

            props_series = SportsSeasonRouter.get_props_for_league(league)
            for series_ticker in props_series:
                if budget_tracker is not None and budget_tracker["remaining"] <= 0:
                    break

                series_markets = _markets_for_series(tradeable_markets, series_ticker)
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

        # Tier 3: General League tradeable sports markets (excluding series already screened in Tiers 1 and 2)
        all_configured_series = {
            s.upper()
            for l in target_leagues
            for s in SportsSeasonRouter.get_series_for_league(l)
        }
        for league in target_leagues:
            if budget_tracker is not None and budget_tracker["remaining"] <= 0:
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

    # 4. Category or keyword match (for explicit non-sports target preference)
    if pref and not is_sports_pref:
        matched = [m for m in tradeable_markets if pref in _text_for_market(m)]
        if matched:
            selected = _select_best_market(
                matched,
                preflight_check=preflight_check,
                budget_tracker=budget_tracker,
            )
            if selected:
                logger.info(f"Matched active market for '{target_preference}': {selected}")
                return selected

    logger.warning("No active in-season sports markets with two-sided quotes found. Idling until market activity resumes.")
    return None


# ============================================================================
# Market Lifecycle & Status Verification (REST & Async)
# ============================================================================

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
    preflight_check: bool = True,
    max_total_probes: int = DEFAULT_MAX_TOTAL_PROBES,
) -> Optional[str]:
    """Non-blocking async wrapper for discover_active_market."""
    return await asyncio.to_thread(
        discover_active_market,
        target_preference,
        exclude_tickers,
        preflight_check,
        max_total_probes,
    )


async def is_market_active_async(ticker: str) -> Optional[bool]:
    """Non-blocking async wrapper for is_market_active."""
    return await asyncio.to_thread(is_market_active, ticker)


async def check_market_status_async(ticker: str) -> Optional[str]:
    """Non-blocking async wrapper for check_market_status."""
    return await asyncio.to_thread(check_market_status, ticker)
