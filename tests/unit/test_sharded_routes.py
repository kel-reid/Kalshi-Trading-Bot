"""
Unit tests for Exchange Shard Routing, Safe Order Cancellation,
Intra-Exchange Collateral Management, and Hedging Circuit Breakers.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from utils.market_api import get_market_exchange_index
from execution.order_manager import OrderManager
from execution.kill_switch import KillSwitch
from strategy.market_maker import AvellanedaStoikovBot


class MockResponse:
    def __init__(self, json_data, status_code=200):
        self.json_data = json_data
        self.status_code = status_code
        self.text = str(json_data)

    def json(self):
        return self.json_data


# ---------------------------------------------------------------------------
# 1. Market Shard Resolution
# ---------------------------------------------------------------------------
def test_get_market_exchange_index_success():
    with patch("utils.market_api.get_market_details", return_value={"exchange_index": 3, "ticker": "NBA-GAME-1"}):
        idx = get_market_exchange_index("NBA-GAME-1")
        assert idx == 3


def test_get_market_exchange_index_fallback_on_missing_or_error():
    with patch("utils.market_api.get_market_details", return_value={"ticker": "NFL-GAME-1"}):
        assert get_market_exchange_index("NFL-GAME-1") == 0

    with patch("utils.market_api.get_market_details", return_value=None):
        assert get_market_exchange_index("FAIL-TICKER") is None

    with patch("utils.market_api.get_market_details", side_effect=Exception("API Error")):
        assert get_market_exchange_index("ERR-TICKER") is None


def test_order_manager_get_ticker_exchange_index_does_not_cache_none():
    om = OrderManager()
    with patch("utils.market_api.get_market_details", return_value=None):
        idx = om.get_ticker_exchange_index("NBA-FAIL")
        assert idx is None
        assert "NBA-FAIL" not in om._ticker_exchange_indices

    # Subsequent recovery caches the verified shard
    with patch("utils.market_api.get_market_details", return_value={"exchange_index": 3}):
        idx = om.get_ticker_exchange_index("NBA-FAIL")
        assert idx == 3
        assert om._ticker_exchange_indices["NBA-FAIL"] == 3


# ---------------------------------------------------------------------------
# 2. OrderManager place_order with exchange_index
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_order_manager_place_order_sharded_payload():
    om = OrderManager()
    captured_payload = {}

    def fake_post(sign_path, payload):
        nonlocal captured_payload
        captured_payload = payload
        return MockResponse({
            "order": {
                "order_id": "kalshi-ord-sharded-123",
                "status": "resting",
                "exchange_index": 3,
                "ticker": "KXNBAGAME-TEST",
            }
        }, 201)

    om._post_request = fake_post

    order_id = await om.place_order(
        ticker="KXNBAGAME-TEST",
        side="yes",
        action="buy",
        count=5,
        price=45,
        exchange_index=3,
    )

    assert order_id is not None
    assert captured_payload.get("exchange_index") == 3
    assert captured_payload.get("ticker") == "KXNBAGAME-TEST"
    assert order_id in om.active_orders
    assert om.active_orders[order_id]["exchange_index"] == 3
    assert om.active_orders[order_id]["kalshi_order_id"] == "kalshi-ord-sharded-123"


# ---------------------------------------------------------------------------
# 3. OrderManager cancel_order with Shard Params and Zero-Trust 404
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_order_manager_cancel_order_sends_shard_query_params():
    om = OrderManager()
    om.active_orders["client-1"] = {
        "kalshi_order_id": "kalshi-123",
        "ticker": "KXNBAGAME-TEST",
        "exchange_index": 3,
        "side": "yes",
        "action": "buy",
        "count": 5,
        "price": 45,
    }

    captured_delete_params = {}
    def fake_delete(sign_path, params=None):
        nonlocal captured_delete_params
        captured_delete_params = params
        return MockResponse({"order": {"status": "canceled"}}, 200)

    om._delete_request = fake_delete

    success = await om.cancel_order("client-1")
    assert success is True
    assert "client-1" not in om.active_orders
    assert captured_delete_params == {"exchange_index": 3, "ticker": "KXNBAGAME-TEST"}


@pytest.mark.asyncio
async def test_order_manager_cancel_order_404_retains_tracking_if_still_resting():
    """
    If DELETE returns 404 but get_order_status confirms the order is still RESTING
    (e.g. wrong shard or API anomaly), order tracking must NOT be cleared.
    """
    om = OrderManager()
    om.active_orders["client-1"] = {
        "kalshi_order_id": "kalshi-123",
        "ticker": "KXNBAGAME-TEST",
        "exchange_index": 0,  # Misspecified shard
    }

    # DELETE returns 404
    om._delete_request = MagicMock(return_value=MockResponse({"error": "not found"}, 404))
    # get_order_status confirms it is still resting on exchange!
    om.get_order_status = AsyncMock(return_value={
        "order_id": "kalshi-123",
        "status": "resting",
        "remaining_count": 5,
    })

    success = await om.cancel_order("client-1")
    assert success is False
    # CRITICAL INVARIANT: order tracking retained
    assert "client-1" in om.active_orders


@pytest.mark.asyncio
async def test_order_manager_cancel_order_404_clears_tracking_if_confirmed_closed():
    """
    If DELETE returns 404 and get_order_status confirms the order is executed/canceled,
    order tracking should be popped.
    """
    om = OrderManager()
    om.active_orders["client-1"] = {
        "kalshi_order_id": "kalshi-123",
        "ticker": "KXNBAGAME-TEST",
        "exchange_index": 3,
    }

    om._delete_request = MagicMock(return_value=MockResponse({"error": "not found"}, 404))
    om.get_order_status = AsyncMock(return_value={
        "order_id": "kalshi-123",
        "status": "executed",
        "remaining_count": 0,
    })

    success = await om.cancel_order("client-1")
    assert success is True
    assert "client-1" not in om.active_orders


# ---------------------------------------------------------------------------
# 4. Intra-Exchange Balance and Transfer
# ---------------------------------------------------------------------------
def test_order_manager_get_portfolio_balances_parsing():
    om = OrderManager()
    mock_resp = MockResponse({
        "balance": 3446,
        "balance_dollars": 34.46,
        "balance_breakdown": [
            {"exchange_index": 0, "balance": 29.45},
            {"exchange_index": 1, "balance": 0.0},
            {"exchange_index": 2, "balance": 0.0},
            {"exchange_index": 3, "balance": 5.01},
        ]
    }, 200)

    with patch("requests.get", return_value=mock_resp):
        res = om.get_portfolio_balances()
        assert res["total_balance_dollars"] == 34.46
        assert res["shard_balances"][0] == 29.45
        assert res["shard_balances"][3] == 5.01


def test_order_manager_transfer_intra_exchange_execution():
    om = OrderManager()
    captured_payload = {}

    def fake_post(sign_path, payload):
        nonlocal captured_payload
        captured_payload = payload
        return MockResponse({"status": "complete"}, 201)

    om._post_request = fake_post

    success = om.transfer_intra_exchange(
        source_shard=0,
        destination_shard=3,
        amount_dollars=15.0,
    )

    assert success is True
    assert captured_payload["amount"] == 1500  # Cents integer
    assert captured_payload["source_exchange_shard"] == 0
    assert captured_payload["destination_exchange_shard"] == 3
    assert "client_transfer_id" in captured_payload


# ---------------------------------------------------------------------------
# 5. MarketMaker ensure_shard_collateral
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_market_maker_ensure_shard_collateral_shard0_noop():
    with patch("strategy.market_maker.get_market_exchange_index", return_value=0):
        bot = AvellanedaStoikovBot("NFL-GAME-1")
        assert bot.exchange_index == 0
        # Shard 0 requires no intra-exchange rebalance
        res = await bot.ensure_shard_collateral(15.0)
        assert res is True


@pytest.mark.asyncio
async def test_market_maker_ensure_shard_collateral_shard3_rebalance_trigger():
    with patch("strategy.market_maker.get_market_exchange_index", return_value=3):
        bot = AvellanedaStoikovBot("NBA-GAME-1")
        assert bot.exchange_index == 3

        # Shard 3 has $5.00, Shard 0 has $25.00 -> needs $10.00 transfer
        bot.om.get_portfolio_balances = MagicMock(return_value={
            "total_balance_dollars": 30.0,
            "shard_balances": {0: 25.0, 3: 5.0}
        })
        bot.om.transfer_intra_exchange = MagicMock(return_value=True)

        res = await bot.ensure_shard_collateral(min_required_dollars=15.0)
        assert res is True
        bot.om.transfer_intra_exchange.assert_called_once_with(
            source_shard=0,
            destination_shard=3,
            amount_dollars=10.0,
        )


# ---------------------------------------------------------------------------
# 6. Stop-Loss Hedge Circuit Breaker
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_stop_loss_hedge_failure_trips_kill_switch():
    with patch("strategy.market_maker.get_market_exchange_index", return_value=3):
        bot = AvellanedaStoikovBot("NBA-GAME-1")
        bot.om.place_order = AsyncMock(return_value=None)  # Simulates order rejection/failure
        bot.ensure_shard_collateral = AsyncMock(return_value=False)

        kill_switch_mock = MagicMock()
        kill_switch_mock.trigger_synchronous = MagicMock()

        with patch("execution.kill_switch.KillSwitch", return_value=kill_switch_mock):
            with patch("strategy.market_maker.send_alert", new_callable=AsyncMock) as alert_mock:
                # 1st failure (selling YES to hedge long inventory)
                await bot._update_quotes(new_bid=None, new_ask=45)
                assert bot._consecutive_hedge_failures == 1
                assert bot._market_inactive is False

                # 2nd failure
                await bot._update_quotes(new_bid=None, new_ask=45)
                assert bot._consecutive_hedge_failures == 2
                assert bot._market_inactive is False

                # 3rd failure: must trip kill switch and alert!
                await bot._update_quotes(new_bid=None, new_ask=45)
                assert bot._consecutive_hedge_failures == 3
                assert bot._market_inactive is True
                kill_switch_mock.trigger_synchronous.assert_called_once_with(ticker="NBA-GAME-1")
                alert_mock.assert_called_once()
                assert "CRITICAL" in alert_mock.call_args[0][0]


@pytest.mark.asyncio
async def test_stop_loss_hedge_success_resets_failure_counter():
    with patch("strategy.market_maker.get_market_exchange_index", return_value=3):
        bot = AvellanedaStoikovBot("NBA-GAME-1")
        bot._consecutive_hedge_failures = 2

        bot.om.place_order = AsyncMock(return_value="order-success-123")
        await bot._update_quotes(new_bid=None, new_ask=45)

        assert bot._consecutive_hedge_failures == 0
        assert bot.current_ask_id == "order-success-123"


def test_kill_switch_sync_retries_failed_deletion_in_step_4():
    om = MagicMock()
    om.active_orders = {
        "cid-1": {"kalshi_order_id": "kid-1", "ticker": "NBA-1", "exchange_index": 3}
    }
    killer = KillSwitch(om, ticker="NBA-1")

    # Step 1 fails on kid-1 (returns False), Step 4 retries and succeeds (returns True)
    delete_calls = []
    def fake_delete(kalshi_id, client_id=None, exchange_index=None, ticker=None):
        delete_calls.append(kalshi_id)
        if len(delete_calls) == 1:
            return False  # Failed on initial attempt
        return True  # Succeeded on sweep retry

    killer._delete_order_sync = MagicMock(side_effect=fake_delete)
    killer._fetch_resting_orders_sync = MagicMock(return_value=[
        {"order_id": "kid-1", "client_order_id": "cid-1", "ticker": "NBA-1", "exchange_index": 3}
    ])

    killer.trigger_synchronous(ticker="NBA-1")

    # kid-1 must be called twice: first in step 1, then retried in step 4 because step 1 failed
    assert killer._delete_order_sync.call_count == 2
    assert delete_calls == ["kid-1", "kid-1"]


@pytest.mark.asyncio
async def test_market_maker_hedge_failure_uses_dynamic_collateral_floor():
    with patch("strategy.market_maker.get_market_exchange_index", return_value=3):
        bot = AvellanedaStoikovBot("NBA-GAME-1")
        bot.om.place_order = AsyncMock(return_value=None)
        bot.ensure_shard_collateral = AsyncMock(return_value=False)

        # 50 contracts @ 60c = $30.00 notional. Floor should be $30.00 + $5.00 = $35.00
        bot._current_quote_size = 50
        await bot._update_quotes(new_bid=None, new_ask=60)

        bot.ensure_shard_collateral.assert_awaited_once_with(min_required_dollars=35.0)
