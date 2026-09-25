"""
Strategy Math Unit Tests

This suite verifies the mathematical correctness of the Avellaneda-Stoikov quoting formula, 
focusing on:
1. Reservation price calculation for neutral, long, and short inventory positions.
2. Optimal bid and ask quoting spreads based on risk aversion (gamma) and min spread parameters.
3. Out-of-bounds price clipping (1c to 99c bounds).
4. Auto-cancellation triggers when orderbooks are empty.
"""

import pytest
import math
from unittest.mock import patch, AsyncMock, MagicMock
from strategy.market_maker import AvellanedaStoikovBot

@pytest.mark.asyncio
async def test_neutral_pricing():
    """When inventory is 0, reservation price should equal the mid price."""
    bot = AvellanedaStoikovBot(ticker="MOCK_TICKER", gamma=0.5, min_spread=4, order_size=1)
    
    # Mock manager inputs
    bot.inv_manager.get_position = MagicMock(return_value=0)
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(50, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(60, 10))
    
    # Mock output execution
    bot._update_quotes = AsyncMock()
    
    await bot._tick()
    
    # Mid = 55.0, Inv = 0, Gamma = 0.5 -> ResPrice = 55.0
    # Optimal Bid = floor(55 - 2) = 53
    # Optimal Ask = ceil(55 + 2) = 57
    bot._update_quotes.assert_called_once_with(53, 57)

@pytest.mark.asyncio
async def test_long_skew_pricing():
    """When inventory is positive (long YES), reservation price and quotes should skew downward."""
    bot = AvellanedaStoikovBot(ticker="MOCK_TICKER", gamma=0.5, min_spread=4, order_size=1)
    
    bot.inv_manager.get_position = MagicMock(return_value=2) # Holds 2 YES contracts (below threshold of 5)
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(50, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(60, 10))
    
    bot._update_quotes = AsyncMock()
    
    await bot._tick()
    
    # Mid = 55.0, Inv = 2, Gamma = 0.5 -> ResPrice = 55.0 - (2 * 0.5) = 54.0
    # Optimal Bid = floor(54.0 - 2) = 52
    # Optimal Ask = ceil(54.0 + 2) = 56
    bot._update_quotes.assert_called_once_with(52, 56)

@pytest.mark.asyncio
async def test_short_skew_pricing():
    """When inventory is negative (short YES / holding NO), reservation price and quotes should skew upward."""
    bot = AvellanedaStoikovBot(ticker="MOCK_TICKER", gamma=0.5, min_spread=4, order_size=1)
    
    bot.inv_manager.get_position = MagicMock(return_value=-2) # Short 2 YES contracts (below threshold of 5)
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(50, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(60, 10))
    
    bot._update_quotes = AsyncMock()
    
    await bot._tick()
    
    # Mid = 55.0, Inv = -2, Gamma = 0.5 -> ResPrice = 55.0 - (-2 * 0.5) = 56.0
    # Optimal Bid = floor(56.0 - 2) = 54
    # Optimal Ask = ceil(56.0 + 2) = 58
    bot._update_quotes.assert_called_once_with(54, 58)

@pytest.mark.asyncio
async def test_active_hedge_long():
    """When long inventory meets/exceeds threshold, bot must halt BIDs and cross ASKs."""
    bot = AvellanedaStoikovBot(ticker="MOCK_TICKER", gamma=0.5, min_spread=4, order_size=1)
    
    bot.inv_manager.get_position = MagicMock(return_value=5) # Meets threshold
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(50, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(60, 10))
    
    bot._update_quotes = AsyncMock()
    
    await bot._tick()
    
    # Bid is None, Ask is matched to best bid (50)
    bot._update_quotes.assert_called_once_with(None, 50)

@pytest.mark.asyncio
async def test_active_hedge_short():
    """When short inventory meets/exceeds threshold, bot must halt ASKs and cross BIDs."""
    bot = AvellanedaStoikovBot(ticker="MOCK_TICKER", gamma=0.5, min_spread=4, order_size=1)
    
    bot.inv_manager.get_position = MagicMock(return_value=-5) # Meets threshold
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(50, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(60, 10))
    
    bot._update_quotes = AsyncMock()
    
    await bot._tick()
    
    # Bid matches best ask (60), Ask is None
    bot._update_quotes.assert_called_once_with(60, None)

