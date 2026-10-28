"""
Unit Tests for Expiration Cutoff Safeguards

Validates:
1. Expiration cutoff screens markets with remaining time <= MIN_TIME_TO_CLOSE_SECONDS (3600s).
2. Markets closing far beyond cutoff are retained as active.
3. Fail-closed screening treats unparseable or corrupt timestamps as closed.
4. Startup tick checks expiry and halts without quoting if initial market is expiring.
"""

import datetime
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from strategy.market_maker import AvellanedaStoikovBot
from utils.market_api import check_market_status, is_market_active


class TestExpirationCutoffSafeguard:
    """Verify time-to-expiration cutoff rules in market status checks."""

    def test_check_market_status_near_expiry_marked_closed(self):
        """Markets closing within MIN_TIME_TO_CLOSE_SECONDS (3600s) are marked 'closed'."""
        now = datetime.datetime.now(datetime.timezone.utc)
        near_expiry = (now + datetime.timedelta(minutes=30)).isoformat()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "market": {
                "ticker": "KXNFLGAME-EXPIRING-SOON",
                "status": "open",
                "close_time": near_expiry,
            }
        }
        with patch("requests.get", return_value=mock_resp):
            status = check_market_status("KXNFLGAME-EXPIRING-SOON")
            assert status == "closed"
            assert is_market_active("KXNFLGAME-EXPIRING-SOON") is False

    def test_check_market_status_safe_expiry_marked_active(self):
        """Markets closing far beyond MIN_TIME_TO_CLOSE_SECONDS are active."""
        now = datetime.datetime.now(datetime.timezone.utc)
        safe_expiry = (now + datetime.timedelta(hours=4)).isoformat()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "market": {
                "ticker": "KXNFLGAME-SAFE-EXPIRY",
                "status": "active",
                "close_time": safe_expiry,
            }
        }
        with patch("requests.get", return_value=mock_resp):
            status = check_market_status("KXNFLGAME-SAFE-EXPIRY")
            assert status == "active"
            assert is_market_active("KXNFLGAME-SAFE-EXPIRY") is True

    def test_check_market_status_malformed_close_time_treated_as_closed(self):
        """Markets with an unparseable close_time are treated as closed (fail closed)."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "market": {
                "ticker": "KXNFLGAME-CORRUPT-EXPIRY",
                "status": "active",
                "close_time": "corrupted-non-iso-timestamp",
            }
        }
        with patch("requests.get", return_value=mock_resp):
            status = check_market_status("KXNFLGAME-CORRUPT-EXPIRY")
            assert status == "closed"
            assert is_market_active("KXNFLGAME-CORRUPT-EXPIRY") is False

    def test_fetch_eligible_markets_skips_malformed_close_time(self):
        """fetch_eligible_markets drops markets whose close_time cannot be parsed."""
        from utils.market_api import fetch_eligible_markets
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "markets": [
                {
                    "ticker": "KXNFLGAME-CORRUPT-CLOSE",
                    "status": "active",
                    "close_time": "invalid-timestamp",
                },
                {
                    "ticker": "KXNFLGAME-VALID-CLOSE",
                    "status": "active",
                    "close_time": (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=2)).isoformat(),
                },
            ]
        }
        with patch("requests.get", return_value=mock_resp):
            eligible = fetch_eligible_markets()
            tickers = [m["ticker"] for m in eligible]
            assert "KXNFLGAME-CORRUPT-CLOSE" not in tickers
            assert "KXNFLGAME-VALID-CLOSE" in tickers

    @pytest.mark.asyncio
    async def test_startup_tick_checks_expiry_and_quiesces_without_autorotate(self):
        """On cold startup (tick 1), expiry is evaluated immediately even if auto_rotate=False."""
        bot = AvellanedaStoikovBot(
            ticker="KXNFLGAME-EXPIRING-COLD",
            gamma=0.5,
            min_spread=4,
            order_dollars=1.0,
            auto_rotate=False,
        )
        bot._cancel_all_quotes = AsyncMock()
        bot._update_quotes = AsyncMock()
        bot.inv_manager.get_balance = MagicMock(return_value=10000)
        bot.inv_manager.get_position = MagicMock(return_value=0)

        # Mock is_market_active_async returning False (due to expiry cutoff)
        with patch("strategy.market_maker.is_market_active_async", new=AsyncMock(return_value=False)):
            await bot._tick()

        # Bot must cancel quotes and set inactive immediately
        bot._cancel_all_quotes.assert_called_once()
        bot._update_quotes.assert_not_called()
        assert bot._market_inactive is True

    def test_expiration_buffer_minutes_doppler_env_override(self):
        """Validates that EXPIRATION_BUFFER_MINUTES translates to MIN_TIME_TO_CLOSE_SECONDS in seconds."""
        import importlib
        import os
        import config

        with patch.dict(os.environ, {"EXPIRATION_BUFFER_MINUTES": "45"}, clear=False):
            importlib.reload(config)
            assert config.EXPIRATION_BUFFER_MINUTES == 45
            assert config.MIN_TIME_TO_CLOSE_SECONDS == 2700

        # Clean reload back to environment state
        importlib.reload(config)

    def test_sport_specific_expiration_buffers_resolution(self):
        """Validates that get_expiration_buffer_seconds returns appropriate sport-specific cutoffs."""
        import importlib
        import os
        import config

        defaults = {
            "EXPIRATION_BUFFER_MINUTES_NBA": "15",
            "EXPIRATION_BUFFER_MINUTES_MLB": "30",
            "EXPIRATION_BUFFER_MINUTES_NFL": "40",
            "EXPIRATION_BUFFER_MINUTES_NCAAF": "45",
        }
        with patch.dict(os.environ, defaults):
            importlib.reload(config)
            from config import get_expiration_buffer_seconds

            # NBA: 15 minutes = 900 seconds
            assert get_expiration_buffer_seconds("KXNBAGAME-26OCT27BKNBOS-BKN") == 900
            assert get_expiration_buffer_seconds("KXNBA-TEST") == 900

            # MLB: 30 minutes = 1800 seconds
            assert get_expiration_buffer_seconds("KXMLBGAME-26OCT27NYYBAL-NYY") == 1800
            assert get_expiration_buffer_seconds("KXMLB-TEST") == 1800

            # NFL: 40 minutes = 2400 seconds
            assert get_expiration_buffer_seconds("KXNFLGAME-26OCT27KCBUF-KC") == 2400
            assert get_expiration_buffer_seconds("KXNFL-TEST") == 2400

            # College Football: 45 minutes = 2700 seconds
            assert get_expiration_buffer_seconds("KXCFBGAME-26OCT27USCSTAN-USC") == 2700
            assert get_expiration_buffer_seconds("KXCFB-TEST") == 2700
            assert get_expiration_buffer_seconds("KXNCAAFGAME-TEST") == 2700
            assert get_expiration_buffer_seconds("CUSTOM_CFB_TEST") == 2700
            assert get_expiration_buffer_seconds("CUSTOM_NCAAF_TEST") == 2700

            # Fallback / Default: 45 minutes = 2700 seconds
            assert get_expiration_buffer_seconds("UNKNOWN-TICKER") == 2700
            assert get_expiration_buffer_seconds(None) == 2700

        # Regression: When default fallback is set below 45m (e.g. 5m = 300s),
        # KXCFBGAME must still resolve to 45m (2700s) and not fall back to 300s.
        with patch.dict(os.environ, {"EXPIRATION_BUFFER_MINUTES": "5", **defaults}):
            importlib.reload(config)
            from config import get_expiration_buffer_seconds
            assert config.MIN_TIME_TO_CLOSE_SECONDS == 300
            assert get_expiration_buffer_seconds("KXCFBGAME-TEST") == 2700
            assert get_expiration_buffer_seconds("KXCFB-GAME") == 2700
            assert get_expiration_buffer_seconds("UNKNOWN-TICKER") == 300

        # Clean reload back to environment state
        importlib.reload(config)

    def test_nba_market_remains_active_at_20_minutes_to_close(self):
        """NBA market with 20 minutes remaining is active under the 15m NBA buffer."""
        now = datetime.datetime.now(datetime.timezone.utc)
        twenty_mins_left = (now + datetime.timedelta(minutes=20)).isoformat()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "market": {
                "ticker": "KXNBAGAME-26OCT27BKNBOS-BKN",
                "status": "active",
                "close_time": twenty_mins_left,
            }
        }
        with patch("requests.get", return_value=mock_resp):
            status = check_market_status("KXNBAGAME-26OCT27BKNBOS-BKN")
            assert status == "active"
            assert is_market_active("KXNBAGAME-26OCT27BKNBOS-BKN") is True

    def test_nfl_market_marked_closed_at_35_minutes_to_close(self):
        """NFL market with 35 minutes remaining is marked closed under the 40m NFL buffer."""
        now = datetime.datetime.now(datetime.timezone.utc)
        thirty_five_mins_left = (now + datetime.timedelta(minutes=35)).isoformat()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "market": {
                "ticker": "KXNFLGAME-26OCT27KCBUF-KC",
                "status": "active",
                "close_time": thirty_five_mins_left,
            }
        }
        with patch("requests.get", return_value=mock_resp):
            status = check_market_status("KXNFLGAME-26OCT27KCBUF-KC")
            assert status == "closed"
            assert is_market_active("KXNFLGAME-26OCT27KCBUF-KC") is False

    def test_sport_buffer_env_overrides(self):
        """Validates that environment variables override sport-specific expiration buffers."""
        import importlib
        import os
        import config

        with patch.dict(os.environ, {"EXPIRATION_BUFFER_MINUTES_NBA": "20"}, clear=False):
            importlib.reload(config)
            assert config.EXPIRATION_BUFFER_MINUTES_NBA == 20
            assert config.get_expiration_buffer_seconds("KXNBAGAME-TEST") == 1200

        # Clean reload back to environment state
        importlib.reload(config)


