"""
Unit Tests for Scheduled Slate Re-Evaluation and Event-Horizon Gating

Tests production slate-aware market selection safeguards:
- Imminent active slate prioritization (<=18h vs distant multi-day games)
- Day-of-week league priority transition rotation (e.g. NFL -> NCAAF on Saturday)
- Zero-inventory gating (never rotate mid-session with open risk)
- Minimum market dwell time enforcement (prevent orderbook flapping/churn)
- Operator-targeted exact contract override protection
- Fail-closed behavior on unconfirmed market details
"""

import asyncio
import datetime
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from strategy.market_maker import AvellanedaStoikovBot


@pytest.fixture
def bot():
    """Construct an AvellanedaStoikovBot test instance with mocked managers."""
    with patch("strategy.market_maker.KalshiWebsocketClient"), \
         patch("strategy.market_maker.OrderbookManager"), \
         patch("strategy.market_maker.InventoryManager"), \
         patch("strategy.market_maker.OrderManager"), \
         patch("strategy.market_maker.start_metrics_server"):
        b = AvellanedaStoikovBot(
            ticker="KXNFLGAME-26OCT18CARPHI-CAR",
            gamma=1.2,
            min_spread=7,
            order_size=5,
            auto_rotate=True,
            target_preference="",
        )
        b._slate_check_interval = 0.0  # Allow immediate check
        b._min_market_dwell_time = 0.0  # Bypass dwell for test unless explicitly overridden
        b.om.reconcile_resting_orders = AsyncMock(return_value=[])
        b.om.record_pnl_snapshot_async = AsyncMock()
        b.om.active_orders = {}
        b.current_bid_id = None
        b.current_ask_id = None
        b.ob_manager.unsubscribe = AsyncMock()
        b.ob_manager.subscribe = AsyncMock()
        b.inv_manager.get_position = MagicMock(return_value=0)
        b.inv_manager.get_last_fill_time = MagicMock(return_value=0.0)
        b.inv_manager.get_realized_pnl = MagicMock(return_value=0.0)
        b.inv_manager.get_unrealized_pnl = MagicMock(return_value=0.0)
        b.inv_manager.pnl_tracker = MagicMock()
        b.inv_manager.get_pnl_summary = MagicMock(return_value={
            "realized_pnl_cents": 0.0,
            "unrealized_pnl_cents": 0.0,
            "total_fees_cents": 0.0,
            "rotation_session_id": "test-session",
        })
        b._cancel_all_quotes = AsyncMock(return_value=True)
        return b


@pytest.mark.asyncio
async def test_slate_rotation_rotates_when_current_market_distant_and_imminent_available(bot):
    """Verify bot rotates from a distant game (40h out) to an imminent game (4h out)."""
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    current_dt = (now_utc + datetime.timedelta(hours=40)).isoformat()
    repl_dt = (now_utc + datetime.timedelta(hours=4)).isoformat()

    current_market_info = {
        "ticker": "KXNFLGAME-26OCT18CARPHI-CAR",
        "close_time": current_dt,
        "status": "active",
    }
    repl_market_info = {
        "ticker": "KXNCAAFGAME-26OCT17NDBYU-BYU",
        "close_time": repl_dt,
        "status": "active",
    }

    async def mock_get_details(ticker):
        if ticker == "KXNFLGAME-26OCT18CARPHI-CAR":
            return current_market_info
        elif ticker == "KXNCAAFGAME-26OCT17NDBYU-BYU":
            return repl_market_info
        return None

    with patch("strategy.market_maker.get_market_details_async", side_effect=mock_get_details), \
         patch("strategy.market_maker.discover_active_market_async", new_callable=AsyncMock) as mock_disc, \
         patch("strategy.market_maker.send_alert", new_callable=AsyncMock):

        mock_disc.return_value = "KXNCAAFGAME-26OCT17NDBYU-BYU"

        rotated = await bot._maybe_recheck_slate(now=now_utc.timestamp())

        assert rotated is True
        assert bot.ticker == "KXNCAAFGAME-26OCT17NDBYU-BYU"
        mock_disc.assert_awaited_once()