@pytest.mark.asyncio
async def test_bounds_clipping():
    """Quotes should be clipped to Kalshi's boundaries (1c to 99c) and not cross each other."""
    bot = AvellanedaStoikovBot(ticker="MOCK_TICKER", gamma=1.0, min_spread=100, order_size=1)
    
    bot.inv_manager.get_position = MagicMock(return_value=0) # 0 position to avoid active hedge
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(2, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(3, 10))
    
    bot._update_quotes = AsyncMock()
    
    await bot._tick()
    
    # Mid = 2.5, Inv = 0 -> ResPrice = 2.5
    # Optimal Bid = floor(2.5 - 50) = -48 -> clipped to 1
    # Optimal Ask = ceil(2.5 + 50) = 53
    # Prevents crossing and verifies clipping
    bot._update_quotes.assert_called_once_with(1, 53)

@pytest.mark.asyncio
async def test_empty_orderbook_handling():
    """If the orderbook is empty, the bot should cancel all active quotes to avoid risk."""
    bot = AvellanedaStoikovBot(ticker="MOCK_TICKER", gamma=0.5, min_spread=4, order_size=1)
    
    bot.inv_manager.get_position = MagicMock(return_value=0)
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    
    # Simulate empty book
    bot.ob_manager.get_best_bid = MagicMock(return_value=None)
    bot.ob_manager.get_best_ask = MagicMock(return_value=None)
    
    bot._cancel_all_quotes = AsyncMock()
    bot._update_quotes = AsyncMock()
    
    await bot._tick()
    
    bot._cancel_all_quotes.assert_called_once()
    bot._update_quotes.assert_not_called()


@pytest.mark.asyncio
async def test_active_hedge_subcent_long():
    """When long inventory is high, matching sub-cent best bid must preserve sub-cent float price."""
    bot = AvellanedaStoikovBot(ticker="MOCK_TICKER", gamma=0.5, min_spread=4, order_size=1)
    
    bot.inv_manager.get_position = MagicMock(return_value=5)
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(32.4, 10.0))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(35.0, 10.0))
    
    bot._update_quotes = AsyncMock()
    
    await bot._tick()
    
    # Ask should match exact sub-cent best bid (32.4)
    bot._update_quotes.assert_called_once_with(None, 32.4)


@pytest.mark.asyncio
async def test_active_hedge_subcent_short():
    """When short inventory is high, matching sub-cent best ask must preserve sub-cent float price."""
    bot = AvellanedaStoikovBot(ticker="MOCK_TICKER", gamma=0.5, min_spread=4, order_size=1)
    
    bot.inv_manager.get_position = MagicMock(return_value=-5)
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(30.0, 10.0))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(32.4, 10.0))
    
    bot._update_quotes = AsyncMock()
    
    await bot._tick()
    
    # Bid should match exact sub-cent best ask (32.4)
    bot._update_quotes.assert_called_once_with(32.4, None)


def test_dynamic_order_sizing_minimum_enforcement():
    """Verify that order_dollars is strictly clamped to a minimum of $1.00 and never lower."""
    # Negative value
    bot_neg = AvellanedaStoikovBot(ticker="MOCK", order_dollars=-5.0)
    assert bot_neg.order_dollars == 1.0

    # Zero value
    bot_zero = AvellanedaStoikovBot(ticker="MOCK", order_dollars=0.0)
    assert bot_zero.order_dollars == 1.0

    # Sub-dollar value (e.g. 50 cents)
    bot_sub = AvellanedaStoikovBot(ticker="MOCK", order_dollars=0.50)
    assert bot_sub.order_dollars == 1.0

    # Exact dollar minimum
    bot_exact = AvellanedaStoikovBot(ticker="MOCK", order_dollars=1.00)
    assert bot_exact.order_dollars == 1.0

    # Scaled value for future bi-weekly increases
    bot_scaled = AvellanedaStoikovBot(ticker="MOCK", order_dollars=2.50)
    assert bot_scaled.order_dollars == 2.50


