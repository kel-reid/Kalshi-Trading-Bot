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
    """Verify SIGINT/SIGTERM triggers synchronous kill and delegates clean shutdown to bot.stop()."""
    import signal

    mock_bot = MagicMock()
    mock_bot.ticker = "KXNFLGAME-TEST"
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
        handler(signal.SIGINT, None)
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass

    mock_bot.start = AsyncMock(side_effect=mock_start)

    with patch("utils.market_discovery.discover_active_market_async", new_callable=AsyncMock, return_value="KXNFLGAME-TEST"), \
         patch("main.AvellanedaStoikovBot", return_value=mock_bot), \
         patch("main.KillSwitch", return_value=mock_killer), \
         patch("main.signal.signal", side_effect=mock_signal):
        
        await main()

    mock_killer.trigger_synchronous.assert_called_once()
    mock_bot.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_sigterm_shutdown():
    """Verify SIGTERM triggers synchronous kill and bot.stop()."""
    import signal

    mock_bot = MagicMock()
    mock_bot.ticker = "KXNFLGAME-TEST"
    mock_bot.stop = AsyncMock()

    mock_killer = MagicMock()
    mock_killer.trigger_synchronous = MagicMock()

    signal_handlers = {}

    def mock_signal(sig, handler):
        signal_handlers[sig] = handler

    async def mock_start():
        handler = signal_handlers.get(signal.SIGTERM)
        assert handler is not None
        handler(signal.SIGTERM, None)
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass

    mock_bot.start = AsyncMock(side_effect=mock_start)

    with patch("utils.market_discovery.discover_active_market_async", new_callable=AsyncMock, return_value="KXNFLGAME-TEST"), \
         patch("main.AvellanedaStoikovBot", return_value=mock_bot), \
         patch("main.KillSwitch", return_value=mock_killer), \
         patch("main.signal.signal", side_effect=mock_signal):
        
        await main()

    mock_killer.trigger_synchronous.assert_called_once()
    mock_bot.stop.assert_awaited_once()


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


@pytest.mark.asyncio
async def test_main_startup_shutdown_signal_during_discovery():
    """Verify signal during discovery aborts startup cleanly without initializing bot."""
    import signal

    signal_handlers = {}

    def mock_signal(sig, handler):
        signal_handlers[sig] = handler

    async def mock_discover(**kwargs):
        # Fire signal during discovery
        handler = signal_handlers.get(signal.SIGINT)
        assert handler is not None
        handler(signal.SIGINT, None)
        return None

    mock_bot_cls = MagicMock()

    with patch("utils.market_discovery.discover_active_market_async", side_effect=mock_discover), \
         patch("main.AvellanedaStoikovBot", mock_bot_cls), \
         patch("main.signal.signal", side_effect=mock_signal):
        await main()

    # Bot should never be instantiated or started
    mock_bot_cls.assert_not_called()


@pytest.mark.asyncio
async def test_main_signal_idempotent():
    """Verify multiple signals are handled idempotently without multiple kill triggers."""
    import signal

    mock_bot = MagicMock()
    mock_bot.ticker = "KXNFLGAME-TEST"
    mock_bot.stop = AsyncMock()

    mock_killer = MagicMock()
    mock_killer.trigger_synchronous = MagicMock()

    signal_handlers = {}

    def mock_signal(sig, handler):
        signal_handlers[sig] = handler

    async def mock_start():
        handler = signal_handlers.get(signal.SIGINT)
        assert handler is not None
        # Fire signal twice
        handler(signal.SIGINT, None)
        handler(signal.SIGINT, None)
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass

    mock_bot.start = AsyncMock(side_effect=mock_start)

    with patch("utils.market_discovery.discover_active_market_async", new_callable=AsyncMock, return_value="KXNFLGAME-TEST"), \
         patch("main.AvellanedaStoikovBot", return_value=mock_bot), \
         patch("main.KillSwitch", return_value=mock_killer), \
         patch("main.signal.signal", side_effect=mock_signal):
        await main()

    # Synchronous kill triggered exactly once despite double signal
    mock_killer.trigger_synchronous.assert_called_once()
    mock_bot.stop.assert_awaited_once()

