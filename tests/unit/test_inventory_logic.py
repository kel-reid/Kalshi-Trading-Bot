"""
Inventory Manager Unit Tests

This suite verifies that execution fill messages are correctly processed by the InventoryManager 
class to maintain an accurate local record of:
1. USD Balance in cents.
2. Net contract inventory position (positive for long YES, negative for short YES / long NO).
"""

import pytest
from unittest.mock import MagicMock
from data.inventory_manager import InventoryManager

def test_inventory_buy_yes_fill():
    """Buying YES contracts should reduce balance and increase position."""
    mock_client = MagicMock()
    manager = InventoryManager(mock_client)
    manager.balance_cents = 10000 # $100.00
    manager.positions = {"MOCK_TICKER": 0}
    
    fill_msg = {
        "type": "fill",
        "msg": {
            "market_ticker": "MOCK_TICKER",
            "action": "buy",
            "side": "yes",
            "count": 10,
            "price": 50 # 50 cents per share
        }
    }
    
    manager._handle_fill(fill_msg["msg"])
    
    # 10 contracts * 50 cents = 500 cents cost -> New Balance: 9500 cents
    assert manager.get_balance() == 9500
    # Net position should be +10
    assert manager.get_position("MOCK_TICKER") == 10

def test_inventory_sell_yes_fill():
    """Selling YES contracts should increase balance and decrease position."""
    mock_client = MagicMock()
    manager = InventoryManager(mock_client)
    manager.balance_cents = 10000
    manager.positions = {"MOCK_TICKER": 10}
    
    fill_msg = {
        "type": "fill",
        "msg": {
            "market_ticker": "MOCK_TICKER",
            "action": "sell",
            "side": "yes",
            "count": 5,
            "price": 60
        }
    }
    
    manager._handle_fill(fill_msg["msg"])
    
    # 5 contracts * 60 cents = 300 cents revenue -> New Balance: 10300 cents
    assert manager.get_balance() == 10300
    # Net position should decrease by 5 -> New Position: +5
    assert manager.get_position("MOCK_TICKER") == 5

def test_inventory_buy_no_fill():
    """Buying NO contracts should reduce balance and decrease net YES-equivalent position."""
    mock_client = MagicMock()
    manager = InventoryManager(mock_client)
    manager.balance_cents = 10000
    manager.positions = {"MOCK_TICKER": 0}
    
    fill_msg = {
        "type": "fill",
        "msg": {
            "market_ticker": "MOCK_TICKER",
            "action": "buy",
            "side": "no",
            "count": 5,
            "price": 40
        }
    }
    
    manager._handle_fill(fill_msg["msg"])
    
    # 5 contracts * 40 cents = 200 cents cost -> New Balance: 9800 cents
    assert manager.get_balance() == 9800
    # Buying NO is equivalent to shorting YES -> New Net Position: -5
    assert manager.get_position("MOCK_TICKER") == -5

def test_inventory_sell_no_fill():
    """Selling NO contracts should increase balance and increase net YES-equivalent position."""
    mock_client = MagicMock()
    manager = InventoryManager(mock_client)
    manager.balance_cents = 10000
    manager.positions = {"MOCK_TICKER": -10}
    
    fill_msg = {
        "type": "fill",
        "msg": {
            "market_ticker": "MOCK_TICKER",
            "action": "sell",
            "side": "no",
            "count": 5,
            "price": 40
        }
    }
    
    manager._handle_fill(fill_msg["msg"])
    
    # 5 contracts * 40 cents = 200 cents revenue -> New Balance: 10200 cents
    assert manager.get_balance() == 10200
    # Selling NO decreases your short position -> New Net Position: -5
    assert manager.get_position("MOCK_TICKER") == -5