def test_dynamic_order_size_calculation_examples():
    """Verify dynamic order sizing calculation across different price levels."""
    bot = AvellanedaStoikovBot(ticker="MOCK", order_dollars=1.00)

    # 3c contract -> 34 contracts ($1.02)
    assert bot.calculate_order_size(mid_price=3.0) == 34
    assert 34 * 3.0 >= 100.0  # Deploys at least $1.00

    # 25c contract -> 4 contracts ($1.00)
    assert bot.calculate_order_size(mid_price=25.0) == 4
    assert 4 * 25.0 == 100.0

    # 50c contract -> 2 contracts ($1.00)
    assert bot.calculate_order_size(mid_price=50.0) == 2
    assert 2 * 50.0 == 100.0

    # 80c contract -> 2 contracts ($1.60)
    assert bot.calculate_order_size(mid_price=80.0) == 2
    assert 2 * 80.0 >= 100.0

    # 99c contract -> 2 contracts ($1.98)
    assert bot.calculate_order_size(mid_price=99.0) == 2
    assert 2 * 99.0 >= 100.0

    # Edge cases: None or zero price falls back to max(1, order_size)
    assert bot.calculate_order_size(mid_price=None) == 1
    assert bot.calculate_order_size(mid_price=0.0) == 1


@pytest.mark.asyncio
async def test_dynamic_order_size_execution_and_normalization():
    """Verify dynamic order sizing integrates into _tick, reservation price math, and order placement."""
    bot = AvellanedaStoikovBot(ticker="MOCK_TICKER", gamma=0.5, min_spread=4, order_dollars=1.0)

    # Book with mid-price = 3.0c (best_bid=2, best_ask=4)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(2, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(4, 10))
    bot.inv_manager.get_balance = MagicMock(return_value=10000)

    # Inventory of 34 contracts (exactly 1 dynamic lot at 3c)
    bot.inv_manager.get_position = MagicMock(return_value=34)

    # Mock order manager
    bot.om.place_order = AsyncMock(return_value="order-123")

    await bot._tick()

    # Dynamic lot size at 3c is ceil(100 / 3) = 34
    assert bot._current_quote_size == 34

    # Normalized inventory: q = 34 / 34 = 1.0 lot
    # ResPrice = 3.0 - (1.0 * 0.5) = 2.5
    # Optimal Bid = floor(2.5 - 2) = 0 -> clamped to 1
    # Optimal Ask = ceil(2.5 + 2) = 5
    assert bot.current_bid_price == 1
    assert bot.current_ask_price == 5

    # Verify orders were placed with dynamic count=34
    assert bot.om.place_order.call_count == 2
    bot.om.place_order.assert_any_call(ticker="MOCK_TICKER", side="yes", action="buy", count=34, price=1)
    bot.om.place_order.assert_any_call(ticker="MOCK_TICKER", side="yes", action="sell", count=34, price=5)


