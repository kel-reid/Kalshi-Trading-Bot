"""
Unit Tests for Daily Report Generation
Validates:
1. compute_fifo_round_trips properly calculates FIFO round trips for YES and NO contracts.
2. fetch_daily_metrics includes both 'verified_executed' and 'cancelled_or_filled_404' orders.
"""

import datetime
from unittest.mock import MagicMock
import pytest

from scripts.generate_daily_report import compute_fifo_round_trips, fetch_daily_metrics


def test_compute_fifo_round_trips_matched():
    fills = [
        {"action": "buy", "side": "yes", "price": 40.0, "count": 10, "created_at": datetime.datetime.now()},
        {"action": "sell", "side": "yes", "price": 45.0, "count": 10, "created_at": datetime.datetime.now()},
    ]
    res = compute_fifo_round_trips(fills)
    assert res["total_round_trips"] == 1
    assert res["winning_trades"] == 1
    assert res["losing_trades"] == 0
    assert res["gross_wins_cents"] == 50.0  # (45 - 40) * 10 = +50c
    assert res["open_unmatched_contracts"] == 0


def test_compute_fifo_round_trips_loss():
    fills = [
        {"action": "buy", "side": "yes", "price": 50.0, "count": 5, "created_at": datetime.datetime.now()},
        {"action": "sell", "side": "yes", "price": 42.0, "count": 5, "created_at": datetime.datetime.now()},
    ]
    res = compute_fifo_round_trips(fills)
    assert res["total_round_trips"] == 1
    assert res["winning_trades"] == 0
    assert res["losing_trades"] == 1
    assert res["gross_losses_cents"] == -40.0  # (42 - 50) * 5 = -40c
    assert res["open_unmatched_contracts"] == 0


def test_fetch_daily_metrics_queries_verified_executed_and_filled_404():
    """Verify that fetch_daily_metrics SQL query filters for both verified_executed and cancelled_or_filled_404."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    # Mock DB queries:
    # 1. Distinct tickers
    mock_cur.fetchall.side_effect = [
        [{"ticker": "KXTEST-TICKER"}],  # distinct tickers
        [],                             # fills for ticker
        [],                             # shard fills
    ]
    # fetchone queries:
    # 2. snapshots agg
    # 3. first_snap
    # 4. last_snap
    # 5. orders agg
    mock_cur.fetchone.side_effect = [
        {"snapshot_count": 0},
        None,  # first_snap
        None,  # last_snap
        {
            "total_orders": 10,
            "buy_orders": 5,
            "sell_orders": 5,
            "resting_orders": 0,
            "cancelled_orders": 8,
            "executed_or_filled_404": 2,
            "total_contracts_quoted": 50,
            "avg_price_cents": 50.0,
        },
    ]

    target_date = datetime.date(2026, 10, 27)
    metrics = fetch_daily_metrics(mock_conn, target_date=target_date)

    # Inspect executed SQL statements
    calls = mock_cur.execute.call_args_list
    sql_statements = [call[0][0] for call in calls]

    # Verify order aggregation query checked both statuses
    agg_query = next(q for q in sql_statements if "COUNT(*) as total_orders" in q)
    assert "status IN ('verified_executed', 'cancelled_or_filled_404')" in agg_query

    # Verify fill query checked both statuses
    fill_query = next(q for q in sql_statements if "SELECT client_order_id, action, side, price" in q)
    assert "status IN ('verified_executed', 'cancelled_or_filled_404')" in fill_query

    assert "KXTEST-TICKER" in metrics["tickers"]
    assert metrics["tickers"]["KXTEST-TICKER"]["fills"] == 2
