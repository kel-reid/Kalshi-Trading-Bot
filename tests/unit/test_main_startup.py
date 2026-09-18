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
    mock_bot.om = MagicMock()

    with patch("utils.market_discovery.discover_active_market_async", side_effect=mock_discover), \
         patch("main.AvellanedaStoikovBot", return_value=mock_bot), \
         patch("main.KillSwitch"), \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:

        await main()

        assert discovery_calls == 2
        mock_sleep.assert_called_once_with(30)
        mock_bot.start.assert_called_once()
