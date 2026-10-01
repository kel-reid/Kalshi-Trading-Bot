"""
Unit Tests for InventoryManager Ingress Validation

Tests boundary validation, rejection of malformed payloads (non-positive counts,
invalid actions/sides, negative or non-numeric prices/fees), and safe parsing of
raw vendor YES/NO fill messages and REST snapshots.
"""

import pytest
from unittest.mock import MagicMock, patch

from data.inventory_manager import InventoryManager


class TestInventoryIngressValidation:
    """Verifies InventoryManager ingress boundary validations."""

    def test_inventory_manager_rejects_non_positive_fill_counts(self):
        """Verify _handle_fill rejects count <= 0 at the ingress boundary without mutating any state."""
        mock_ws = MagicMock()
        im = InventoryManager(mock_ws)
        im.balance_cents = 10000
        initial_fill_count = im._fill_count

        # Zero count
        im._handle_fill({"market_ticker": "KXTEST-REJECT", "action": "buy", "side": "yes", "count": 0, "price": 50})
        # Negative count
        im._handle_fill({"market_ticker": "KXTEST-REJECT", "action": "buy", "side": "yes", "count": -5, "price": 50})
        im._handle_fill({"market_ticker": "KXTEST-REJECT", "action": "sell", "side": "yes", "count": -1, "price": 50})

        assert im.balance_cents == 10000
        assert im.get_position("KXTEST-REJECT") == 0
        assert im._fill_count == initial_fill_count
        assert im.get_realized_pnl("KXTEST-REJECT") == 0.0

    def test_inventory_manager_rejects_invalid_action_and_side(self):
        """Verify _handle_fill rejects invalid action and side values without mutating state or counters."""
        mock_ws = MagicMock()
        im = InventoryManager(mock_ws)
        im.balance_cents = 10000
        initial_fill_count = im._fill_count

        # Missing action
        im._handle_fill({"market_ticker": "KXTEST-REJECT", "side": "yes", "count": 5, "price": 50})
        # Invalid action
        im._handle_fill({"market_ticker": "KXTEST-REJECT", "action": "hold", "side": "yes", "count": 5, "price": 50})
        # Missing side
        im._handle_fill({"market_ticker": "KXTEST-REJECT", "action": "buy", "count": 5, "price": 50})
        # Invalid side
        im._handle_fill({"market_ticker": "KXTEST-REJECT", "action": "buy", "side": "maybe", "count": 5, "price": 50})

        assert im.balance_cents == 10000
        assert im.get_position("KXTEST-REJECT") == 0
        assert im._fill_count == initial_fill_count
        assert im.get_realized_pnl("KXTEST-REJECT") == 0.0

    def test_inventory_manager_rejects_missing_non_numeric_and_negative_prices(self):
        """Verify _handle_fill drops missing, non-numeric, zero, negative, out-of-range (>=100), or boolean prices without incrementing _fill_count."""
        mock_ws = MagicMock()
        im = InventoryManager(ws_client=mock_ws)
        initial_balance = im.balance_cents

        invalid_prices = [None, "", "invalid", 0, -10, -0.01, True, False, 100, 100.5, 150, float("nan"), float("inf")]
        for p in invalid_prices:
            im._handle_fill({
                "market_ticker": "KXTEST-PRICE",
                "action": "buy",
                "side": "yes",
                "count": 5,
                "price": p
            })
            assert im._fill_count == 0, f"Expected _fill_count == 0 for price {p!r}"
            assert im.balance_cents == initial_balance
            assert im.get_position("KXTEST-PRICE") == 0

    def test_inventory_manager_rejects_invalid_fees_without_incrementing_counter(self):
        """Verify _handle_fill drops non-numeric or negative fees without incrementing _fill_count or mutating balance."""
        mock_ws = MagicMock()
        im = InventoryManager(ws_client=mock_ws)
        initial_balance = im.balance_cents

        invalid_fees = ["bad-fee", -5.0, -0.001, float("nan"), float("inf")]
        for f in invalid_fees:
            im._handle_fill({
                "market_ticker": "KXTEST-FEE",
                "action": "buy",
                "side": "yes",
                "count": 5,
                "price": 50,
                "fee_cents": f
            })
            assert im._fill_count == 0, f"Expected _fill_count == 0 for fee {f!r}"
            assert im.balance_cents == initial_balance
            assert im.get_position("KXTEST-FEE") == 0

    def test_fetch_balance_rejects_missing_or_invalid_balance_field(self):
        """Verify _fetch_balance returns None when 'balance' is missing or non-numeric/boolean."""
        im = InventoryManager(ws_client=MagicMock())

        invalid_responses = [
            {},
            {"other_key": 5000},
            {"balance": None},
            {"balance": "5000"},
            {"balance": True},
            {"balance": False},
            {"balance": float("nan")},
            {"balance": float("inf")},
            {"balance": float("-inf")},
        ]
        for invalid_json in invalid_responses:
            mock_resp = MagicMock(status_code=200)
            mock_resp.json.return_value = invalid_json
            with patch("requests.get", return_value=mock_resp):
                assert im._fetch_balance() is None, f"Expected None for invalid payload: {invalid_json}"

        # Valid balance returns integer cents
        valid_resp = MagicMock(status_code=200)
        valid_resp.json.return_value = {"balance": 12500}
        with patch("requests.get", return_value=valid_resp):
            assert im._fetch_balance() == 12500

    def test_fetch_positions_rejects_missing_or_invalid_positions_field(self):
        """Verify _fetch_positions returns None when 'market_positions' is missing or not a list."""
        im = InventoryManager(ws_client=MagicMock())

        invalid_responses = [
            {},
            {"other_key": []},
            {"market_positions": None},
            {"market_positions": "invalid"},
            {"market_positions": 123},
        ]
        for invalid_json in invalid_responses:
            mock_resp = MagicMock(status_code=200)
            mock_resp.json.return_value = invalid_json
            with patch("requests.get", return_value=mock_resp):
                assert im._fetch_positions() is None, f"Expected None for invalid payload: {invalid_json}"

        # Valid positions returns the list
        valid_resp = MagicMock(status_code=200)
        valid_resp.json.return_value = {"market_positions": [{"ticker": "KXTEST", "position": 5}]}
        with patch("requests.get", return_value=valid_resp):
            assert im._fetch_positions() == [{"ticker": "KXTEST", "position": 5}]

    def test_handle_fill_supports_raw_vendor_yes_and_no_price_payloads(self):
        """Verify _handle_fill parses real Kalshi vendor fill messages carrying yes_price and no_price."""
        im = InventoryManager(ws_client=MagicMock())
        im.balance_cents = 10000

        # 1. Raw Kalshi YES fill payload with yes_price=45, no_price=55
        raw_yes_fill = {
            "trade_id": "t-1",
            "market_ticker": "KXTEST-VENDOR",
            "action": "buy",
            "side": "yes",
            "count": 10,
            "yes_price": 45,
            "no_price": 55,
            "fee_cents": 2.0
        }
        im._handle_fill(raw_yes_fill)
        assert im._fill_count == 1
        assert im.get_position("KXTEST-VENDOR") == 10
        # Paid (45 * 10) + 2 fee = 452c
        assert im.balance_cents == 10000 - 452
        assert im.get_realized_pnl("KXTEST-VENDOR") == 0.0

        # 2. Raw Kalshi NO fill payload with yes_price=45, no_price=55
        raw_no_fill = {
            "trade_id": "t-2",
            "market_ticker": "KXTEST-VENDOR-NO",
            "action": "buy",
            "side": "no",
            "count": 5,
            "yes_price": 45,
            "no_price": 55,
            "fee_cents": 1.0
        }
        im._handle_fill(raw_no_fill)
        assert im._fill_count == 2
        # Bought NO -> Net YES position is -5
        assert im.get_position("KXTEST-VENDOR-NO") == -5
        # Paid (55 * 5) + 1 fee = 276c
        assert im.balance_cents == (10000 - 452) - 276

    def test_apply_positions_atomically_rejects_malformed_entries_without_partial_mutation(self):
        """Verify _apply_positions rejects malformed snapshots upfront without partially mutating state or PnL lots."""
        im = InventoryManager(ws_client=MagicMock())
        im.positions = {"KXTEST-EXISTING": 5}

        # 1. Non-dict entry in market_positions
        malformed_snapshot_1 = [
            {"ticker": "KXTEST-NEW", "position": 10},
            "not-a-dict"
        ]
        im._apply_positions(malformed_snapshot_1, is_startup=True)
        assert im.positions == {"KXTEST-EXISTING": 5}
        assert len(im.pnl_tracker.get_or_create_market("KXTEST-NEW").open_lots) == 0

        # 2. Malformed non-numeric position_fp value
        malformed_snapshot_2 = [
            {"ticker": "KXTEST-NEW", "position": 10},
            {"ticker": "KXTEST-BAD", "position_fp": "invalid-pos"}
        ]
        im._apply_positions(malformed_snapshot_2, is_startup=True)
        assert im.positions == {"KXTEST-EXISTING": 5}
        assert len(im.pnl_tracker.get_or_create_market("KXTEST-NEW").open_lots) == 0

        # 3. Missing ticker
        malformed_snapshot_3 = [
            {"ticker": "KXTEST-NEW", "position": 10},
            {"position": 5}
        ]
        assert im._apply_positions(malformed_snapshot_3, is_startup=False) is False
        assert im.positions == {"KXTEST-EXISTING": 5}
        assert len(im.pnl_tracker.get_or_create_market("KXTEST-NEW").open_lots) == 0

        # 4. Missing position field (neither position_fp nor position provided)
        malformed_snapshot_4 = [
            {"ticker": "KXTEST-NEW", "position": 10},
            {"ticker": "KXTEST-NOPOS", "market_exposure": 500}
        ]
        assert im._apply_positions(malformed_snapshot_4, is_startup=False) is False
        assert im.positions == {"KXTEST-EXISTING": 5}
        assert len(im.pnl_tracker.get_or_create_market("KXTEST-NEW").open_lots) == 0

        # 5. Malformed non-finite position
        malformed_snapshot_5 = [
            {"ticker": "KXTEST-NEW", "position": 10},
            {"ticker": "KXTEST-NAN", "position_fp": "nan"}
        ]
        assert im._apply_positions(malformed_snapshot_5, is_startup=False) is False
        assert im.positions == {"KXTEST-EXISTING": 5}
        assert len(im.pnl_tracker.get_or_create_market("KXTEST-NEW").open_lots) == 0

    def test_fetch_positions_rejects_non_string_and_blank_tickers(self):
        """Verify _fetch_positions rejects truthy non-string tickers (int, bool), blank strings, and missing position fields."""
        im = InventoryManager(ws_client=MagicMock())

        # 1. Non-string integer ticker
        resp_int = MagicMock(status_code=200)
        resp_int.json.return_value = {"market_positions": [{"ticker": 123, "position": 5}]}
        with patch("requests.get", return_value=resp_int):
            assert im._fetch_positions() is None

        # 2. Blank whitespace ticker
        resp_blank = MagicMock(status_code=200)
        resp_blank.json.return_value = {"market_positions": [{"ticker": "   ", "position": 5}]}
        with patch("requests.get", return_value=resp_blank):
            assert im._fetch_positions() is None

        # 3. Boolean ticker
        resp_bool = MagicMock(status_code=200)
        resp_bool.json.return_value = {"market_positions": [{"ticker": True, "position": 5}]}
        with patch("requests.get", return_value=resp_bool):
            assert im._fetch_positions() is None

        # 4. Missing position and position_fp fields
        resp_nopos = MagicMock(status_code=200)
        resp_nopos.json.return_value = {"market_positions": [{"ticker": "KXTEST-NOPOS", "market_exposure": 500}]}
        with patch("requests.get", return_value=resp_nopos):
            assert im._fetch_positions() is None

        # 5. Non-finite position values
        resp_nan = MagicMock(status_code=200)
        resp_nan.json.return_value = {"market_positions": [{"ticker": "KXTEST-NAN", "position_fp": "nan"}]}
        with patch("requests.get", return_value=resp_nan):
            assert im._fetch_positions() is None

        resp_inf = MagicMock(status_code=200)
        resp_inf.json.return_value = {"market_positions": [{"ticker": "KXTEST-INF", "position": float("inf")}]}
        with patch("requests.get", return_value=resp_inf):
            assert im._fetch_positions() is None

    def test_apply_positions_startup_ignores_non_finite_exposure(self):
        """Verify startup hydration ignores non-finite exposure (leaves lot uncosted) without error."""
        im = InventoryManager(ws_client=MagicMock())
        snapshot = [
            {"ticker": "KXTEST-NAN-EXP", "position": 10, "market_exposure": "nan"},
            {"ticker": "KXTEST-INF-EXP", "position": 5, "market_exposure": float("inf")},
            {"ticker": "KXTEST-ZERO-EXP", "position": 4, "market_exposure": 0},
            {"ticker": "KXTEST-VALID-EXP", "position": 2, "market_exposure": 80.0},
        ]
        assert im._apply_positions(snapshot, is_startup=True) is True
        # Uncosted lots:
        assert im.pnl_tracker.get_or_create_market("KXTEST-NAN-EXP").open_lots[0].is_uncosted is True
        assert im.pnl_tracker.get_or_create_market("KXTEST-INF-EXP").open_lots[0].is_uncosted is True
        assert im.pnl_tracker.get_or_create_market("KXTEST-ZERO-EXP").open_lots[0].is_uncosted is True
        # Costed lot: 80 / 2 = 40.0c
        valid_lot = im.pnl_tracker.get_or_create_market("KXTEST-VALID-EXP").open_lots[0]
        assert valid_lot.is_uncosted is False
        assert valid_lot.price_cents == 40.0

    def test_handle_fill_complements_opposite_side_price(self):
        """Verify _handle_fill calculates 100c complement when only opposite-side price is provided."""
        im = InventoryManager(ws_client=MagicMock())
        im.balance_cents = 10000.0

        # 1. YES fill with only no_price=55 -> YES price must be 100 - 55 = 45c
        im._handle_fill({
            "market_ticker": "KXTEST-COMP",
            "action": "buy",
            "side": "yes",
            "count": 10,
            "no_price": 55,
            "fee_cents": 1.0
        })
        assert im.get_position("KXTEST-COMP") == 10
        # Cost: 10 * 45c + 1c fee = 451c -> balance = 10000 - 451 = 9549.0
        assert im.balance_cents == 9549.0

        # 2. NO fill with only yes_price=40 -> NO price must be 100 - 40 = 60c
        im._handle_fill({
            "market_ticker": "KXTEST-COMP-NO",
            "action": "buy",
            "side": "no",
            "count": 5,
            "yes_price": 40,
            "fee_cents": 1.5
        })
        # Buying NO -> Net YES position is -5
        assert im.get_position("KXTEST-COMP-NO") == -5
        # Cost: 5 * 60c + 1.5c fee = 301.5c -> balance = 9549.0 - 301.5 = 9247.5
        assert im.balance_cents == 9247.5

    def test_fetch_and_apply_positions_supports_fractional_and_integral_positions(self):
        """Verify _fetch_positions and _apply_positions parse both fractional and integral position values accurately."""
        im = InventoryManager(ws_client=MagicMock())

        # 1. _fetch_positions accepts fractional and integral positions
        mock_resp_frac1 = MagicMock(status_code=200)
        mock_resp_frac1.json.return_value = {"market_positions": [{"ticker": "KXTEST-FRAC", "position": "1.5"}]}
        with patch("requests.get", return_value=mock_resp_frac1):
            pos_list = im._fetch_positions()
            assert pos_list is not None
            assert pos_list[0]["position"] == "1.5"

        mock_resp_frac2 = MagicMock(status_code=200)
        mock_resp_frac2.json.return_value = {"market_positions": [{"ticker": "KXTEST-FRAC2", "position_fp": "1.69"}]}
        with patch("requests.get", return_value=mock_resp_frac2):
            pos_list = im._fetch_positions()
            assert pos_list is not None
            assert pos_list[0]["position_fp"] == "1.69"

        # Rejects non-finite positions
        mock_resp_nan = MagicMock(status_code=200)
        mock_resp_nan.json.return_value = {"market_positions": [{"ticker": "KXTEST-NAN", "position": "nan"}]}
        with patch("requests.get", return_value=mock_resp_nan):
            assert im._fetch_positions() is None

        # Integral float (e.g. 5.0) is accepted
        mock_resp_int = MagicMock(status_code=200)
        mock_resp_int.json.return_value = {"market_positions": [{"ticker": "KXTEST-INT", "position": "5.0"}]}
        with patch("requests.get", return_value=mock_resp_int):
            pos_list = im._fetch_positions()
            assert pos_list is not None
            assert len(pos_list) == 1
            assert pos_list[0]["ticker"] == "KXTEST-INT"

        # 2. _apply_positions parses fractional and integral positions
        assert im._apply_positions([{"ticker": "KXTEST-FRAC-1", "position": "3.5"}]) is True
        assert im.get_position("KXTEST-FRAC-1") == 3.5
        assert im._apply_positions([{"ticker": "KXTEST-FRAC-2", "position_fp": "1.69"}]) is True
        assert im.get_position("KXTEST-FRAC-2") == 1.69
        assert im._apply_positions([{"ticker": "KXTEST-INT-OK", "position": 4.0}]) is True
        assert im.get_position("KXTEST-INT-OK") == 4
        assert isinstance(im.get_position("KXTEST-INT-OK"), int)

        # Rejects non-finite position values
        assert im._apply_positions([{"ticker": "KXTEST-INF", "position": float("inf")}]) is False

    def test_handle_fill_drops_infinite_opposite_side_prices_without_overflow_error(self):
        """Verify _handle_fill gracefully drops infinite or NaN opposite-side fallback prices without OverflowError."""
        im = InventoryManager(ws_client=MagicMock())
        initial_fill_count = im._fill_count

        infinite_payloads = [
            {"market_ticker": "KXTEST-INF1", "action": "buy", "side": "yes", "count": 1, "no_price": float("inf")},
            {"market_ticker": "KXTEST-INF2", "action": "buy", "side": "yes", "count": 1, "no_price": float("-inf")},
            {"market_ticker": "KXTEST-INF3", "action": "buy", "side": "no", "count": 1, "yes_price": float("inf")},
            {"market_ticker": "KXTEST-INF4", "action": "buy", "side": "no", "count": 1, "yes_price": float("-inf")},
            {"market_ticker": "KXTEST-NAN", "action": "buy", "side": "yes", "count": 1, "no_price": float("nan")},
        ]

        for payload in infinite_payloads:
            # Must not raise OverflowError or mutate state
            im._handle_fill(payload)
            assert im._fill_count == initial_fill_count
            assert im.get_position(payload["market_ticker"]) == 0
