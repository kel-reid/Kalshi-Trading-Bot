"""
Alerting Utility

This module manages error notifications. It broadcasts high-priority alerts to 
Slack/Discord webhook URLs in case of connection dropouts, critical errors, 
or safety triggers. It falls back to standard log files if no webhook is set.
"""

import asyncio
import logging
import json
import time
import re
from typing import Dict, Tuple, Optional

from config import ALERT_WEBHOOK_URL

logger = logging.getLogger("Alerting")

DEFAULT_ALERT_COOLDOWN_SECONDS: float = 300.0

# In-memory map: alert_key -> (last_sent_timestamp, suppressed_count)
_alert_history: Dict[str, Tuple[float, int]] = {}


def reset_alert_history() -> None:
    """Clear in-memory alert history. Primarily used in unit tests."""
    global _alert_history
    _alert_history.clear()


def _normalize_alert_key(message: str) -> str:
    """
    Generate a deduplication key by masking dynamic numeric quantities,
    timestamps, and identifiers while preserving the core error category, ticker, and context.
    """
    # Replace position/count/inventory variations (e.g. position=11.68, position 11.68, inventory: 11.68)
    key = re.sub(r"(?:position|inventory|contracts?)[=:\s]+-?\d+(?:\.\d+)?", "position=<N>", message, flags=re.IGNORECASE)
    # Replace floating/integer contract numbers before "contracts"
    key = re.sub(r"-?\d+(?:\.\d+)?\s+contracts?", "<N> contracts", key, flags=re.IGNORECASE)
    # Replace UUID-like identifiers
    key = re.sub(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", "<ID>", key)
    # Replace elapsed/remaining seconds e.g. "in 300s", "12.3s"
    key = re.sub(r"\d+(?:\.\d+)?s\b", "<SEC>s", key)
    return key.strip()


async def send_alert(
    message: str,
    cooldown_seconds: Optional[float] = DEFAULT_ALERT_COOLDOWN_SECONDS,
    bypass_throttle: bool = False,
):
    """
    Sends an asynchronous alert to a configured webhook URL (e.g., Slack or Discord).
    If no URL is configured, it falls back to standard logging.

    Applies in-memory throttling to suppress repeated identical alerts within cooldown_seconds.
    When a suppressed alert is finally allowed to fire after cooldown, it summarizes the number
    of suppressed notifications.
    """
    now = time.time()
    key = _normalize_alert_key(message)
    suppressed_count = 0
    throttle_active = (not bypass_throttle) and (cooldown_seconds is not None) and (cooldown_seconds > 0)

    if throttle_active:
        if key in _alert_history:
            last_sent, count = _alert_history[key]
            elapsed = now - last_sent
            if elapsed < cooldown_seconds:
                _alert_history[key] = (last_sent, count + 1)
                logger.debug(
                    f"Alert throttled (key='{key}', suppressed {count + 1} times, {elapsed:.1f}s / {cooldown_seconds}s): {message}"
                )
                return
            else:
                suppressed_count = count

    # Prepend summary if repeating notifications were suppressed during the cooldown window
    delivered_message = message
    if suppressed_count > 0:
        delivered_message = (
            f"[Suppressed {suppressed_count} identical alerts in last {int(cooldown_seconds or 0)}s]\n{message}"
        )

    if not ALERT_WEBHOOK_URL:
        # Fallback if no webhook is configured (local logging)
        logger.warning(f"ALERT (Local Only - No Webhook Configured): {delivered_message}")
        if throttle_active:
            _alert_history[key] = (now, 0)
        return

    # Slack expects a JSON payload with a "text" field
    payload = {
        "text": f"*Kalshi Bot Alert* \n{delivered_message}"
    }

    try:
        import requests
        resp = await asyncio.to_thread(
            requests.post,
            ALERT_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=5
        )
        if resp.status_code < 400:
            if throttle_active:
                _alert_history[key] = (now, 0)
            logger.info(f"Webhook alert sent successfully (key='{key}').")
        else:
            logger.error(f"Failed to send webhook alert (HTTP {resp.status_code}): {resp.text}")
    except Exception as e:
        logger.error(f"Failed to send webhook alert: {e}")

