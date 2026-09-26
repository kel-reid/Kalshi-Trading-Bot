"""
Market Scoring, Liquidity Ranking, and Candidate Selection

Calculates liquidity keys, expiration horizon weighting, and probes top candidates
for active two-sided orderbook quotes within a budgeted quota.
"""

import datetime
import logging
from typing import Any, Callable, Dict, List, Optional

from utils.horizon import _parse_float, _parse_iso_timestamp
from utils.sports_router import SportsSeasonRouter

logger = logging.getLogger("MarketDiscovery")

DEFAULT_MAX_TOTAL_PROBES: int = 10
DEFAULT_MAX_PROBES_PER_SERIES: int = 2
DEFAULT_MAX_TARGETED_SERIES_FALLBACKS: int = 3


def _get_orderbook_checker() -> Callable[..., bool]:
    """Retrieve the check_orderbook_has_quotes function, honoring any mock in utils.market_discovery."""
    import sys
    md = sys.modules.get("utils.market_discovery")
    if md is not None and hasattr(md, "check_orderbook_has_quotes"):
        return getattr(md, "check_orderbook_has_quotes")
    from utils.market_api import check_orderbook_has_quotes
    return check_orderbook_has_quotes


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
        close_dt = _parse_iso_timestamp(close_time_str)
        if close_dt is not None:
            import sys
            md = sys.modules.get("utils.market_discovery")
            dt_module = getattr(md, "datetime", datetime) if md else datetime
            now_utc = dt_module.datetime.now(datetime.timezone.utc)
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

    # In-season sports bonus: prioritize high-velocity weekly game lines and player props
    series_bonus = 0.0
    ticker = str(m.get("ticker", "")).upper()
    if any(ticker.startswith(s) for s in SportsSeasonRouter.ALL_IN_SEASON_PREFIXES):
        series_bonus = 500_000.0

    base_score = (has_quotes * 1_000_000.0) + series_bonus + vol + (oi * 0.5)
    return base_score * horizon_multiplier


def _select_best_market(
    candidates: List[Dict[str, Any]],
    preflight_check: bool = True,
    max_probes: int = 10,
    budget_tracker: Optional[Dict[str, int]] = None,
    min_mid_price: Optional[float] = None,
    max_mid_price: Optional[float] = None,
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

        check_orderbook_has_quotes = _get_orderbook_checker()
        top_candidates = pool[:probe_limit]
        for candidate in top_candidates:
            cand_ticker = candidate.get("ticker")
            if not cand_ticker:
                continue

            if budget_tracker is not None:
                budget_tracker["remaining"] -= 1

            try:
                has_quotes = check_orderbook_has_quotes(
                    cand_ticker,
                    min_mid_price=min_mid_price,
                    max_mid_price=max_mid_price,
                )
            except TypeError:
                has_quotes = check_orderbook_has_quotes(cand_ticker)

            if has_quotes:
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