@pytest.mark.asyncio
async def test_dynamic_order_size_scaled_hedge_threshold():
    """Verify that hedge threshold scales with dynamic quote size."""
    bot = AvellanedaStoikovBot(ticker="MOCK_TICKER", gamma=0.5, min_spread=4, order_dollars=1.0)

    bot.ob_manager.get_best_bid = MagicMock(return_value=(2, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(4, 10))
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot._update_quotes = AsyncMock()

    # Mid = 3.0 -> quote_size = 34. Hedge threshold = 5 * 34 = 170
    # At 169 contracts, hedge should NOT trigger
    bot.inv_manager.get_position = MagicMock(return_value=169)
    await bot._tick()
    # Quotes should be active on both sides
    bot._update_quotes.assert_called_once()
    bid_arg, ask_arg = bot._update_quotes.call_args[0]
    assert bid_arg is not None
    assert ask_arg is not None

    bot._update_quotes.reset_mock()

    # At 170 contracts (5 full lots), hedge triggers: halts BIDs, crosses ASKs
    bot.inv_manager.get_position = MagicMock(return_value=170)
    await bot._tick()
    bot._update_quotes.assert_called_once_with(None, 2)


def test_dynamic_order_sizing_adversarial_inputs():
    """Adversarial negative inputs (NaN, inf, invalid strings) must safely resolve to $1.00."""
    # NaN float
    bot_nan = AvellanedaStoikovBot(ticker="MOCK", order_dollars=float("nan"))
    assert bot_nan.order_dollars == 1.0

    # Inf float
    bot_inf = AvellanedaStoikovBot(ticker="MOCK", order_dollars=float("inf"))
    assert bot_inf.order_dollars == 1.0

    # Negative inf
    bot_ninf = AvellanedaStoikovBot(ticker="MOCK", order_dollars=float("-inf"))
    assert bot_ninf.order_dollars == 1.0

    # Malformed string
    bot_str = AvellanedaStoikovBot(ticker="MOCK", order_dollars="invalid_value")
    assert bot_str.order_dollars == 1.0

    # Adversarial mid_prices in calculate_order_size
    assert bot_nan.calculate_order_size(float("nan")) == 1
    assert bot_nan.calculate_order_size(float("inf")) == 1
    assert bot_nan.calculate_order_size(-50.0) == 1
    assert bot_nan.calculate_order_size("50") == 1
    assert bot_nan.calculate_order_size(True) == 1  # Boolean should not be treated as int 1


def test_dynamic_order_sizing_financial_accounting_invariants():
    """
    Assert Financial Accounting Invariant:
    Total PnL = Realized PnL + Unrealized PnL strictly holds through state transitions
    (open, partial close, reversal, full close) with multi-contract dynamic order fills and fees.
    """
    from data.pnl_tracker import PnLTracker

    tracker = PnLTracker()
    ticker = "TEST-DYNAMIC-TICKER"

    # Step 1: Open 34 contracts at 3c with 10c fee (Dynamic lot corresponding to $1.02 notional)
    tracker.record_fill(ticker, action="buy", side="yes", count=34, price_cents=3.0, fee_cents=10.0)
    tracker.update_mid_price(ticker, 4.0)
    summary_1 = tracker.get_market_summary(ticker)
    realized_1 = summary_1["realized_pnl_cents"]
    unrealized_1 = summary_1["unrealized_pnl_cents"]
    total_1 = summary_1["total_pnl_cents"]
    assert round(realized_1 + unrealized_1, 4) == round(total_1, 4)

    # Step 2: Partial close of 17 contracts at 5c with 5c exit fee
    tracker.record_fill(ticker, action="sell", side="yes", count=17, price_cents=5.0, fee_cents=5.0)
    tracker.update_mid_price(ticker, 5.0)
    summary_2 = tracker.get_market_summary(ticker)
    realized_2 = summary_2["realized_pnl_cents"]
    unrealized_2 = summary_2["unrealized_pnl_cents"]
    total_2 = summary_2["total_pnl_cents"]
    assert round(realized_2 + unrealized_2, 4) == round(total_2, 4)

    # Step 3: Reversal - Sell 34 contracts at 6c with 10c fee (closes remaining 17 long, opens 17 short)
    tracker.record_fill(ticker, action="sell", side="yes", count=34, price_cents=6.0, fee_cents=10.0)
    tracker.update_mid_price(ticker, 6.0)
    summary_3 = tracker.get_market_summary(ticker)
    realized_3 = summary_3["realized_pnl_cents"]
    unrealized_3 = summary_3["unrealized_pnl_cents"]
    total_3 = summary_3["total_pnl_cents"]
    assert round(realized_3 + unrealized_3, 4) == round(total_3, 4)

    # Step 4: Full close - Buy 17 contracts at 4c with 5c fee
    tracker.record_fill(ticker, action="buy", side="yes", count=17, price_cents=4.0, fee_cents=5.0)
    tracker.update_mid_price(ticker, 4.0)
    summary_4 = tracker.get_market_summary(ticker)
    realized_4 = summary_4["realized_pnl_cents"]
    unrealized_4 = summary_4["unrealized_pnl_cents"]
    total_4 = summary_4["total_pnl_cents"]
    assert round(realized_4 + unrealized_4, 4) == round(total_4, 4)
    assert summary_4["net_inventory"] == 0
    assert summary_4["unrealized_pnl_cents"] == 0.0




