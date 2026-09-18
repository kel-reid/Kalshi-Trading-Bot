"""
Orderbook Manager Unit Tests

This suite verifies that L2 orderbook updates and price conversions are handled correctly by 
the OrderbookManager class:
1. Parsing full snapshots for 'yes' and 'no' shares.
2. Appending and reducing volumes via orderbook delta updates.
3. Removing price levels completely when quantities drop to 0 or below.
4. Computing implied YES ask prices from the highest resting NO bid prices.
"""

import pytest
from unittest.mock import MagicMock
from data.orderbook_manager import OrderbookManager

def test_orderbook_snapshot_parsing():
    """Verify that snapshots hydrate the books structure correctly."""
    mock_client = MagicMock()
    manager = OrderbookManager(mock_client)
    
    snapshot_msg = {
        "type": "orderbook_snapshot",
        "msg": {
            "market_ticker": "MOCK_TICKER",
            "yes": [[50, 10], [49, 15]],
            "no": [[45, 5], [44, 8]]
        }
    }
    
    # Process snapshot
    manager._handle_snapshot(snapshot_msg["msg"])
    
    # Assert book structure was loaded correctly
    assert manager.books["MOCK_TICKER"]["yes"] == {50: 10, 49: 15}
    assert manager.books["MOCK_TICKER"]["no"] == {45: 5, 44: 8}
    
    # Check best bids/asks
    assert manager.get_best_bid("MOCK_TICKER") == (50, 10)
    # Highest NO bid = 45 -> Implied YES Ask = 100 - 45 = 55
    assert manager.get_best_ask("MOCK_TICKER") == (55, 5)

def test_orderbook_delta_processing():
    """Verify that delta events add, subtract, and remove price levels correctly."""
    mock_client = MagicMock()
    manager = OrderbookManager(mock_client)
    
    # 1. Initialize with snapshot
    manager._handle_snapshot({
        "market_ticker": "MOCK_TICKER",
        "yes": [[50, 10]],
        "no": [[45, 5]]
    })
    
    # 2. Add delta to existing YES level
    manager._handle_delta({
        "market_ticker": "MOCK_TICKER",
        "side": "yes",
        "price": 50,
        "delta": 5
    })
    assert manager.books["MOCK_TICKER"]["yes"][50] == 15
    
    # 3. Add delta to new YES level
    manager._handle_delta({
        "market_ticker": "MOCK_TICKER",
        "side": "yes",
        "price": 51,
        "delta": 2
    })
    assert manager.books["MOCK_TICKER"]["yes"][51] == 2
    assert manager.get_best_bid("MOCK_TICKER") == (51, 2)
    
    # 4. Subtract delta to remove a level completely
    manager._handle_delta({
        "market_ticker": "MOCK_TICKER",
        "side": "yes",
        "price": 51,
        "delta": -2
    })
    # Level should be popped from dict
    assert 51 not in manager.books["MOCK_TICKER"]["yes"]
    assert manager.get_best_bid("MOCK_TICKER") == (50, 15)


def test_orderbook_v2_snapshot_parsing():
    """Verify that Kalshi v2 yes_dollars_fp and no_dollars_fp snapshots hydrate correctly."""
    mock_client = MagicMock()
    manager = OrderbookManager(mock_client)

    snapshot_msg = {
        "market_ticker": "KXNFLGAME-26SEP17DETBUF",
        "yes_dollars_fp": [["0.3200", "150.00"], ["0.3100", "50.00"]],
        "no_dollars_fp": [["0.6500", "80.00"]],
    }

    manager._handle_snapshot(snapshot_msg)

    assert manager.books["KXNFLGAME-26SEP17DETBUF"]["yes"] == {32: 150, 31: 50}
    assert manager.books["KXNFLGAME-26SEP17DETBUF"]["no"] == {65: 80}

    # Best bid: 32c @ 150 contracts
    assert manager.get_best_bid("KXNFLGAME-26SEP17DETBUF") == (32, 150)
    # Highest NO bid is 65c -> Implied YES Ask = 100 - 65 = 35c @ 80 contracts
    assert manager.get_best_ask("KXNFLGAME-26SEP17DETBUF") == (35, 80)


def test_orderbook_v2_delta_processing():
    """Verify that Kalshi v2 price_dollars and delta_fp delta updates modify and prune levels correctly."""
    mock_client = MagicMock()
    manager = OrderbookManager(mock_client)

    # Initial state via v2 snapshot
    manager._handle_snapshot({
        "market_ticker": "KXNFLGAME-26SEP17DETBUF",
        "yes_dollars_fp": [["0.4500", "100.00"]],
        "no_dollars_fp": [["0.5200", "60.00"]],
    })

    # Delta adding 50 contracts to YES 0.4500
    manager._handle_delta({
        "market_ticker": "KXNFLGAME-26SEP17DETBUF",
        "side": "yes",
        "price_dollars": "0.4500",
        "delta_fp": "50.00",
    })
    assert manager.books["KXNFLGAME-26SEP17DETBUF"]["yes"][45] == 150

    # Delta pruning YES 0.4500 to 0
    manager._handle_delta({
        "market_ticker": "KXNFLGAME-26SEP17DETBUF",
        "side": "yes",
        "price_dollars": "0.4500",
        "delta_fp": "-150.00",
    })
    assert 45 not in manager.books["KXNFLGAME-26SEP17DETBUF"]["yes"]
    assert manager.get_best_bid("KXNFLGAME-26SEP17DETBUF") is None


