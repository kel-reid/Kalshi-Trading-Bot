"""
Emergency Safety Kill Switch

This module provides emergency procedures to immediately cancel all resting orders 
and cease market making. It supports both asynchronous cancellation in the event 
of software crashes and synchronous cancellation for manual developer triggers (like Ctrl+C).
"""

import logging
import asyncio
import requests
import certifi

from config import BASE_URL
from auth.kalshi_auth import get_auth_headers
from execution.order_manager import OrderManager
from utils.alerting import send_alert

logger = logging.getLogger("KillSwitch")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)


class KillSwitch:
    """
    A unified kill switch layer for the market making application.
    Capable of cancelling known active orders instantly to drop exposure.
    """
    def __init__(self, order_manager: OrderManager):
        self.om = order_manager

    def trigger_synchronous(self):
        """Immediately cancel all known local orders synchronously."""
        logger.warning("Kill Switch Triggered (Sync). Canceling all local orders...")
        # Since this is synchronous and we are about to exit, we must block to send the alert
        from config import ALERT_WEBHOOK_URL
        if ALERT_WEBHOOK_URL:
            try:
                payload = {"text": "*Kalshi Bot Alert* \nKill Switch Triggered (Synchronous). Withdrawing all quotes."}
                requests.post(
                    ALERT_WEBHOOK_URL,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=2
                )
            except Exception as e:
                logger.error(f"Failed to send sync webhook alert: {e}")

        active_ids = list(self.om.active_orders.keys())
        
        if not active_ids:
            logger.info("No active local orders to kill.")
            return
            
        for client_order_id in active_ids:
            order_info = self.om.active_orders.get(client_order_id)
            kalshi_order_id = order_info.get("kalshi_order_id") if order_info else None
            
            if not kalshi_order_id:
                # Fallback just in case Kalshi order ID wasn't populated yet
                kalshi_order_id = client_order_id

            logger.warning(f"Canceling {client_order_id} (Kalshi ID: {kalshi_order_id})...")
            # Fire an emergency blocking cancel to Kalshi using the raw request wrapper
            sign_path = f"/trade-api/v2/portfolio/events/orders/{kalshi_order_id}"
            try:
                headers = get_auth_headers(method="DELETE", sign_path=sign_path)
                resp = requests.delete(
                    BASE_URL + sign_path, 
                    headers=headers, 
                    timeout=5, 
                    verify=certifi.where()
                )
                if resp.status_code in [200, 204]:
                    logger.info(f"Successfully killed {client_order_id}")
                    self.om.active_orders.pop(client_order_id, None)
                elif resp.status_code == 404:
                    logger.info(f"Order {client_order_id} already closed/filled.")
                    self.om.active_orders.pop(client_order_id, None)
                else:
                    logger.error(f"Failed to kill {client_order_id}: {resp.status_code} - {resp.text}")
            except Exception as e:
                import traceback
                logger.error(f"Exception while killing {client_order_id}: {e}\n{traceback.format_exc()}")

    async def trigger(self):
        """Asynchronously triggers the kill switch using the core order manager."""
        logger.warning("Kill Switch Triggered (Async). Canceling all local orders...")
        await send_alert("Kill Switch Triggered (Asynchronous). Withdrawing all quotes.")
        
        active_ids = list(self.om.active_orders.keys())
        
        if not active_ids:
            logger.info("No active local orders to kill.")
            return

        tasks = []
        for order_id in active_ids:
            tasks.append(self.om.cancel_order(order_id))
            
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for order_id, res in zip(active_ids, results):
            if isinstance(res, Exception):
                logger.error(f"Exception encountered attempting to kill {order_id}: {res}")
            elif not res:
                logger.error(f"Failed to kill {order_id} (API rejected)")
            else:
                logger.info(f"Successfully completed cancellation of {order_id}")
