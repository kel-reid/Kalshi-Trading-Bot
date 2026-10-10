"""
Unit tests for emergency KillSwitch.

Verifies synchronous and asynchronous cancellation routines, ensuring:
1. Orders with verified exchange IDs are cancelled via /portfolio/events/orders/{kalshi_order_id}.
2. Unverified orders (kalshi_order_id is None) are reconciled against /portfolio/orders.
3. Missing exchange order IDs do NOT fall back to client_order_id.
4. Unverified orders are NOT popped if reconciliation fails.
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from execution.kill_switch import KillSwitch


@pytest.fixture
def mock_order_manager():
    om = MagicMock()
    om.active_orders = {}
    return om


def test_kill_switch_sync_no_orders(mock_order_manager):
    killer = KillSwitch(mock_order_manager)
    with patch("requests.delete") as mock_del:
        killer.trigger_synchronous()
        mock_del.assert_not_called()


def test_kill_switch_sync_cancels_verified_orders(mock_order_manager):
    cid = "client-uuid-1"
    kid = "kalshi-exchange-id-1"
    mock_order_manager.active_orders[cid] = {
        "ticker": "KXTEST",
        "kalshi_order_id": kid,
        "count": 2,
    }

    killer = KillSwitch(mock_order_manager)
    mock_resp = MagicMock(status_code=200)

    with patch("requests.delete", return_value=mock_resp) as mock_del, \
         patch("execution.kill_switch.get_auth_headers", return_value={"test": "header"}):
        killer.trigger_synchronous()

        assert cid not in mock_order_manager.active_orders
        mock_del.assert_called_once()
        called_url = mock_del.call_args[0][0]
        assert kid in called_url
        assert cid not in called_url


def test_kill_switch_sync_reconciles_missing_exchange_id(mock_order_manager):
    cid = "client-uuid-2"
    kid = "kalshi-exchange-id-2"
    # Unverified order: kalshi_order_id is None
    mock_order_manager.active_orders[cid] = {
        "ticker": "KXTEST",
        "kalshi_order_id": None,
        "count": 3,
    }

    killer = KillSwitch(mock_order_manager)
    mock_resting = [
        {"client_order_id": cid, "order_id": kid, "ticker": "KXTEST"}
    ]
    mock_del_resp = MagicMock(status_code=200)

    with patch.object(killer, "_fetch_resting_orders_sync", return_value=mock_resting) as mock_fetch, \
         patch("requests.delete", return_value=mock_del_resp) as mock_del, \
         patch("execution.kill_switch.get_auth_headers", return_value={"test": "header"}):
        killer.trigger_synchronous()

        mock_fetch.assert_called_once()
        assert cid not in mock_order_manager.active_orders
        mock_del.assert_called_once()
        called_url = mock_del.call_args[0][0]
        assert kid in called_url
        assert cid not in called_url


def test_kill_switch_sync_pops_order_when_reconciled_not_on_exchange(mock_order_manager):
    cid = "client-uuid-3"
    # Unverified order: kalshi_order_id is None
    mock_order_manager.active_orders[cid] = {
        "ticker": "KXTEST",
        "kalshi_order_id": None,
        "count": 1,
    }

    killer = KillSwitch(mock_order_manager)
    # Resting orders returned, but client_order_id is NOT in the list
    mock_resting = [
        {"client_order_id": "other-cid", "order_id": "other-kid", "ticker": "KXTEST"}
    ]

    with patch.object(killer, "_fetch_resting_orders_sync", return_value=mock_resting) as mock_fetch, \
         patch("requests.delete") as mock_del:
        killer.trigger_synchronous()

        mock_fetch.assert_called_once()
        # Order confirmed not resting, popped safely without firing DELETE
        assert cid not in mock_order_manager.active_orders
        mock_del.assert_not_called()


def test_kill_switch_sync_retains_order_when_reconciliation_fails(mock_order_manager):
    cid = "client-uuid-4"
    mock_order_manager.active_orders[cid] = {
        "ticker": "KXTEST",
        "kalshi_order_id": None,
        "count": 5,
    }

    killer = KillSwitch(mock_order_manager)

    with patch.object(killer, "_fetch_resting_orders_sync", return_value=None) as mock_fetch, \
         patch("requests.delete") as mock_del:
        killer.trigger_synchronous()

        mock_fetch.assert_called_once()
        # Order is NOT popped and NO DELETE request is fired
        assert cid in mock_order_manager.active_orders
        mock_del.assert_not_called()


def test_kill_switch_sync_verified_404_pops_order(mock_order_manager):
    cid = "client-uuid-5"
    kid = "kalshi-exchange-id-5"
    mock_order_manager.active_orders[cid] = {
        "ticker": "KXTEST",
        "kalshi_order_id": kid,
        "count": 1,
    }

    killer = KillSwitch(mock_order_manager)
    mock_del_resp = MagicMock(status_code=404)

    with patch("requests.delete", return_value=mock_del_resp), \
         patch("execution.kill_switch.get_auth_headers", return_value={"test": "header"}):
        killer.trigger_synchronous()

        # 404 on verified exchange order ID confirms it's closed/filled
        assert cid not in mock_order_manager.active_orders


def test_kill_switch_fetch_resting_orders_sync_success():
    mock_om = MagicMock()
    killer = KillSwitch(mock_om)
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {"orders": [{"order_id": "k1", "client_order_id": "c1"}]}

    with patch("requests.get", return_value=mock_resp), \
         patch("execution.kill_switch.get_auth_headers", return_value={"test": "header"}):
        result = killer._fetch_resting_orders_sync()
        assert result == [{"order_id": "k1", "client_order_id": "c1"}]


def test_kill_switch_fetch_resting_orders_sync_paginates():
    mock_om = MagicMock()
    killer = KillSwitch(mock_om)

    resp_page1 = MagicMock(status_code=200)
    resp_page1.json.return_value = {
        "orders": [{"order_id": "k1", "client_order_id": "c1"}],
        "cursor": "cursor_token_page2"
    }
    resp_page2 = MagicMock(status_code=200)
    resp_page2.json.return_value = {
        "orders": [{"order_id": "k2", "client_order_id": "c2"}],
        "cursor": None
    }

    with patch("requests.get", side_effect=[resp_page1, resp_page2]) as mock_get, \
         patch("execution.kill_switch.get_auth_headers", return_value={"test": "header"}):
        result = killer._fetch_resting_orders_sync()

        assert len(result) == 2
        assert result == [
            {"order_id": "k1", "client_order_id": "c1"},
            {"order_id": "k2", "client_order_id": "c2"},
        ]
        assert mock_get.call_count == 2
        first_call_params = mock_get.call_args_list[0][1]["params"]
        second_call_params = mock_get.call_args_list[1][1]["params"]
        assert "cursor" not in first_call_params
        assert second_call_params["cursor"] == "cursor_token_page2"


def test_kill_switch_fetch_resting_orders_sync_subsequent_page_failure_returns_none():
    mock_om = MagicMock()
    killer = KillSwitch(mock_om)

    resp_page1 = MagicMock(status_code=200)
    resp_page1.json.return_value = {
        "orders": [{"order_id": "k1", "client_order_id": "c1"}],
        "cursor": "cursor_token_page2"
    }
    resp_page2 = MagicMock(status_code=500, text="Internal Server Error")

    with patch("requests.get", side_effect=[resp_page1, resp_page2]) as mock_get, \
         patch("execution.kill_switch.get_auth_headers", return_value={"test": "header"}):
        result = killer._fetch_resting_orders_sync()

        assert result is None
        assert mock_get.call_count == 2


def test_kill_switch_fetch_resting_orders_sync_stagnant_cursor_returns_none():
    mock_om = MagicMock()
    killer = KillSwitch(mock_om)

    resp_page1 = MagicMock(status_code=200)
    resp_page1.json.return_value = {
        "orders": [{"order_id": "k1", "client_order_id": "c1"}],
        "cursor": "token_stagnant"
    }
    resp_page2 = MagicMock(status_code=200)
    resp_page2.json.return_value = {
        "orders": [{"order_id": "k2", "client_order_id": "c2"}],
        "cursor": "token_stagnant"  # identical to requested cursor
    }

    with patch("requests.get", side_effect=[resp_page1, resp_page2]) as mock_get, \
         patch("execution.kill_switch.get_auth_headers", return_value={"test": "header"}):
        result = killer._fetch_resting_orders_sync()

        assert result is None
        assert mock_get.call_count == 2


def test_kill_switch_fetch_resting_orders_sync_cyclic_cursor_returns_none():
    mock_om = MagicMock()
    killer = KillSwitch(mock_om)

    resp_page1 = MagicMock(status_code=200)
    resp_page1.json.return_value = {
        "orders": [{"order_id": "k1", "client_order_id": "c1"}],
        "cursor": "cursor_a"
    }
    resp_page2 = MagicMock(status_code=200)
    resp_page2.json.return_value = {
        "orders": [{"order_id": "k2", "client_order_id": "c2"}],
        "cursor": "cursor_b"
    }
    resp_page3 = MagicMock(status_code=200)
    resp_page3.json.return_value = {
        "orders": [{"order_id": "k3", "client_order_id": "c3"}],
        "cursor": "cursor_a"  # cycle back to cursor_a
    }

    with patch("requests.get", side_effect=[resp_page1, resp_page2, resp_page3]) as mock_get, \
         patch("execution.kill_switch.get_auth_headers", return_value={"test": "header"}):
        result = killer._fetch_resting_orders_sync()

        assert result is None
        assert mock_get.call_count == 3


def test_kill_switch_fetch_resting_orders_sync_error():
    mock_om = MagicMock()
    killer = KillSwitch(mock_om)
    mock_resp = MagicMock(status_code=500, text="Internal Error")

    with patch("requests.get", return_value=mock_resp), \
         patch("execution.kill_switch.get_auth_headers", return_value={"test": "header"}):
        result = killer._fetch_resting_orders_sync()
        assert result is None


@pytest.mark.asyncio
async def test_kill_switch_async_delegates_to_order_manager(mock_order_manager):
    mock_order_manager.active_orders = {"cid-1": {}, "cid-2": {}}
    mock_order_manager.cancel_order = AsyncMock(return_value=True)

    killer = KillSwitch(mock_order_manager)
    with patch("execution.kill_switch.send_alert", new_callable=AsyncMock) as mock_alert:
        await killer.trigger()

        mock_alert.assert_awaited_once()
        assert mock_order_manager.cancel_order.await_count == 2
