"""
Unit Tests for PnL Session Tracking and Database Persistence

Tests session attribution across market rotations and PostgreSQL
snapshot persistence via OrderManager.
"""

import pytest
from unittest.mock import MagicMock, patch

from data.pnl_tracker import PnLTracker
from execution.order_manager import OrderManager


class TestSessionAttributionAndRotation:
    """Tests session tracking across market rotations."""

    def test_rotation_session_reset(self):
        """Starting a new session resets rotation_session_id without dropping ticker state."""
        tracker = PnLTracker()
        ticker = "KXTEST-26SEP-ROT"

        s1 = tracker.get_or_create_market(ticker).rotation_session_id
        assert s1 is not None

        s2 = tracker.reset_market_session(ticker)
        assert s2 != s1
        assert tracker.get_or_create_market(ticker).rotation_session_id == s2

    def test_rotation_session_resets_session_fees_and_realized_deltas(self):
        """Resetting market session resets session_fees_cents and session_realized_pnl_cents to zero while preserving cumulatives."""
        tracker = PnLTracker()
        ticker = "KXTEST-26SEP-FEES-ROT"

        # Trade 1 in session 1: Buy 10 @ 40c, fee = 15c
        tracker.record_fill(ticker, action="buy", side="yes", count=10, price_cents=40, fee_cents=15.0)
        # Sell 10 @ 60c, fee = 15c -> Realized PnL = +200c, Total Fees = 30c
        tracker.record_fill(ticker, action="sell", side="yes", count=10, price_cents=60, fee_cents=15.0)

        s1_summary = tracker.get_market_summary(ticker)
        assert s1_summary["realized_pnl_cents"] == 170.0  # (60 - 40) * 10 - 15 (entry fee) - 15 (exit fee)
        assert s1_summary["total_fees_cents"] == 30.0
        assert s1_summary["session_realized_pnl_cents"] == 170.0
        assert s1_summary["session_fees_cents"] == 30.0

        # Rotate away / reset session
        tracker.reset_market_session(ticker)

        s2_summary = tracker.get_market_summary(ticker)
        # Cumulative totals are conserved
        assert s2_summary["realized_pnl_cents"] == 170.0
        assert s2_summary["total_fees_cents"] == 30.0
        # Session deltas reset to 0
        assert s2_summary["session_realized_pnl_cents"] == 0.0
        assert s2_summary["session_fees_cents"] == 0.0

        # Trade in session 2: Buy 5 @ 50c, fee = 10c
        tracker.record_fill(ticker, action="buy", side="yes", count=5, price_cents=50, fee_cents=10.0)
        s2_mid_summary = tracker.get_market_summary(ticker)
        assert s2_mid_summary["total_fees_cents"] == 40.0
        assert s2_mid_summary["session_fees_cents"] == 10.0


class TestOrderManagerPnLPersistence:
    """Verifies OrderManager persists PnL attribution snapshots to DB."""

    def test_record_pnl_snapshot_sql(self):
        om = OrderManager()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        with patch.object(om, "_get_connection") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn

            om.record_pnl_snapshot(
                ticker="KXTEST-26SEP-PERSIST",
                realized_pnl_cents=125.50,
                unrealized_pnl_cents=32.00,
                total_fees_cents=8.50,
                inventory=5,
                rotation_session_id="11111111-2222-3333-4444-555555555555"
            )

            assert mock_cursor.execute.called
            call_sql = mock_cursor.execute.call_args[0][0]
            call_args = mock_cursor.execute.call_args[0][1]

            assert "INSERT INTO pnl_attribution" in call_sql
            assert call_args[0] == "KXTEST-26SEP-PERSIST"
            assert call_args[2] == 125.5
            assert call_args[3] == 32.0
            assert call_args[4] == 8.5
            assert call_args[5] == 5
            assert call_args[6] == "11111111-2222-3333-4444-555555555555"
            assert mock_conn.commit.called

    def test_record_pnl_snapshot_rejects_invalid_uuid(self):
        om = OrderManager()
        with pytest.raises(ValueError, match="Invalid rotation_session_id"):
            om.record_pnl_snapshot(
                ticker="KXTEST-BAD-UUID",
                realized_pnl_cents=0.0,
                unrealized_pnl_cents=0.0,
                total_fees_cents=0.0,
                inventory=0,
                rotation_session_id="not-a-valid-uuid"
            )

    @pytest.mark.asyncio
    async def test_record_pnl_snapshot_async(self):
        om = OrderManager()
        with patch.object(om, "record_pnl_snapshot") as mock_record:
            await om.record_pnl_snapshot_async(
                ticker="KXTEST-26SEP-ASYNC",
                realized_pnl_cents=50.0,
                unrealized_pnl_cents=10.0,
                total_fees_cents=2.0,
                inventory=2,
                rotation_session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            )
            mock_record.assert_called_once_with(
                ticker="KXTEST-26SEP-ASYNC",
                realized_pnl_cents=50.0,
                unrealized_pnl_cents=10.0,
                total_fees_cents=2.0,
                inventory=2,
                rotation_session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            )

    def test_record_pnl_snapshot_exception_handled(self):
        om = OrderManager()
        with patch.object(om, "_get_connection", side_effect=Exception("DB pool exhausted")):
            # Should not raise exception
            om.record_pnl_snapshot(
                ticker="KXTEST-26SEP-ERR",
                realized_pnl_cents=10.0,
                unrealized_pnl_cents=5.0,
                total_fees_cents=1.0,
                inventory=1,
                rotation_session_id="22222222-3333-4444-5555-666666666666"
            )