def test_inventory_v2_live_websocket_fill_payload():
    """
    Empirical test: Kalshi v2 WebSocket emits count_fp string, yes_price_dollars,
    and fee_cost in dollars. Verify inventory, balance, and PnLTracker state correctly hydrate.
    """
    mock_client = MagicMock()
    manager = InventoryManager(mock_client)
    manager.balance_cents = 10000.0  # $100.00
    manager.positions = {"KXNFLGAME-26SEP27KCMIA-KC": 0}

    # Exact payload observed on production DigitalOcean droplet
    v2_fill_msg = {
        "trade_id": "07237475-ddf1-9fd2-6041-2de4f54682af",
        "order_id": "01a0e417-e3b0-7d54-900c-cf5a74ad0c2f",
        "market_ticker": "KXNFLGAME-26SEP27KCMIA-KC",
        "action": "buy",
        "side": "yes",
        "count_fp": "2.00",
        "yes_price_dollars": "0.8700",
        "fee_cost": "0.004000",
    }

    manager._handle_fill(v2_fill_msg)

    # 2 contracts * 87.0 cents + 0.4 cents fee = 174.4 cents cost
    # Balance: 10000.0 - 174.4 = 9825.6 cents
    assert manager.get_balance() == 9825.6
    assert manager.get_position("KXNFLGAME-26SEP27KCMIA-KC") == 2
    assert manager._fill_count == 1

    summary = manager.pnl_tracker.get_market_summary("KXNFLGAME-26SEP27KCMIA-KC")
    assert summary["net_inventory"] == 2
    assert summary["total_fees_cents"] == 0.4
    assert summary["volume_contracts"] == 2
    # Unrealized PnL reflects allocated entry fee of 0.4c before any mid-price tick
    assert summary["unrealized_pnl_cents"] == -0.4
    assert summary["total_pnl_cents"] == -0.4


def test_inventory_v2_partial_fractional_fill_round_trip():
    """
    Verify fractional count_fp execution (e.g. 0.69 contracts) matches FIFO lots cleanly
    without floating-point epsilon drift, and strictly satisfies Total PnL = Realized + Unrealized.
    """
    mock_client = MagicMock()
    manager = InventoryManager(mock_client)
    manager.balance_cents = 10000.0
    ticker = "KXNFLGAME-26SEP27KCMIA-KC"
    manager.positions = {ticker: 0}

    # 1. Partial fill: Buy 0.69 YES @ $0.87 with $0.001380 fee (0.138 cents)
    buy_fill = {
        "market_ticker": ticker,
        "action": "buy",
        "side": "yes",
        "count_fp": "0.69",
        "yes_price_dollars": "0.8700",
        "fee_cost": "0.001380",
    }
    manager._handle_fill(buy_fill)

    assert manager.get_position(ticker) == 0.69
    # Cost = 0.69 * 87.0 + 0.138 = 60.03 + 0.138 = 60.168 cents
    assert round(manager.get_balance(), 4) == round(10000.0 - 60.168, 4)

    # 2. Closing fill: Sell 0.69 YES @ $0.91 with $0.001380 fee (0.138 cents)
    sell_fill = {
        "market_ticker": ticker,
        "action": "sell",
        "side": "yes",
        "count_fp": "0.69",
        "yes_price_dollars": "0.9100",
        "fee_cost": "0.001380",
    }
    manager._handle_fill(sell_fill)

    # Position is fully closed to 0
    assert manager.get_position(ticker) == 0
    # Revenue = 0.69 * 91.0 - 0.138 = 62.79 - 0.138 = 62.652 cents
    # Net Balance = (10000 - 60.168) + 62.652 = 10002.484 cents
    assert round(manager.get_balance(), 4) == 10002.484

    summary = manager.pnl_tracker.get_market_summary(ticker)
    assert summary["net_inventory"] == 0
    # Gross gain: 0.69 * (91 - 87) = 2.76 cents. Total fees: 0.276 cents.
    # Net realized PnL: 2.76 - 0.276 = 2.484 cents.
    assert summary["realized_pnl_cents"] == 2.484
    assert summary["unrealized_pnl_cents"] == 0.0
    assert summary["total_pnl_cents"] == 2.484
    assert summary["winning_trades"] == 1
    assert summary["round_trips_count"] == 1
    # Financial Accounting Invariant strictly satisfied:
    assert summary["total_pnl_cents"] == round(summary["realized_pnl_cents"] + summary["unrealized_pnl_cents"], 4)


def test_inventory_v2_no_price_dollars_conversion():
    """Verify buying NO contracts with no_price_dollars sets correct YES-equivalent short position and fee."""
    mock_client = MagicMock()
    manager = InventoryManager(mock_client)
    manager.balance_cents = 10000.0
    ticker = "KXNFLGAME-26SEP27KCMIA-KC"
    manager.positions = {ticker: 0}

    no_fill = {
        "market_ticker": ticker,
        "action": "buy",
        "side": "no",
        "count_fp": "5.00",
        "no_price_dollars": "0.4000",
        "fee_cost": "0.010000",
    }
    manager._handle_fill(no_fill)

    # Buying 5 NO @ 40c = 200c + 1.0c fee = 201c cost
    assert manager.get_balance() == 9799.0
    # Net YES position: -5
    assert manager.get_position(ticker) == -5
    summary = manager.pnl_tracker.get_market_summary(ticker)
    assert summary["net_inventory"] == -5
    assert summary["total_fees_cents"] == 1.0

