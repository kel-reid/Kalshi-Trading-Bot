"""
Unit Tests for Weekend Recommendations:
1. Expiration Blackout Safeguard (Late-game freeze / auto-rotate / unwind-only)
2. Danger-Zone Blackout Safeguard (Suppress buy <= 20c, suppress sell >= 80c)
3. Bidirectional Shard Sweeping (Reverse sweep surplus from Shard 3 back to Shard 0)
4. Quote Replacement Tolerance (Suppress micro-cent churn on unchanged sizes)
"""

import datetime
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from strategy.market_maker import AvellanedaStoikovBot


# ---------------------------------------------------------------------------
# 1. EXPIRATION BLACKOUT SAFEGUARD
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_expiration_blackout_flat_inventory_rotates():
    """
    When time to close <= expiration_blackout_seconds and inventory == 0,
    bot cancels quotes and triggers market rotation if auto_rotate=True.
    """
    bot = AvellanedaStoikovBot(
        ticker="KXTEST-EXPIRING",
        gamma=0.5,
        min_spread=4,
        order_size=1,
        auto_rotate=True,
        blackout_enabled=True,
        expiration_blackout_seconds=1800,
    )

    now = time.time()
    bot._last_market_status_check = now  # Suppress periodic metadata overwrite
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    # Market expires in 10 minutes (600s <= 1800s)
    bot._market_close_time = now_utc + datetime.timedelta(minutes=10)

    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.inv_manager.get_position = MagicMock(return_value=0)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(49, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(51, 10))

    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.rotate_market = AsyncMock(return_value=True)
    bot._update_quotes = AsyncMock()

    with patch("strategy.market_maker.discover_active_market_async", new=AsyncMock(return_value="KXTEST-NEW")):
        await bot._tick()

    bot._cancel_all_quotes.assert_called_once()
    bot.rotate_market.assert_called_once_with("KXTEST-NEW")
    bot._update_quotes.assert_not_called()


@pytest.mark.asyncio
async def test_expiration_blackout_flat_inventory_no_autorotate_freezes():
    """
    When time to close <= expiration_blackout_seconds, inventory == 0, and auto_rotate=False,
    bot cancels quotes and halts further quoting without rotating.
    """
    bot = AvellanedaStoikovBot(
        ticker="KXTEST-EXPIRING",
        gamma=0.5,
        min_spread=4,
        order_size=1,
        auto_rotate=False,
        blackout_enabled=True,
        expiration_blackout_seconds=1800,
    )

    now = time.time()
    bot._last_market_status_check = now
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    bot._market_close_time = now_utc + datetime.timedelta(minutes=10)

    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.inv_manager.get_position = MagicMock(return_value=0)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(49, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(51, 10))

    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.rotate_market = AsyncMock()
    bot._update_quotes = AsyncMock()

    await bot._tick()

    assert bot._in_expiration_blackout is True
    assert bot._market_inactive is True
    bot._cancel_all_quotes.assert_called_once()
    bot.rotate_market.assert_not_called()
    bot._update_quotes.assert_not_called()


