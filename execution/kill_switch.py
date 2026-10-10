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

    def _fetch_resting_orders_sync(self):
        """Fetch all resting orders across the portfolio synchronously for emergency reconciliation."""
        sign_path = "/trade-api/v2/portfolio/orders"
        cursor = None
        orders_list = []
        seen_cursors = set()
        try:
            while True:
                headers = get_auth_headers(method="GET", sign_path=sign_path)
                params = {"status": "resting", "limit": 100}
                if cursor:
                    params["cursor"] = cursor
                resp = requests.get(
                    BASE_URL + sign_path,
                    headers=headers,
                    params=params,
                    timeout=5,
                    verify=certifi.where()
                )
                if resp.status_code != 200:
                    logger.error(
                        f"Failed to fetch resting orders for sync reconciliation: {resp.status_code} - {resp.text}"
                    )
                    return None
                data = resp.json()
                page_orders = data.get("orders", [])
                orders_list.extend(page_orders)
                next_cursor = data.get("cursor")
                if not next_cursor:
                    break
                if next_cursor == cursor or next_cursor in seen_cursors:
                    logger.error("Resting order pagination cursor did not advance; aborting reconciliation.")
                    return None
                if cursor:
                    seen_cursors.add(cursor)
                cursor = next_cursor
            return orders_list
        except Exception as e:
            logger.error(f"Exception fetching resting orders for sync reconciliation: {e}")
            return None

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

        # Check if any active orders are missing the exchange-assigned kalshi_order_id
        unverified_ids = [
            cid for cid in active_ids 
            if not (self.om.active_orders.get(cid) or {}).get("kalshi_order_id")
        ]

        # If any order is unverified, reconcile against exchange resting orders first
        client_to_exchange = {}
        recon_succeeded = False
        if unverified_ids:
            resting_orders = self._fetch_resting_orders_sync()
            if resting_orders is not None:
                recon_succeeded = True
                client_to_exchange = {
                    o.get("client_order_id"): o.get("order_id")
                    for o in resting_orders
                    if o.get("client_order_id") and o.get("order_id")
                }
            
        for client_order_id in active_ids:
            order_info = self.om.active_orders.get(client_order_id)
            kalshi_order_id = order_info.get("kalshi_order_id") if order_info else None
            
            if not kalshi_order_id:
                if recon_succeeded:
                    if client_order_id in client_to_exchange:
                        kalshi_order_id = client_to_exchange[client_order_id]
                        if order_info:
                            order_info["kalshi_order_id"] = kalshi_order_id
                    else:
                        # Exchange confirms this client order is NOT in resting orders
                        logger.info(f"Order {client_order_id} verified not resting on exchange.")
                        self.om.active_orders.pop(client_order_id, None)
                        continue
                else:
                    logger.error(
                        f"Cannot safely cancel unverified order {client_order_id}: "
                        "missing exchange order ID and resting order reconciliation failed."
                    )
                    continue

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
                    # Verified Kalshi ID returned 404: confirmed closed/filled on exchange
                    logger.info(f"Order {client_order_id} ({kalshi_order_id}) already closed/filled on exchange.")
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
