"""
Unit Test Suite: Post-Fill Adverse Selection Backoff Safeguards

Validates:
1. Post-fill adverse selection backoff pause cancels quotes and suppresses placement.
2. Normal quoting resumes once post-fill backoff window expires.
3. Setting post_fill_pause_seconds to 0.0 disables adverse selection backoff.
"""

import time
import pytest
from unittest.mock import MagicMock, AsyncMock
from strategy.market_maker import AvellanedaStoikovBot


class TestPostFillAdverseSelectionBackoff:
    """Verify post-fill adverse selection backoff pause mechanics."""

    @pytest.mark.asyncio
    async def test_post_fill_pause_cancels_quotes_and_suppresses_placement(self):
        """When a fill occurred recently within post_fill_pause_seconds, quotes are withdrawn and placement skipped."""
        ticker = "KXPAUSE-TICKER"
        bot = AvellanedaStoikovBot(
            ticker=ticker,
            gamma=0.5,
            min_spread=4,
            post_fill_pause_seconds=3.0,
        )
        bot._cancel_all_quotes = AsyncMock(return_value=True)
        bot._update_quotes = AsyncMock(return_value=True)
        bot.current_bid_id = "bid-pause-1"
        bot.ob_manager.get_best_bid = MagicMock(return_value=(48, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(52, 10))
        bot.inv_manager.get_position = MagicMock(return_value=0)

        # Fill occurred 1.0 second ago (< 3.0s pause)
        bot.inv_manager._last_fill_times[ticker] = time.time() - 1.0

        await bot._tick()

        # Quotes must be withdrawn, and no new quotes placed
        bot._cancel_all_quotes.assert_called_once()
        bot._update_quotes.assert_not_called()

    @pytest.mark.asyncio
    async def test_post_fill_pause_resumes_after_duration_expires(self):
        """When elapsed time since fill exceeds post_fill_pause_seconds, quoting proceeds normally."""
        ticker = "KXPAUSE-EXPIRED"
        bot = AvellanedaStoikovBot(
            ticker=ticker,
            gamma=0.5,
            min_spread=4,
            post_fill_pause_seconds=3.0,
        )
        bot._cancel_all_quotes = AsyncMock(return_value=True)
        bot._update_quotes = AsyncMock(return_value=True)
        bot.ob_manager.get_best_bid = MagicMock(return_value=(48, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(52, 10))
        bot.inv_manager.get_position = MagicMock(return_value=0)

        # Fill occurred 4.0 seconds ago (> 3.0s pause)
        bot.inv_manager._last_fill_times[ticker] = time.time() - 4.0

        await bot._tick()

        # Quoting proceeded normally
        bot._update_quotes.assert_called_once_with(48, 52)

    @pytest.mark.asyncio
    async def test_post_fill_pause_zero_disables_backoff(self):
        """When post_fill_pause_seconds is 0.0, quoting is never suppressed."""
        ticker = "KXPAUSE-ZERO"
        bot = AvellanedaStoikovBot(
            ticker=ticker,
            gamma=0.5,
            min_spread=4,
            post_fill_pause_seconds=0.0,
        )
        bot._update_quotes = AsyncMock(return_value=True)
        bot.ob_manager.get_best_bid = MagicMock(return_value=(48, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(52, 10))
        bot.inv_manager.get_position = MagicMock(return_value=0)

        # Fill just occurred
        bot.inv_manager._last_fill_times[ticker] = time.time()

        await bot._tick()

        # Quoting proceeded without pause
        bot._update_quotes.assert_called_once_with(48, 52)
