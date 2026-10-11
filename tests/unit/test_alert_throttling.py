"""
Unit tests for alert throttling, deduplication, and cooldown behavior in utils/alerting.py.
"""

import pytest
import time
from unittest.mock import patch, MagicMock, AsyncMock

from utils.alerting import (
    send_alert,
    reset_alert_history,
    _normalize_alert_key,
    DEFAULT_ALERT_COOLDOWN_SECONDS,
)


@pytest.fixture(autouse=True)
def clean_alert_history():
    reset_alert_history()
    yield
    reset_alert_history()


def test_normalize_alert_key():
    """Verify that _normalize_alert_key masks dynamic values while preserving core errors."""
    msg1 = "⚠️ Inactive market liquidation retry failed for KXTEST (position=11.68)."
    msg2 = "⚠️ Inactive market liquidation retry failed for KXTEST (position=5.0)."
    assert _normalize_alert_key(msg1) == _normalize_alert_key(msg2)

    uuid_msg1 = "Order 12345678-abcd-1234-abcd-123456789abc failed on KXTEST"
    uuid_msg2 = "Order 87654321-dcba-4321-dcba-cba987654321 failed on KXTEST"
    assert _normalize_alert_key(uuid_msg1) == _normalize_alert_key(uuid_msg2)

    diff_msg = "⚠️ Safeguard liquidation failed for DIFFERENT_TICKER"
    assert _normalize_alert_key(msg1) != _normalize_alert_key(diff_msg)


@pytest.mark.asyncio
async def test_alert_sent_and_cooldown_throttled():
    """Verify first alert is sent, identical alert within cooldown is throttled, and summary is sent after cooldown."""
    with patch("utils.alerting.ALERT_WEBHOOK_URL", "https://hooks.slack.com/services/TEST/123"), \
         patch("requests.post") as mock_post:

        # 1. First alert should be sent
        await send_alert("⚠️ Market liquidation failed for KXTEST (position=10).", cooldown_seconds=300.0)
        assert mock_post.call_count == 1
        payload = mock_post.call_args[1]["json"]
        assert "Market liquidation failed for KXTEST" in payload["text"]
        assert "[Suppressed" not in payload["text"]

        # 2. Second alert within cooldown with varying position quantity should be throttled
        await send_alert("⚠️ Market liquidation failed for KXTEST (position=10.5).", cooldown_seconds=300.0)
        assert mock_post.call_count == 1  # Not sent again

        # 3. Third alert with different category should fire immediately
        await send_alert("⚠️ Different alert for KXTEST.", cooldown_seconds=300.0)
        assert mock_post.call_count == 2

        # 4. Advance time past cooldown (301 seconds later)
        with patch("time.time", return_value=time.time() + 305):
            await send_alert("⚠️ Market liquidation failed for KXTEST (position=9.0).", cooldown_seconds=300.0)
            assert mock_post.call_count == 3
            latest_payload = mock_post.call_args[1]["json"]
            assert "[Suppressed 1 identical alerts in last 300s]" in latest_payload["text"]


@pytest.mark.asyncio
async def test_bypass_throttle_fires_always():
    """Verify that bypass_throttle=True ignores cooldown and always dispatches."""
    with patch("utils.alerting.ALERT_WEBHOOK_URL", "https://hooks.slack.com/services/TEST/123"), \
         patch("requests.post") as mock_post:

        await send_alert("Kill Switch Triggered", cooldown_seconds=300.0, bypass_throttle=True)
        assert mock_post.call_count == 1

        await send_alert("Kill Switch Triggered", cooldown_seconds=300.0, bypass_throttle=True)
        assert mock_post.call_count == 2


@pytest.mark.asyncio
async def test_alert_no_webhook_configured(caplog):
    """Verify that without webhook URL, alert falls back to logger warning."""
    with patch("utils.alerting.ALERT_WEBHOOK_URL", ""):
        await send_alert("Test message without webhook")
        assert any("ALERT (Local Only - No Webhook Configured)" in r.message for r in caplog.records)