def test_normalize_price_to_cents_formats():
    """Verify that both v2 dollar strings and legacy float/int cents normalize accurately."""
    from data.orderbook_manager import _normalize_price_to_cents

    # v2 dollar representations (< 1.0)
    assert _normalize_price_to_cents("0.5000") == 50
    assert _normalize_price_to_cents(0.50) == 50
    assert _normalize_price_to_cents("0.0100") == 1
    assert _normalize_price_to_cents("0.9900") == 99

    # Legacy cent representations (>= 1.0)
    assert _normalize_price_to_cents(50) == 50
    assert _normalize_price_to_cents(50.0) == 50
    assert _normalize_price_to_cents("50.0") == 50
    assert _normalize_price_to_cents("50") == 50
    assert _normalize_price_to_cents(1) == 1
    assert _normalize_price_to_cents(99.0) == 99

    # Error handling
    assert _normalize_price_to_cents(None) is None
    assert _normalize_price_to_cents("invalid") is None

    # $1.00 boundary case
    assert _normalize_price_to_cents("1.0000", is_dollars=True) == 100.0
    assert _normalize_price_to_cents("1.0000") == 100.0


def test_subcent_price_levels_coexist_without_collision():
    """Verify that distinct sub-cent levels (e.g. 0.3210 and 0.3240) coexist without overwriting."""
    mock_client = MagicMock()
    manager = OrderbookManager(mock_client)

    manager._handle_snapshot({
        "market_ticker": "KXTEST-SUBCENT",
        "yes_dollars_fp": [["0.3210", "100.00"], ["0.3240", "150.00"]],
        "no_dollars_fp": [["0.6760", "200.00"]],
    })

    # Both sub-cent levels must coexist distinctly in the book
    assert 32.1 in manager.books["KXTEST-SUBCENT"]["yes"]
    assert 32.4 in manager.books["KXTEST-SUBCENT"]["yes"]
    assert manager.books["KXTEST-SUBCENT"]["yes"][32.1] == 100.0
    assert manager.books["KXTEST-SUBCENT"]["yes"][32.4] == 150.0

    # Best bid is highest price: 32.4c @ 150.0
    assert manager.get_best_bid("KXTEST-SUBCENT") == (32.4, 150.0)
    # Best ask: 100.0 - 67.6 = 32.4c @ 200.0
    assert manager.get_best_ask("KXTEST-SUBCENT") == (32.4, 200.0)

    # Delta modifying only 0.3240
    manager._handle_delta({
        "market_ticker": "KXTEST-SUBCENT",
        "side": "yes",
        "price_dollars": "0.3240",
        "delta_fp": "-50.00",
    })
    assert manager.books["KXTEST-SUBCENT"]["yes"][32.4] == 100.0
    assert manager.books["KXTEST-SUBCENT"]["yes"][32.1] == 100.0


def test_fractional_quantities_and_delta_reversibility():
    """Verify that fractional contract sizes (e.g. 0.50) are retained and reversible."""
    mock_client = MagicMock()
    manager = OrderbookManager(mock_client)

    # Fractional resting liquidity
    manager._handle_snapshot({
        "market_ticker": "KXTEST-FRACT",
        "yes_dollars_fp": [["0.5000", "0.50"]],
        "no_dollars_fp": [],
    })

    # 0.50 contracts must not be rounded to 0
    assert manager.books["KXTEST-FRACT"]["yes"][50.0] == 0.50
    assert manager.get_best_bid("KXTEST-FRACT") == (50.0, 0.50)

    # Adding +0.50 -> 1.00
    manager._handle_delta({
        "market_ticker": "KXTEST-FRACT",
        "side": "yes",
        "price_dollars": "0.5000",
        "delta_fp": "0.50",
    })
    assert manager.books["KXTEST-FRACT"]["yes"][50.0] == 1.00

    # Subtracting -1.00 -> completely pruned to 0
    manager._handle_delta({
        "market_ticker": "KXTEST-FRACT",
        "side": "yes",
        "price_dollars": "0.5000",
        "delta_fp": "-1.00",
    })
    assert 50.0 not in manager.books["KXTEST-FRACT"]["yes"]
    assert manager.get_best_bid("KXTEST-FRACT") is None


