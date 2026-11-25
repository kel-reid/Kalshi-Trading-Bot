"""
Unit Tests for Daily Report Generation
Validates:
1. compute_fifo_round_trips properly calculates FIFO round trips for YES and NO contracts.
2. fetch_daily_metrics includes both 'verified_executed' and 'cancelled_or_filled_404' orders.
"""

import datetime
from unittest.mock import MagicMock, patch
import pytest

from scripts.generate_daily_report import (
    compute_fifo_round_trips,
    fetch_daily_metrics,
    format_markdown_report,
    format_slack_report,
    send_slack_report,
    generate_observations,
    generate_recommendations,
)


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


def test_fetch_daily_metrics_uses_kalshi_fills_and_ignores_404_orders():
    """Verify fetch_daily_metrics reads fills/fees/maker-taker from kalshi_fills and does not count 404 cancel races."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    now = datetime.datetime(2026, 10, 27, 20, 0, 0, tzinfo=datetime.timezone.utc)
    mock_cur.fetchall.side_effect = [
        [{"ticker": "KXTEST-TICKER"}],  # 1. pnl_attribution distinct tickers
        [],                             # 2. orders distinct tickers
        [],                             # 3. kalshi_fills distinct tickers
        [                               # 4. kalshi_fills rows for KXTEST-TICKER
            {
                "client_order_id": "ord-buy-1",
                "action": "buy",
                "side": "yes",
                "price": 45.0,
                "count": 2,
                "fee_cents": 0.0,
                "is_taker": False,
                "created_at": now,
            },
            {
                "client_order_id": "ord-sell-1",
                "action": "sell",
                "side": "yes",
                "price": 50.0,
                "count": 2,
                "fee_cents": 2.0,
                "is_taker": True,
                "created_at": now + datetime.timedelta(seconds=5),
            },
        ],
    ]
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
            "total_contracts_quoted": 50,
            "avg_price_cents": 50.0,
        },
    ]

    target_date = datetime.date(2026, 10, 27)
    metrics = fetch_daily_metrics(mock_conn, target_date=target_date)

    calls = mock_cur.execute.call_args_list
    sql_statements = [call[0][0] for call in calls]

    # orders aggregation must not treat cancelled_or_filled_404 as executed fills
    agg_query = next(q for q in sql_statements if "COUNT(*) as total_orders" in q)
    assert "cancelled_or_filled_404" not in agg_query

    # ground-truth fill reconstruction comes from kalshi_fills
    fill_query = next(q for q in sql_statements if "FROM kalshi_fills" in q and "COALESCE(is_taker, FALSE)" in q)
    assert "fee_dollars * 100.0" in fill_query

    t = metrics["tickers"]["KXTEST-TICKER"]
    assert t["fills"] == 2
    assert t["maker_fills"] == 1
    assert t["taker_fills"] == 1
    assert t["maker_fees_cents"] == 0.0
    assert t["taker_fees_cents"] == 2.0
    assert t["fees_delta_cents"] == 2.0
    assert metrics["totals"]["total_fills"] == 2
    assert metrics["totals"]["total_maker_fills"] == 1
    assert metrics["totals"]["total_taker_fills"] == 1

    md = format_markdown_report(metrics)
    assert "Maker" in md and "Taker" in md
    slack = format_slack_report(metrics)
    assert "1 Maker / 1 Taker" in slack


def test_format_slack_report():
    metrics = {
        "date": "2026-11-22",
        "timezone": "America/New_York",
        "totals": {
            "total_realized_delta_cents": -258.0,
            "total_unrealized_delta_cents": -15.0,
            "total_fees_delta_cents": 73.0,
            "net_strategy_pnl_cents": -273.0,
            "total_orders": 707,
            "total_buy_orders": 343,
            "total_sell_orders": 364,
            "total_fills": 55,
            "total_cancels": 642,
            "active_market_count": 2,
            "total_round_trips": 39,
            "winning_trades": 12,
            "losing_trades": 26,
            "scratch_trades": 1,
            "win_rate_pct": 30.8,
            "win_rate_ex_scratches_pct": 31.6,
            "gross_wins_cents": 388.0,
            "gross_losses_cents": -609.0,
            "avg_win_cents": 32.0,
            "avg_loss_cents": -23.0,
            "profit_factor": 0.64,
        },
        "tickers": {
            "KXTEST-1": {
                "net_pnl_cents": -175.0,
                "fees_delta_cents": 51.0,
                "fills": 15,
                "trade_outcomes": {"win_rate_pct": 10.0, "total_round_trips": 10},
            },
            "KXTEST-2": {
                "net_pnl_cents": 0.0,
                "fees_delta_cents": 69.0,
                "fills": 36,
                "trade_outcomes": {"win_rate_pct": 39.1, "total_round_trips": 20},
            },
        },
    }

    report = format_slack_report(metrics)
    assert "*Kalshi Trading Bot — Daily Performance Report: 2026-11-22*" in report
    assert "    • *Session Realized PnL*: `$-2.58`" in report
    assert "    • *Net Strategy PnL*: `$-2.73`" in report
    assert "    • *Total Session Fees Paid*: `$0.73`" in report
    assert "    • *Win Rate (ex-scratches)*: *31.6%*" in report
    assert "    • *Profit Factor*: *0.64*" in report
    assert "KXTEST-1" in report
    assert "KXTEST-2" in report

    # Verify Section 4 and Section 5 headers and absence of bullet points
    assert "*4. OBSERVATIONS*" in report
    assert "*5. RECOMMENDATIONS*" in report
    sec4_text = report.split("*4. OBSERVATIONS*")[1].split("*5. RECOMMENDATIONS*")[0]
    sec5_text = report.split("*5. RECOMMENDATIONS*")[1].split("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")[0]
    assert "•" not in sec4_text
    assert "•" not in sec5_text


def test_generate_observations_and_recommendations():
    metrics = {
        "date": "2026-11-22",
        "timezone": "America/New_York",
        "totals": {
            "total_realized_delta_cents": -258.0,
            "total_fees_delta_cents": 73.0,
            "total_orders": 707,
            "total_fills": 55,
            "total_buy_orders": 343,
            "total_sell_orders": 364,
            "total_cancels": 642,
            "snapshot_count": 100,
        },
        "tickers": {
            "KXTEST-1": {
                "realized_delta_cents": -175.0,
                "min_inventory": -8,
                "max_inventory": 6,
                "ending_inventory": 0,
                "has_inventory_snapshot": True,
            },
        },
    }
    obs = generate_observations(metrics)
    recs = generate_recommendations(metrics)
    assert len(obs) > 0
    assert len(recs) > 0
    # Check that neither obs nor recs start with bullet characters
    for o in obs:
        assert not o.strip().startswith("•")
        assert not o.strip().startswith("- ")
    for r in recs:
        assert not r.strip().startswith("•")
        assert not r.strip().startswith("- ")
        # Ensure recommendations formulate a clear consensus without ambiguous ' or ' options
        assert " or " not in r.lower()
        assert "consensus" in r.lower()


def test_drawdown_concentration_majority_vs_minority():
    # Majority case: qualifying loss is 175 of 200 (87.5% >= 50%)
    majority_metrics = {
        "totals": {"total_realized_delta_cents": -200.0, "snapshot_count": 50},
        "tickers": {
            "KXTEST-1": {"realized_delta_cents": -175.0, "min_inventory": -8, "max_inventory": 6, "has_inventory_snapshot": True},
            "KXTEST-2": {"realized_delta_cents": -25.0, "min_inventory": -1, "max_inventory": 1, "has_inventory_snapshot": True},
        },
    }
    obs_maj = generate_observations(majority_metrics)
    assert any("concentrated in `KXTEST-1`" in o and "87.5%" in o for o in obs_maj)
    # Ensure unmeasured causal claims are absent
    for o in obs_maj:
        assert "score momentum" not in o
        assert "collars" not in o

    # Minority case: qualifying loss is 60 of 1000 (6.0% < 50%)
    minority_metrics = {
        "totals": {"total_realized_delta_cents": -1000.0, "snapshot_count": 50},
        "tickers": {
            "KXTEST-A": {"realized_delta_cents": -60.0, "min_inventory": -4, "max_inventory": 4, "has_inventory_snapshot": True},
            "KXTEST-B": {"realized_delta_cents": -940.0, "min_inventory": -1, "max_inventory": 1, "has_inventory_snapshot": True},
        },
    }
    obs_min = generate_observations(minority_metrics)
    # Should NOT claim majority
    assert not any("accounting for" in o and "majority" in o for o in obs_min)
    assert any("Significant drawdown and inventory excursions occurred in `KXTEST-A`" in o for o in obs_min)

    # Offsetting profit case: A loses 60c (inv 4), B loses 940c (inv 1), C gains 900c.
    # Net session realized is -100c, but gross ticker losses are 1,000c.
    # A accounts for only 6% of gross ticker losses, so it must NOT be reported as a 60% majority.
    offsetting_profit_metrics = {
        "totals": {"total_realized_delta_cents": -100.0, "snapshot_count": 50},
        "tickers": {
            "KXTEST-A": {"realized_delta_cents": -60.0, "min_inventory": -4, "max_inventory": 4, "has_inventory_snapshot": True},
            "KXTEST-B": {"realized_delta_cents": -940.0, "min_inventory": -1, "max_inventory": 1, "has_inventory_snapshot": True},
            "KXTEST-C": {"realized_delta_cents": 900.0, "min_inventory": 0, "max_inventory": 0, "has_inventory_snapshot": True},
        },
    }
    obs_offset = generate_observations(offsetting_profit_metrics)
    assert not any("accounting for" in o for o in obs_offset)
    assert any("Significant drawdown and inventory excursions occurred in `KXTEST-A`" in o for o in obs_offset)

    # All-negative inventory range case: min -8, max -4
    # Must format as `[-8, -4]` and NOT `[-8, +-4]`
    all_neg_metrics = {
        "totals": {"total_realized_delta_cents": -200.0, "snapshot_count": 50},
        "tickers": {
            "KXTEST-NEG": {"realized_delta_cents": -175.0, "min_inventory": -8, "max_inventory": -4, "has_inventory_snapshot": True},
        },
    }
    obs_all_neg = generate_observations(all_neg_metrics)
    assert any("`KXTEST-NEG` ($-1.75, inv [-8, -4])" in o for o in obs_all_neg)
    assert not any("+-4" in o for o in obs_all_neg)


def test_ending_inventory_telemetry_availability():
    # Case 1: No snapshots recorded
    no_snap_metrics = {
        "totals": {"snapshot_count": 0, "total_orders": 10},
        "tickers": {"KXTEST-1": {"ending_inventory": 0, "has_inventory_snapshot": False, "snapshot_count": 0}},
    }
    obs_no_snap = generate_observations(no_snap_metrics)
    assert any("Inventory snapshot telemetry was unavailable" in o for o in obs_no_snap)
    assert not any("All event markets resolved or liquidated to exactly 0" in o for o in obs_no_snap)

    # Case 2: Complete snapshots recorded with 5 open contracts
    open_inv_metrics = {
        "totals": {"snapshot_count": 50, "total_orders": 10},
        "tickers": {"KXTEST-1": {"ending_inventory": 5, "has_inventory_snapshot": True, "snapshot_count": 50}},
    }
    obs_open = generate_observations(open_inv_metrics)
    assert any("Carried 5 open contracts into session close" in o for o in obs_open)


def test_ending_inventory_telemetry_partial_coverage():
    # Case 1: Partial coverage with 0 measured contracts
    partial_flat_metrics = {
        "totals": {"snapshot_count": 25, "total_orders": 20},
        "tickers": {
            "KXTEST-MEASURED": {"ending_inventory": 0, "has_inventory_snapshot": True, "snapshot_count": 25, "net_pnl_cents": 0, "fees_delta_cents": 0},
            "KXTEST-UNRECORDED": {"ending_inventory": 0, "has_inventory_snapshot": False, "snapshot_count": 0, "net_pnl_cents": 0, "fees_delta_cents": 0},
        },
    }
    obs_partial_flat = generate_observations(partial_flat_metrics)
    assert any("Measured markets resolved or liquidated to 0 open contracts before session close (1 market(s) lacked snapshot telemetry)." in o for o in obs_partial_flat)
    slack_partial_flat = format_slack_report(partial_flat_metrics)
    assert "• *Ending Open Inventory*: `0` contracts (partial: 1 market(s) unrecorded)" in slack_partial_flat

    # Case 2: Partial coverage with 5 measured open contracts
    partial_open_metrics = {
        "totals": {"snapshot_count": 25, "total_orders": 20},
        "tickers": {
            "KXTEST-MEASURED": {"ending_inventory": 5, "has_inventory_snapshot": True, "snapshot_count": 25, "net_pnl_cents": 0, "fees_delta_cents": 0},
            "KXTEST-UNRECORDED": {"ending_inventory": 0, "has_inventory_snapshot": False, "snapshot_count": 0, "net_pnl_cents": 0, "fees_delta_cents": 0},
        },
    }
    obs_partial_open = generate_observations(partial_open_metrics)
    assert any("Carried 5 open contracts into session close across measured markets (1 market(s) lacked snapshot telemetry)." in o for o in obs_partial_open)
    slack_partial_open = format_slack_report(partial_open_metrics)
    assert "• *Ending Open Inventory*: `5` contracts (partial: 1 market(s) unrecorded)" in slack_partial_open


def test_inventory_risk_consensus_no_incorrect_gamma_baseline():
    metrics = {
        "totals": {"total_realized_delta_cents": 0.0, "total_orders": 50, "snapshot_count": 10},
        "tickers": {"KXTEST-SKEW": {"min_inventory": -12, "max_inventory": 2}},
    }
    recs = generate_recommendations(metrics)
    assert any("Inventory Risk Consensus" in r for r in recs)
    for r in recs:
        # Assert no hardcoded obsolete baseline
        assert "from 0.7 to 1.0" not in r
        assert "0.7" not in r


def test_slack_summary_ending_inventory_consistency():
    # Case 1: Open contracts carried
    metrics_open = {
        "date": "2026-11-22",
        "timezone": "America/New_York",
        "totals": {
            "total_realized_delta_cents": 0.0,
            "total_unrealized_delta_cents": 0.0,
            "total_fees_delta_cents": 0.0,
            "net_strategy_pnl_cents": 0.0,
            "snapshot_count": 20,
            "total_orders": 10,
        },
        "tickers": {
            "KXTEST-1": {"ending_inventory": 5, "has_inventory_snapshot": True, "net_pnl_cents": 0, "fees_delta_cents": 0},
        },
    }
    report_open = format_slack_report(metrics_open)
    assert "• *Ending Open Inventory*: `5` contracts" in report_open

    # Case 2: No snapshot telemetry
    metrics_no_snap = {
        "date": "2026-11-22",
        "timezone": "America/New_York",
        "totals": {
            "total_realized_delta_cents": 0.0,
            "total_unrealized_delta_cents": 0.0,
            "total_fees_delta_cents": 0.0,
            "net_strategy_pnl_cents": 0.0,
            "snapshot_count": 0,
            "total_orders": 10,
        },
        "tickers": {
            "KXTEST-1": {"ending_inventory": 0, "has_inventory_snapshot": False, "net_pnl_cents": 0, "fees_delta_cents": 0},
        },
    }
    report_no_snap = format_slack_report(metrics_no_snap)
    assert "• *Ending Open Inventory*: `N/A` (no snapshot telemetry)" in report_no_snap


def test_send_slack_report_success():
    with patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 200
        mock_post.return_value.text = "ok"

        success = send_slack_report("https://hooks.slack.com/services/TEST", "Test Report")
        assert success is True
        mock_post.assert_called_once_with(
            "https://hooks.slack.com/services/TEST",
            json={"text": "Test Report"},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )


def test_send_slack_report_with_channel():
    with patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 200
        mock_post.return_value.text = "ok"

        success = send_slack_report("https://hooks.slack.com/services/TEST", "Test Report", channel="#reports")
        assert success is True
        mock_post.assert_called_once_with(
            "https://hooks.slack.com/services/TEST",
            json={"text": "Test Report", "channel": "#reports"},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )


def test_send_slack_report_failure_status():
    with patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 404
        mock_post.return_value.text = "Not Found"

        success = send_slack_report("https://hooks.slack.com/services/TEST", "Test Report")
        assert success is False


def test_send_slack_report_no_url():
    success = send_slack_report(None, "Test Report")
    assert success is False

