"""
Alerting Utility

This module manages error notifications. It broadcasts high-priority alerts to 
Slack/Discord webhook URLs in case of connection dropouts, critical errors, 
or safety triggers. It falls back to standard log files if no webhook is set.
"""

import asyncio
import logging
import json


from config import ALERT_WEBHOOK_URL

logger = logging.getLogger("Alerting")

async def send_alert(message: str):
    """
    Sends an asynchronous alert to a configured webhook URL (e.g., Slack or Discord).
    If no URL is configured, it falls back to standard logging.
    """
    if not ALERT_WEBHOOK_URL:
        # Fallback if no webhook is configured
        logger.warning(f"ALERT (Local Only - No Webhook Configured): {message}")
        return

    # Slack expects a JSON payload with a "text" field
    payload = {
        "text": f"*Kalshi Bot Alert* \n{message}"
    }

    try:
        import requests # using requests in a thread to keep it simple, or aiohttp if available
        await asyncio.to_thread(
            requests.post,
            ALERT_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=5
        )
        logger.info("Webhook alert sent successfully.")
    except Exception as e:
        logger.error(f"Failed to send webhook alert: {e}")
