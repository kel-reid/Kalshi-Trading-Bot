"""
Unit tests for OrderManager V2 API payload compliance.

These tests validate that the order payloads constructed by OrderManager
conform to the Kalshi V2 API schema requirements, specifically:
- All numeric fields (count, price) are serialized as strings, not integers.
- Endpoint paths use the V2 events/orders path.
- Price is formatted as a fixed-point dollar string (e.g., "0.49").
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from execution.order_manager import OrderManager


@pytest.fixture
def order_manager():
    """Create an OrderManager with mocked database connection."""
    with patch("execution.order_manager.pool.ThreadedConnectionPool") as mock_pool_cls:
        mock_conn = MagicMock()
        mock_conn.closed = False
        mock_pool = MagicMock()
        mock_pool.getconn.return_value = mock_conn
        mock_pool_cls.return_value = mock_pool
        om = OrderManager()
        return om


@pytest.fixture
def order_capture(order_manager):
    """Fixture that intercepts OrderManager REST calls and captures payloads and endpoint paths."""
    captured = {"path": None, "payload": {}}

    def capture_post(sign_path, payload):
        captured["path"] = sign_path
        captured["payload"].update(payload)
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"order": {"order_id": "test-id-123", "status": "resting"}}
        return mock_resp

    order_manager._post_request = capture_post
    return order_manager, captured


class TestV2PayloadSchema:
    """Verify that order payloads match the Kalshi V2 API's expected types."""

    @pytest.mark.asyncio
    async def test_count_field_is_string(self, order_capture):
        """V2 requires 'count' as a string. Sending an int causes a Go unmarshal error."""
        om, captured = order_capture
        await om.place_order(ticker="TEST-TICKER", side="yes", action="buy", count=5, price=49)
        payload = captured["payload"]
        assert "count" in payload, "Payload missing 'count' field"
        assert isinstance(payload["count"], str), (
            f"'count' must be a string for V2 API, got {type(payload['count']).__name__}: {payload['count']}"
        )
        assert payload["count"] == "5"

    @pytest.mark.asyncio
    async def test_price_field_is_dollar_string(self, order_capture):
        """V2 requires 'price' as a fixed-point dollar string (e.g., '0.49'), not cents integer."""
        om, captured = order_capture
        await om.place_order(ticker="TEST-TICKER", side="no", action="sell", count=1, price=42)
        payload = captured["payload"]
        assert "price" in payload, "Payload missing 'price' field"
        assert isinstance(payload["price"], str), (
            f"'price' must be a string for V2 API, got {type(payload['price']).__name__}: {payload['price']}"
        )
        assert payload["price"] == "0.42", (
            f"Price 42 cents should be formatted as '0.42', got '{payload['price']}'"
        )

    @pytest.mark.asyncio
    async def test_price_boundary_one_cent(self, order_capture):
        """Verify 1 cent formats correctly as '0.01'."""
        om, captured = order_capture
        await om.place_order(ticker="TEST-TICKER", side="yes", action="buy", count=1, price=1)
        assert captured["payload"]["price"] == "0.01"

    @pytest.mark.asyncio
    async def test_price_boundary_ninety_nine_cents(self, order_capture):
        """Verify 99 cents formats correctly as '0.99'."""
        om, captured = order_capture
        await om.place_order(ticker="TEST-TICKER", side="yes", action="sell", count=1, price=99)
        assert captured["payload"]["price"] == "0.99"

    @pytest.mark.asyncio
    async def test_payload_uses_v2_events_endpoint(self, order_capture):
        """Verify the order is posted to the V2 events/orders path, not the deprecated V1 path."""
        om, captured = order_capture
        await om.place_order(ticker="TEST-TICKER", side="yes", action="buy", count=1, price=50)
        assert captured["path"] == "/trade-api/v2/portfolio/events/orders", (
            f"Expected V2 events endpoint, got '{captured['path']}'"
        )

    @pytest.mark.asyncio
    async def test_payload_has_no_legacy_yes_no_price_fields(self, order_capture):
        """V2 uses a single 'price' field. Legacy 'yes_price'/'no_price' must not be present."""
        om, captured = order_capture
        await om.place_order(ticker="TEST-TICKER", side="yes", action="buy", count=1, price=50)
        payload = captured["payload"]
        assert "yes_price" not in payload, "Legacy 'yes_price' field must not be in V2 payload"
        assert "no_price" not in payload, "Legacy 'no_price' field must not be in V2 payload"

    @pytest.mark.asyncio
    async def test_payload_contains_all_required_fields(self, order_capture):
        """Verify the payload contains all fields required by the V2 CreateOrderV2Request schema."""
        om, captured = order_capture
        await om.place_order(ticker="TEST-TICKER", side="yes", action="buy", count=3, price=65)
        payload = captured["payload"]

        required_fields = [
            "side", "count", "type", "ticker",
            "client_order_id", "price", "time_in_force",
            "self_trade_prevention_type"
        ]
        for field in required_fields:
            assert field in payload, f"Required V2 field '{field}' missing from payload"

        assert "action" not in payload, "Deprecated 'action' field must be omitted from V2 payload"
        assert payload["side"] == "bid"
        assert payload["count"] == "3"
        assert payload["type"] == "limit"
        assert payload["ticker"] == "TEST-TICKER"
        assert payload["price"] == "0.65"
        assert payload["time_in_force"] == "good_till_canceled"
        assert payload["self_trade_prevention_type"] == "taker_at_cross"

    @pytest.mark.asyncio
    async def test_side_mapping_buy_yes_is_bid(self, order_capture):
        """Buying YES contracts must map to 'bid'."""
        om, captured = order_capture
        await om.place_order(ticker="T", side="yes", action="buy", count=1, price=50)
        assert captured["payload"]["side"] == "bid"

    @pytest.mark.asyncio
    async def test_side_mapping_sell_yes_is_ask(self, order_capture):
        """Selling YES contracts must map to 'ask'."""
        om, captured = order_capture
        await om.place_order(ticker="T", side="yes", action="sell", count=1, price=50)
        assert captured["payload"]["side"] == "ask"

    @pytest.mark.asyncio
    async def test_side_mapping_buy_no_is_ask(self, order_capture):
        """Buying NO contracts must map to 'ask' (selling YES)."""
        om, captured = order_capture
        await om.place_order(ticker="T", side="no", action="buy", count=1, price=50)
        assert captured["payload"]["side"] == "ask"

    @pytest.mark.asyncio
    async def test_side_mapping_sell_no_is_bid(self, order_capture):
        """Selling NO contracts must map to 'bid' (buying YES)."""
        om, captured = order_capture
        await om.place_order(ticker="T", side="no", action="sell", count=1, price=50)
        assert captured["payload"]["side"] == "bid"

    @pytest.mark.asyncio
    async def test_price_field_preserves_subcent_precision(self, order_capture):
        """Sub-cent cent prices (e.g. 32.4c, 32.12c) must format with up to 4 decimal places in dollars."""
        om, captured = order_capture
        # 32.4 cents = $0.324
        await om.place_order(ticker="T", side="yes", action="buy", count=1, price=32.4)
        assert captured["payload"]["price"] == "0.324"

        # 32.12 cents = $0.3212
        await om.place_order(ticker="T", side="yes", action="sell", count=1, price=32.12)
        assert captured["payload"]["price"] == "0.3212"

        # Whole cent 50 cents = $0.50
        await om.place_order(ticker="T", side="yes", action="buy", count=1, price=50)
        assert captured["payload"]["price"] == "0.50"

    @pytest.mark.asyncio
    async def test_place_order_parses_root_v2_order_id(self, order_manager):
        """Verify order_id is properly extracted when returned at the root of the JSON response."""
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {
            "order_id": "kalshi-root-id-456",
            "client_order_id": "client-uuid",
            "fill_count": "0.00",
            "remaining_count": "2.00",
        }
        order_manager._post_request = MagicMock(return_value=mock_resp)
        cid = await order_manager.place_order(ticker="T", side="yes", action="buy", count=2, price=45)
        assert cid in order_manager.active_orders
        assert order_manager.active_orders[cid]["kalshi_order_id"] == "kalshi-root-id-456"

    @pytest.mark.asyncio
    async def test_reconcile_resting_orders_cancels_orphans(self, order_manager):
        """Verify reconcile_resting_orders cancels orders on the exchange that are not tracked locally."""
        order_manager.active_orders["tracked-cid"] = {
            "ticker": "TEST-TICKER",
            "kalshi_order_id": "tracked-kalshi-id"
        }

        mock_get = MagicMock()
        mock_get.status_code = 200
        mock_get.json.return_value = {
            "orders": [
                {"order_id": "tracked-kalshi-id", "client_order_id": "tracked-cid", "ticker": "TEST-TICKER"},
                {"order_id": "orphan-1", "client_order_id": "orphan-cid-1", "ticker": "TEST-TICKER"},
                {"order_id": "orphan-2", "client_order_id": "orphan-cid-2", "ticker": "TEST-TICKER"},
            ]
        }

        with patch("execution.order_manager.requests.get", return_value=mock_get):
            order_manager._cancel_by_kalshi_id = AsyncMock(return_value=True)
            cancelled = await order_manager.reconcile_resting_orders(ticker="TEST-TICKER")

            assert cancelled == 2
            assert order_manager._cancel_by_kalshi_id.call_count == 2
            calls = [c.args for c in order_manager._cancel_by_kalshi_id.call_args_list]
            assert ("orphan-1", "orphan-cid-1") in calls
            assert ("orphan-2", "orphan-cid-2") in calls

    @pytest.mark.asyncio
    async def test_reconcile_resting_orders_returns_none_on_fetch_failure(self, order_manager):
        """Verify reconcile_resting_orders returns None when GET /orders fails."""
        mock_get = MagicMock()
        mock_get.status_code = 500
        mock_get.text = "Internal Server Error"
        with patch("execution.order_manager.requests.get", return_value=mock_get):
            assert await order_manager.reconcile_resting_orders() is None

    @pytest.mark.asyncio
    async def test_reconcile_resting_orders_returns_none_on_cancellation_failure(self, order_manager):
        """Verify reconcile_resting_orders returns None when any orphan cancellation fails."""
        mock_get = MagicMock()
        mock_get.status_code = 200
        mock_get.json.return_value = {
            "orders": [
                {"order_id": "orphan-fail", "client_order_id": "cid-fail", "ticker": "TEST-TICKER"},
            ]
        }
        with patch("execution.order_manager.requests.get", return_value=mock_get):
            order_manager._cancel_by_kalshi_id = AsyncMock(return_value=False)
            assert await order_manager.reconcile_resting_orders(ticker="TEST-TICKER") is None

    @pytest.mark.asyncio
    async def test_cancel_order_404_unverified_retains_order_when_reconciliation_fails(self, order_manager):
        """When 404 occurs on unverified order and reconciliation fails, order tracking is preserved and False returned."""
        cid = "unverified-cid-1"
        order_manager.active_orders[cid] = {"ticker": "TICKER-X", "kalshi_order_id": None}
        mock_del = MagicMock(status_code=404)
        order_manager._delete_request = MagicMock(return_value=mock_del)
        order_manager.reconcile_resting_orders = AsyncMock(return_value=None)

        result = await order_manager.cancel_order(cid)

        assert result is False
        assert cid in order_manager.active_orders
        order_manager.reconcile_resting_orders.assert_awaited_once_with(target_client_order_id=cid)

    @pytest.mark.asyncio
    async def test_cancel_order_404_unverified_clears_order_when_reconciliation_confirms(self, order_manager):
        """When 404 occurs on unverified order and reconciliation succeeds, order is popped and True returned."""
        cid = "unverified-cid-2"
        order_manager.active_orders[cid] = {"ticker": "TICKER-X", "kalshi_order_id": None}
        mock_del = MagicMock(status_code=404)
        order_manager._delete_request = MagicMock(return_value=mock_del)
        order_manager.reconcile_resting_orders = AsyncMock(return_value=1)
        order_manager._update_db_order_status = MagicMock()

        result = await order_manager.cancel_order(cid)

        assert result is True
        assert cid not in order_manager.active_orders
        order_manager.reconcile_resting_orders.assert_awaited_once_with(target_client_order_id=cid)
        order_manager._update_db_order_status.assert_called_once_with(cid, "reconciled_after_404")

    @pytest.mark.asyncio
    async def test_reconcile_resting_orders_follows_cursor_pagination(self, order_manager):
        """Verify reconcile_resting_orders paginates using cursor across multiple pages."""
        page1 = MagicMock(status_code=200)
        page1.json.return_value = {
            "orders": [{"order_id": "p1-orphan", "client_order_id": "p1-cid", "ticker": "T1"}],
            "cursor": "cursor_for_page2",
        }
        page2 = MagicMock(status_code=200)
        page2.json.return_value = {
            "orders": [{"order_id": "p2-orphan", "client_order_id": "p2-cid", "ticker": "T1"}],
            "cursor": None,
        }

        with patch("execution.order_manager.requests.get", side_effect=[page1, page2]) as mock_get:
            order_manager._cancel_by_kalshi_id = AsyncMock(return_value=True)
            cancelled = await order_manager.reconcile_resting_orders(ticker="T1")

            assert cancelled == 2
            assert mock_get.call_count == 2
            # Check params of second call contains cursor
            assert mock_get.call_args_list[0][1]["params"].get("cursor") is None
            assert mock_get.call_args_list[1][1]["params"].get("cursor") == "cursor_for_page2"
            assert order_manager._cancel_by_kalshi_id.call_count == 2

    @pytest.mark.asyncio
    async def test_reconcile_resting_orders_returns_none_if_subsequent_page_fails(self, order_manager):
        """Verify reconcile_resting_orders returns None if any subsequent paginated request fails."""
        page1 = MagicMock(status_code=200)
        page1.json.return_value = {
            "orders": [{"order_id": "p1-orphan", "client_order_id": "p1-cid", "ticker": "T1"}],
            "cursor": "cursor_for_page2",
        }
        page2 = MagicMock(status_code=500, text="Internal Error")

        with patch("execution.order_manager.requests.get", side_effect=[page1, page2]):
            order_manager._cancel_by_kalshi_id = AsyncMock(return_value=True)
            cancelled = await order_manager.reconcile_resting_orders(ticker="T1")

            assert cancelled is None
            # Must not execute any orphan cancellations from partial pages
            order_manager._cancel_by_kalshi_id.assert_not_called()