@pytest.mark.asyncio
async def test_slate_rotation_does_not_rotate_when_current_already_imminent(bot):
    """Verify bot preserves queue priority and stays on current market if already within imminent window."""
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    imminent_dt = (now_utc + datetime.timedelta(hours=6)).isoformat()

    bot.ticker = "KXNCAAFGAME-26OCT17NDBYU-BYU"
    current_market_info = {
        "ticker": "KXNCAAFGAME-26OCT17NDBYU-BYU",
        "close_time": imminent_dt,
        "status": "active",
    }

    with patch("strategy.market_maker.get_market_details_async", AsyncMock(return_value=current_market_info)), \
         patch("strategy.market_maker.SportsSeasonRouter.get_in_season_leagues", return_value=["NCAAF", "NFL"]), \
         patch("strategy.market_maker.discover_active_market_async", new_callable=AsyncMock) as mock_disc:

        rotated = await bot._maybe_recheck_slate(now=now_utc.timestamp())

        assert rotated is False
        assert bot.ticker == "KXNCAAFGAME-26OCT17NDBYU-BYU"
        mock_disc.assert_not_called()


@pytest.mark.asyncio
async def test_slate_rotation_gated_by_zero_inventory(bot):
    """Verify slate re-evaluation is strictly skipped in step() when inventory is non-zero."""
    bot.inv_manager.get_position.return_value = 5  # Open position of 5 contracts
    bot._last_slate_check = 0.0
    bot._slate_check_interval = 10.0
    now = 100.0

    bot._maybe_recheck_slate = AsyncMock(return_value=True)

    with patch("strategy.market_maker.is_market_active_async", new_callable=AsyncMock, return_value=True), \
         patch("strategy.market_maker.time.time", return_value=now), \
         patch.object(bot.ob_manager, "get_best_bid", return_value=(50.0, 100)), \
         patch.object(bot.ob_manager, "get_best_ask", return_value=(55.0, 100)), \
         patch.object(bot, "_update_quotes", new_callable=AsyncMock):

        await bot._tick()

        # Gated by inventory == 0; _maybe_recheck_slate must NOT be called
        bot._maybe_recheck_slate.assert_not_called()


@pytest.mark.asyncio
async def test_slate_rotation_gated_by_dwell_time(bot):
    """Verify slate re-evaluation is skipped when minimum dwell time has not elapsed."""
    bot.inv_manager.get_position.return_value = 0
    bot._last_slate_check = 0.0
    bot._slate_check_interval = 10.0
    bot._min_market_dwell_time = 1800.0  # 30 minutes
    bot._market_attached_time = 90.0
    now = 100.0  # Only 10s elapsed since attach

    bot._maybe_recheck_slate = AsyncMock(return_value=True)

    with patch("strategy.market_maker.is_market_active_async", new_callable=AsyncMock, return_value=True), \
         patch("strategy.market_maker.time.time", return_value=now), \
         patch.object(bot.ob_manager, "get_best_bid", return_value=(50.0, 100)), \
         patch.object(bot.ob_manager, "get_best_ask", return_value=(55.0, 100)), \
         patch.object(bot, "_update_quotes", new_callable=AsyncMock):

        await bot._tick()

        # Dwell time not elapsed (10s < 1800s); _maybe_recheck_slate must NOT be called
        bot._maybe_recheck_slate.assert_not_called()


@pytest.mark.asyncio
async def test_slate_rotation_operator_pinned_ticker_bypasses_rotation(bot):
    """Verify operator-pinned exact contract is never rotated away by slate re-evaluation."""
    bot.target_preference = "KXNFLGAME-26OCT18CARPHI-CAR"
    bot.ticker = "KXNFLGAME-26OCT18CARPHI-CAR"

    with patch("strategy.market_maker.discover_active_market_async", new_callable=AsyncMock) as mock_disc:
        rotated = await bot._maybe_recheck_slate(now=100.0)

        assert rotated is False
        mock_disc.assert_not_called()


@pytest.mark.asyncio
async def test_slate_rotation_fails_closed_on_unconfirmed_details(bot):
    """Verify slate re-evaluation fails closed (returns False) when market details lookup fails."""
    with patch("strategy.market_maker.get_market_details_async", AsyncMock(return_value=None)), \
         patch("strategy.market_maker.discover_active_market_async", new_callable=AsyncMock) as mock_disc:

        rotated = await bot._maybe_recheck_slate(now=100.0)

        assert rotated is False
        mock_disc.assert_not_called()