@pytest.mark.asyncio
async def test_expiration_blackout_long_inventory_unwinds_only():
    """
    When in expiration blackout with long inventory (>0),
    bot suppresses BUY YES quotes (optimal_bid=None) and only quotes SELL YES (optimal_ask) to unwind.
    """
    bot = AvellanedaStoikovBot(
        ticker="KXTEST-EXPIRING",
        gamma=0.5,
        min_spread=4,
        order_size=1,
        auto_rotate=True,
        blackout_enabled=True,
        expiration_blackout_seconds=1800,
    )

    now = time.time()
    bot._last_market_status_check = now
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    bot._market_close_time = now_utc + datetime.timedelta(minutes=15)

    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.inv_manager.get_position = MagicMock(return_value=5)  # Long 5
    bot.ob_manager.get_best_bid = MagicMock(return_value=(49, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(51, 10))

    bot._update_quotes = AsyncMock()

    await bot._tick()

    assert bot._in_expiration_blackout is True
    bot._update_quotes.assert_called_once()
    call_args = bot._update_quotes.call_args[0]
    optimal_bid, optimal_ask = call_args[0], call_args[1]

    # Bid must be suppressed (None); Ask must be present to liquidate
    assert optimal_bid is None
    assert optimal_ask is not None
    assert optimal_ask > 0


@pytest.mark.asyncio
async def test_expiration_blackout_short_inventory_unwinds_only():
    """
    When in expiration blackout with short inventory (<0),
    bot suppresses SELL YES quotes (optimal_ask=None) and only quotes BUY YES (optimal_bid) to cover.
    """
    bot = AvellanedaStoikovBot(
        ticker="KXTEST-EXPIRING",
        gamma=0.5,
        min_spread=4,
        order_size=1,
        auto_rotate=True,
        blackout_enabled=True,
        expiration_blackout_seconds=1800,
    )

    now = time.time()
    bot._last_market_status_check = now
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    bot._market_close_time = now_utc + datetime.timedelta(minutes=15)

    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.inv_manager.get_position = MagicMock(return_value=-5)  # Short 5
    bot.ob_manager.get_best_bid = MagicMock(return_value=(49, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(51, 10))

    bot._update_quotes = AsyncMock()

    await bot._tick()

    assert bot._in_expiration_blackout is True
    bot._update_quotes.assert_called_once()
    call_args = bot._update_quotes.call_args[0]
    optimal_bid, optimal_ask = call_args[0], call_args[1]

    # Ask must be suppressed (None); Bid must be present to cover
    assert optimal_ask is None
    assert optimal_bid is not None
    assert optimal_bid > 0


# ---------------------------------------------------------------------------
# 2. DANGER-ZONE BLACKOUT SAFEGUARD
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_danger_zone_blackout_suppresses_bids_at_or_below_low_threshold():
    """
    When mid_price <= blackout_danger_price_low (e.g. 18c <= 20c),
    bot suppresses BUY YES quotes (optimal_bid=None) to prevent catching binary wipeout knives.
    """
    bot = AvellanedaStoikovBot(
        ticker="KXTEST-LOWPRICE",
        gamma=0.5,
        min_spread=2,
        order_size=1,
        blackout_enabled=True,
        blackout_danger_price_low=20.0,
        blackout_danger_price_high=80.0,
    )

    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.inv_manager.get_position = MagicMock(return_value=2)  # Long 2
    # Mid = (17 + 19) / 2 = 18.0c <= 20.0c
    bot.ob_manager.get_best_bid = MagicMock(return_value=(17, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(19, 10))

    bot._update_quotes = AsyncMock()

    await bot._tick()

    bot._update_quotes.assert_called_once()
    call_args = bot._update_quotes.call_args[0]
    optimal_bid, optimal_ask = call_args[0], call_args[1]

    assert optimal_bid is None
    assert optimal_ask is not None


@pytest.mark.asyncio
async def test_danger_zone_blackout_suppresses_asks_at_or_above_high_threshold():
    """
    When mid_price >= blackout_danger_price_high (e.g. 85c >= 80c),
    bot suppresses SELL YES quotes (optimal_ask=None) to prevent shorting into guaranteed winners.
    """
    bot = AvellanedaStoikovBot(
        ticker="KXTEST-HIGHPRICE",
        gamma=0.5,
        min_spread=2,
        order_size=1,
        blackout_enabled=True,
        blackout_danger_price_low=20.0,
        blackout_danger_price_high=80.0,
    )

    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.inv_manager.get_position = MagicMock(return_value=-2)  # Short 2
    # Mid = (84 + 86) / 2 = 85.0c >= 80.0c
    bot.ob_manager.get_best_bid = MagicMock(return_value=(84, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(86, 10))

    bot._update_quotes = AsyncMock()

    await bot._tick()

    bot._update_quotes.assert_called_once()
    call_args = bot._update_quotes.call_args[0]
    optimal_bid, optimal_ask = call_args[0], call_args[1]

    assert optimal_ask is None
    assert optimal_bid is not None


@pytest.mark.asyncio
async def test_danger_zone_normal_price_allows_two_sided_quotes():
    """
    When mid_price is within safe boundaries (20c < mid < 80c),
    both bid and ask quotes are active.
    """
    bot = AvellanedaStoikovBot(
        ticker="KXTEST-SAFEPRICE",
        gamma=0.5,
        min_spread=4,
        order_size=1,
        blackout_enabled=True,
        blackout_danger_price_low=20.0,
        blackout_danger_price_high=80.0,
    )

    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.inv_manager.get_position = MagicMock(return_value=0)
    # Mid = (49 + 51) / 2 = 50.0c
    bot.ob_manager.get_best_bid = MagicMock(return_value=(49, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(51, 10))

    bot._update_quotes = AsyncMock()

    await bot._tick()

    bot._update_quotes.assert_called_once()
    call_args = bot._update_quotes.call_args[0]
    optimal_bid, optimal_ask = call_args[0], call_args[1]

    assert optimal_bid is not None
    assert optimal_ask is not None


# ---------------------------------------------------------------------------
# 3. BIDIRECTIONAL SHARD SWEEPING
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sweep_shard_to_shard0_transfers_surplus():
    """
    sweep_shard_to_shard0 sweeps cash in excess of retain_dollars back to Shard 0.
    """
    bot = AvellanedaStoikovBot(
        ticker="KXTEST-SWEEP",
        auto_sweep_reverse_enabled=True,
        shard_retain_dollars=10.0,
    )

    bot.om.get_portfolio_balances = MagicMock(return_value={
        "shard_balances": {
            0: 5.0,
            3: 35.50,
        }
    })
    bot.om.transfer_intra_exchange = MagicMock(return_value=True)

    # Shard 3 has $35.50, retain $10.00 -> transfer $25.50 back to Shard 0
    success = await bot.sweep_shard_to_shard0(source_shard=3, retain_dollars=10.0)

    assert success is True
    bot.om.transfer_intra_exchange.assert_called_once_with(
        source_shard=3,
        destination_shard=0,
        amount_dollars=25.50,
    )


@pytest.mark.asyncio
async def test_sweep_shard_to_shard0_no_surplus_skips_transfer():
    """
    sweep_shard_to_shard0 skips transfer if balance is already <= retain_dollars.
    """
    bot = AvellanedaStoikovBot(
        ticker="KXTEST-SWEEP",
        auto_sweep_reverse_enabled=True,
        shard_retain_dollars=10.0,
    )

    bot.om.get_portfolio_balances = MagicMock(return_value={
        "shard_balances": {
            0: 25.0,
            3: 8.50,
        }
    })
    bot.om.transfer_intra_exchange = MagicMock()

    success = await bot.sweep_shard_to_shard0(source_shard=3, retain_dollars=10.0)

    assert success is True
    bot.om.transfer_intra_exchange.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_shard_collateral_triggers_reverse_sweep_on_excess():
    """
    ensure_shard_collateral triggers reverse sweep if active trading shard (Shard 3)
    has surplus capital exceeding min_required + shard_retain_dollars.
    """
    bot = AvellanedaStoikovBot(
        ticker="KXTEST-SWEEP",
        auto_sweep_shard0_enabled=False,
        auto_sweep_reverse_enabled=True,
        shard_retain_dollars=10.0,
    )
    bot.exchange_index = 3

    # min_required = $15.00, retain = $10.00 -> threshold = $25.00
    # Current Shard 3 balance = $40.00 (surplus above threshold is $15.00)
    bot.om.get_portfolio_balances = MagicMock(return_value={
        "shard_balances": {
            0: 2.00,
            3: 40.00,
        }
    })
    bot.om.transfer_intra_exchange = MagicMock(return_value=True)

    result = await bot.ensure_shard_collateral(min_required_dollars=15.0)

    assert result is True
    # Should transfer surplus back: 40.00 - (15.00 + 10.00) = 15.00 to Shard 0
    bot.om.transfer_intra_exchange.assert_called_once_with(
        source_shard=3,
        destination_shard=0,
        amount_dollars=15.00,
    )


@pytest.mark.asyncio
async def test_stop_sweeps_all_funds_to_shard0():
    """
    bot.stop() triggers reverse sweep with retain_dollars=0.0 to return 100% of capital to Shard 0.
    """
    bot = AvellanedaStoikovBot(
        ticker="KXTEST-SWEEP",
        auto_sweep_reverse_enabled=True,
    )
    bot.exchange_index = 3

    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.sweep_shard_to_shard0 = AsyncMock(return_value=True)
    bot.om.record_pnl_snapshot_async = AsyncMock()

    await bot.stop()

    bot.sweep_shard_to_shard0.assert_called_once_with(
        retain_dollars=0.0,
    )


# ---------------------------------------------------------------------------
# 4. QUOTE REPLACEMENT TOLERANCE
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_quote_replacement_tolerance_suppresses_churn():
    """
    When new bid and ask prices change by less than quote_replacement_tolerance_cents
    and order sizes remain unchanged, _update_quotes skips cancellation and re-order.
    """
    bot = AvellanedaStoikovBot(
        ticker="KXTEST-TOLERANCE",
        order_size=5,
        quote_replacement_tolerance_cents=1,
    )

    # Active quotes: Bid 45c (size 5), Ask 55c (size 5)
    bot.current_bid_id = "bid_1"
    bot.current_bid_price = 45
    bot.current_bid_size = 5

    bot.current_ask_id = "ask_1"
    bot.current_ask_price = 55
    bot.current_ask_size = 5

    bot.om.cancel_order = AsyncMock()
    bot.om.place_order = AsyncMock()

    # Requested new quotes: Bid 45c (diff=0), Ask 55c (diff=0) -> exactly unchanged
    await bot._update_quotes(45, 55)

    bot.om.cancel_order.assert_not_called()
    bot.om.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_quote_replacement_tolerance_updates_when_diff_exceeds_tolerance():
    """
    When price shift >= quote_replacement_tolerance_cents,
    quotes are cancelled and replaced normally.
    """
    bot = AvellanedaStoikovBot(
        ticker="KXTEST-TOLERANCE",
        order_size=5,
        quote_replacement_tolerance_cents=1,
    )

    # Active quotes: Bid 45c, Ask 55c
    bot.current_bid_id = "bid_1"
    bot.current_bid_price = 45
    bot.current_bid_size = 5

    bot.current_ask_id = "ask_1"
    bot.current_ask_price = 55
    bot.current_ask_size = 5

    bot.om.cancel_order = AsyncMock(return_value=True)
    bot.om.place_order = AsyncMock(return_value="new_bid_order")

    # Requested new quotes: Bid 47c (diff=2 >= 1), Ask 55c -> Bid must update
    await bot._update_quotes(47, 55)

    bot.om.cancel_order.assert_called_once_with("bid_1")
    assert bot.om.place_order.call_count == 1


# ---------------------------------------------------------------------------
# 5. CODERABBIT REVIEW REFINEMENT TESTS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_expiration_blackout_unwind_quote_size_clamped_to_inventory():
    """
    In the expiration blackout unwind path, quote size must be clamped to abs(inventory)
    to prevent overshooting and opening a new risk position on the opposite side.
    """
    bot = AvellanedaStoikovBot(
        ticker="KXTEST-EXPIRING",
        order_size=10,  # quote_size would normally be 10
        blackout_enabled=True,
        expiration_blackout_seconds=1800,
    )

    now = time.time()
    bot._last_market_status_check = now
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    bot._market_close_time = now_utc + datetime.timedelta(minutes=15)

    # Open inventory is only 2 contracts (less than order_size 10)
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.inv_manager.get_position = MagicMock(return_value=2)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(49, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(51, 10))

    bot._update_quotes = AsyncMock()

    await bot._tick()

    assert bot._current_quote_size == 2
    bot._update_quotes.assert_called_once()
    assert bot._update_quotes.call_args[0][0] is None
    assert bot._update_quotes.call_args[0][1] is not None


@pytest.mark.asyncio
async def test_danger_zone_blackout_allows_exit_leg():
    """
    Danger-zone blackout should only suppress quotes that add risk.
    When short at 18c (<= 20c), the BID is the exit/covering leg and must NOT be suppressed.
    When long at 85c (>= 80c), the ASK is the exit/liquidating leg and must NOT be suppressed.
    """
    bot = AvellanedaStoikovBot(
        ticker="KXTEST-DANGER",
        order_size=1,
        blackout_enabled=True,
        blackout_danger_price_low=20.0,
        blackout_danger_price_high=80.0,
    )

    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot._update_quotes = AsyncMock()

    # 1. Short inventory (-2) at 18c (mid <= 20c): BID must be permitted to cover
    bot.inv_manager.get_position = MagicMock(return_value=-2)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(17, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(19, 10))
    await bot._tick()
    call_args_short = bot._update_quotes.call_args[0]
    assert call_args_short[0] is not None  # BID allowed for short

    # 2. Long inventory (+2) at 85c (mid >= 80c): ASK must be permitted to liquidate
    bot._update_quotes.reset_mock()
    bot._mid_price_history.clear()
    bot._fast_market_until = 0.0
    bot.inv_manager.get_position = MagicMock(return_value=2)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(84, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(86, 10))
    await bot._tick()
    call_args_long = bot._update_quotes.call_args[0]
    assert call_args_long[1] is not None  # ASK allowed for long


@pytest.mark.asyncio
async def test_quote_replacement_tolerance_zero_unchanged_price_skips():
    """
    When quote_replacement_tolerance_cents is 0, an unchanged price (delta == 0)
    must NOT trigger an order cancellation and replacement.
    """
    bot = AvellanedaStoikovBot(
        ticker="KXTEST-TOLERANCE",
        order_size=5,
        quote_replacement_tolerance_cents=0,
    )

    bot.current_bid_id = "bid_1"
    bot.current_bid_price = 45
    bot.current_bid_size = 5
    bot.current_ask_id = "ask_1"
    bot.current_ask_price = 55
    bot.current_ask_size = 5

    bot.om.cancel_order = AsyncMock()
    bot.om.place_order = AsyncMock()

    # Prices are identical (45, 55) -> delta == 0 -> should skip update even with tolerance=0
    await bot._update_quotes(45, 55)

    bot.om.cancel_order.assert_not_called()
    bot.om.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_rotate_market_same_shard_skips_sweep():
    """
    When rotating to a market that resides on the same shard (e.g. Shard 3 to Shard 3),
    rotate_market must skip sweep_shard_to_shard0 to prevent wasteful capital churn.
    """
    bot = AvellanedaStoikovBot(
        ticker="KXTEST-A",
        auto_sweep_reverse_enabled=True,
    )
    bot.exchange_index = 3

    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.ob_manager.unsubscribe = AsyncMock()
    bot.ob_manager.subscribe = AsyncMock()
    bot.om.record_pnl_snapshot_async = AsyncMock()
    bot.sweep_shard_to_shard0 = AsyncMock(return_value=True)
    bot.ensure_shard_collateral = AsyncMock(return_value=True)
    bot._refresh_market_metadata = AsyncMock()

    # Both old market and new market map to Shard 3
    with patch("strategy.market_maker.get_market_exchange_index", return_value=3):
        await bot.rotate_market("KXTEST-B")

    bot.sweep_shard_to_shard0.assert_not_called()
    assert bot.exchange_index == 3


@pytest.mark.asyncio
async def test_stop_skips_sweep_if_quotes_not_cancelled():
    """
    In bot.stop(), if quotes_cancelled is False, reverse sweep must NOT run
    to avoid pulling collateral away from unconfirmed resting orders.
    """
    bot = AvellanedaStoikovBot(
        ticker="KXTEST-SWEEP",
        auto_sweep_reverse_enabled=True,
    )
    bot.exchange_index = 3

    bot._cancel_all_quotes = AsyncMock(return_value=False)
    bot._escalate_to_kill_switch = AsyncMock(return_value=False)
    bot.sweep_shard_to_shard0 = AsyncMock(return_value=True)
    bot.om.record_pnl_snapshot_async = AsyncMock()

    await bot.stop()

    bot.sweep_shard_to_shard0.assert_not_called()

