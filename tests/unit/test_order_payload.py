"""
Unit tests for OrderManager V2 API payload compliance.

These tests validate that the order payloads constructed by OrderManager
conform to the Kalshi V2 API schema requirements, specifically:
- All numeric fields (count, price) are serialized as strings, not integers.
- Endpoint paths use the V2 events/orders path.
- Price is formatted as a fixed-point dollar string (e.g., "0.49").
"""

import pytest
import json
from unittest.mock import patch, MagicMock, AsyncMock
from execution.order_manager import OrderManager


@pytest.fixture
def order_manager():
    """Create an OrderManager with mocked database connection."""
    with patch("execution.order_manager.psycopg2") as mock_pg:
        mock_conn = MagicMock()
        mock_pg.connect.return_value = mock_conn
        om = OrderManager()
        om.db_conn = mock_conn
        return om


class TestV2PayloadSchema:
    """Verify that order payloads match the Kalshi V2 API's expected types."""

    @pytest.mark.asyncio
    async def test_count_field_is_string(self, order_manager):
        """V2 requires 'count' as a string. Sending an int causes a Go unmarshal error."""
        captured_payload = {}

        def capture_post(sign_path, payload):
            captured_payload.update(payload)
            mock_resp = MagicMock()
            mock_resp.status_code = 201
            mock_resp.json.return_value = {"order": {"order_id": "test-id-123", "status": "resting"}}
            return mock_resp

        order_manager._post_request = capture_post

        await order_manager.place_order(
            ticker="TEST-TICKER",
            side="yes",
            action="buy",
            count=5,
            price=49
        )

        assert "count" in captured_payload, "Payload missing 'count' field"
        assert isinstance(captured_payload["count"], str), (
            f"'count' must be a string for V2 API, got {type(captured_payload['count']).__name__}: {captured_payload['count']}"
        )
        assert captured_payload["count"] == "5"

    @pytest.mark.asyncio
    async def test_price_field_is_dollar_string(self, order_manager):
        """V2 requires 'price' as a fixed-point dollar string (e.g., '0.49'), not cents integer."""
        captured_payload = {}

        def capture_post(sign_path, payload):
            captured_payload.update(payload)
            mock_resp = MagicMock()
            mock_resp.status_code = 201
            mock_resp.json.return_value = {"order": {"order_id": "test-id-456", "status": "resting"}}
            return mock_resp

        order_manager._post_request = capture_post

        await order_manager.place_order(
            ticker="TEST-TICKER",
            side="no",
            action="sell",
            count=1,
            price=42
        )

        assert "price" in captured_payload, "Payload missing 'price' field"
        assert isinstance(captured_payload["price"], str), (
            f"'price' must be a string for V2 API, got {type(captured_payload['price']).__name__}: {captured_payload['price']}"
        )
        assert captured_payload["price"] == "0.42", (
            f"Price 42 cents should be formatted as '0.42', got '{captured_payload['price']}'"
        )

    @pytest.mark.asyncio
    async def test_price_boundary_one_cent(self, order_manager):
        """Verify 1 cent formats correctly as '0.01'."""
        captured_payload = {}

        def capture_post(sign_path, payload):
            captured_payload.update(payload)
            mock_resp = MagicMock()
            mock_resp.status_code = 201
            mock_resp.json.return_value = {"order": {"order_id": "test-id-789", "status": "resting"}}
            return mock_resp

        order_manager._post_request = capture_post

        await order_manager.place_order(
            ticker="TEST-TICKER",
            side="yes",
            action="buy",
            count=1,
            price=1
        )

        assert captured_payload["price"] == "0.01"

    @pytest.mark.asyncio
    async def test_price_boundary_ninety_nine_cents(self, order_manager):
        """Verify 99 cents formats correctly as '0.99'."""
        captured_payload = {}

        def capture_post(sign_path, payload):
            captured_payload.update(payload)
            mock_resp = MagicMock()
            mock_resp.status_code = 201
            mock_resp.json.return_value = {"order": {"order_id": "test-id-abc", "status": "resting"}}
            return mock_resp

        order_manager._post_request = capture_post

        await order_manager.place_order(
            ticker="TEST-TICKER",
            side="yes",
            action="sell",
            count=1,
            price=99
        )

        assert captured_payload["price"] == "0.99"

    @pytest.mark.asyncio
    async def test_payload_uses_v2_events_endpoint(self, order_manager):
        """Verify the order is posted to the V2 events/orders path, not the deprecated V1 path."""
        captured_path = {}

        def capture_post(sign_path, payload):
            captured_path["path"] = sign_path
            mock_resp = MagicMock()
            mock_resp.status_code = 201
            mock_resp.json.return_value = {"order": {"order_id": "test-id-def", "status": "resting"}}
            return mock_resp

        order_manager._post_request = capture_post

        await order_manager.place_order(
            ticker="TEST-TICKER",
            side="yes",
            action="buy",
            count=1,
            price=50
        )

        assert captured_path["path"] == "/trade-api/v2/portfolio/events/orders", (
            f"Expected V2 events endpoint, got '{captured_path['path']}'"
        )

    @pytest.mark.asyncio
    async def test_payload_has_no_legacy_yes_no_price_fields(self, order_manager):
        """V2 uses a single 'price' field. Legacy 'yes_price'/'no_price' must not be present."""
        captured_payload = {}

        def capture_post(sign_path, payload):
            captured_payload.update(payload)
            mock_resp = MagicMock()
            mock_resp.status_code = 201
            mock_resp.json.return_value = {"order": {"order_id": "test-id-ghi", "status": "resting"}}
            return mock_resp

        order_manager._post_request = capture_post

        await order_manager.place_order(
            ticker="TEST-TICKER",
            side="yes",
            action="buy",
            count=1,
            price=50
        )

        assert "yes_price" not in captured_payload, "Legacy 'yes_price' field must not be in V2 payload"
        assert "no_price" not in captured_payload, "Legacy 'no_price' field must not be in V2 payload"

    @pytest.mark.asyncio
    async def test_payload_contains_all_required_fields(self, order_manager):
        """Verify the payload contains all fields required by the V2 CreateOrderV2Request schema."""
        captured_payload = {}

        def capture_post(sign_path, payload):
            captured_payload.update(payload)
            mock_resp = MagicMock()
            mock_resp.status_code = 201
            mock_resp.json.return_value = {"order": {"order_id": "test-id-jkl", "status": "resting"}}
            return mock_resp

        order_manager._post_request = capture_post

        await order_manager.place_order(
            ticker="TEST-TICKER",
            side="yes",
            action="buy",
            count=3,
            price=65
        )

        required_fields = ["action", "side", "count", "type", "ticker", "client_order_id", "price"]
        for field in required_fields:
            assert field in captured_payload, f"Required V2 field '{field}' missing from payload"

        # Verify correct values
        assert captured_payload["action"] == "buy"
        assert captured_payload["side"] == "yes"
        assert captured_payload["count"] == "3"
        assert captured_payload["type"] == "limit"
        assert captured_payload["ticker"] == "TEST-TICKER"
        assert captured_payload["price"] == "0.65"
