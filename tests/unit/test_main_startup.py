"""
Unit tests for main.py startup market discovery and idle retry logic.
"""

import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from main import main


@pytest.mark.asyncio
async def test_main_startup_retries_discovery_until_market_found():
    """Verify main() idles and retries discovery when initial market discovery returns None."""
    discovery_calls = 0

    async def mock_discover(**kwargs):
        nonlocal discovery_calls
        discovery_calls += 1
        if discovery_calls == 1:
            return None  # First attempt: quiet market
        return "KXNFLGAME-26SEP17DETBUF"  # Second attempt: market found

    mock_bot = MagicMock()
    mock_bot.start = AsyncMock(return_value=None)
    mock_bot.stop = AsyncMock(return_value=None)
    mock_bot.om = MagicMock()

    with patch("utils.market_discovery.discover_active_market_async", side_effect=mock_discover), \
         patch("main.AvellanedaStoikovBot", return_value=mock_bot), \
         patch("main.KillSwitch"), \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:

        await main()

        assert discovery_calls == 2
        mock_sleep.assert_called_once_with(30)
        mock_bot.start.assert_called_once()
        mock_bot.stop.assert_called_once()


@pytest.mark.asyncio
async def test_main_signal_shutdown():
    """Verify SIGINT/SIGTERM triggers synchronous kill and records PnL snapshot."""
    import signal

    mock_bot = MagicMock()
    mock_bot.ticker = "KXNFLGAME-TEST"
    mock_bot.inv_manager.get_pnl_summary.return_value = {
        "realized_pnl_cents": 120.0,
        "unrealized_pnl_cents": 30.0,
        "total_fees_cents": 5.0,
        "rotation_session_id": "test-session-id"
    }
    mock_bot.inv_manager.get_position.return_value = 3
    mock_bot.om.record_pnl_snapshot = MagicMock()
    mock_bot.stop = AsyncMock()

    mock_killer = MagicMock()
    mock_killer.trigger_synchronous = MagicMock()

    signal_handlers = {}

    def mock_signal(sig, handler):
        signal_handlers[sig] = handler

    # When bot.start() is invoked, trigger the registered handle_shutdown handler
    async def mock_start():
        handler = signal_handlers.get(signal.SIGINT)
        assert handler is not None
        # Invoke handler and catch sys.exit
        with pytest.raises(SystemExit):
            handler(signal.SIGINT, None)

    mock_bot.start = AsyncMock(side_effect=mock_start)

    with patch("utils.market_discovery.discover_active_market_async", new_callable=AsyncMock, return_value="KXNFLGAME-TEST"), \
         patch("main.AvellanedaStoikovBot", return_value=mock_bot), \
         patch("main.KillSwitch", return_value=mock_killer), \
         patch("main.signal.signal", side_effect=mock_signal):
        
        await main()

    mock_killer.trigger_synchronous.assert_called_once()
    mock_bot.om.record_pnl_snapshot.assert_called_once_with(
        ticker="KXNFLGAME-TEST",
        realized_pnl_cents=120.0,
        unrealized_pnl_cents=30.0,
        total_fees_cents=5.0,
        inventory=3,
        rotation_session_id="test-session-id"
    )


@pytest.mark.asyncio
async def test_main_signal_shutdown_exception_handled():
    """Verify exception during shutdown PnL snapshot persistence is gracefully caught."""
    import signal

    mock_bot = MagicMock()
    mock_bot.ticker = "KXNFLGAME-TEST"
    mock_bot.inv_manager.get_pnl_summary.side_effect = Exception("DB snapshot error")
    mock_bot.stop = AsyncMock()

    mock_killer = MagicMock()

    signal_handlers = {}

    def mock_signal(sig, handler):
        signal_handlers[sig] = handler

    async def mock_start():
        handler = signal_handlers.get(signal.SIGTERM)
        assert handler is not None
        with pytest.raises(SystemExit):
            handler(signal.SIGTERM, None)

    mock_bot.start = AsyncMock(side_effect=mock_start)

    with patch("utils.market_discovery.discover_active_market_async", new_callable=AsyncMock, return_value="KXNFLGAME-TEST"), \
         patch("main.AvellanedaStoikovBot", return_value=mock_bot), \
         patch("main.KillSwitch", return_value=mock_killer), \
         patch("main.signal.signal", side_effect=mock_signal):
        
        await main()

    mock_killer.trigger_synchronous.assert_called_once()


@pytest.mark.asyncio
async def test_main_bot_crash_triggers_safety_and_stop():
    """Verify bot crash triggers killer.trigger() and bot.stop()."""
    mock_bot = MagicMock()
    mock_bot.start = AsyncMock(side_effect=RuntimeError("Unexpected MM loop crash"))
    mock_bot.stop = AsyncMock()
    mock_killer = MagicMock()
    mock_killer.trigger = AsyncMock()

    with patch("utils.market_discovery.discover_active_market_async", new_callable=AsyncMock, return_value="KXNFLGAME-TEST"), \
         patch("main.AvellanedaStoikovBot", return_value=mock_bot), \
         patch("main.KillSwitch", return_value=mock_killer):
        
        await main()

    mock_killer.trigger.assert_awaited_once()
    mock_bot.stop.assert_awaited_once()

