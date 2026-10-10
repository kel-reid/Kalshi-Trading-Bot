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
    def __init__(self, order_manager: OrderManager, ticker: Optional[str] = None):
        self.om = order_manager
        self.ticker = ticker

    def _fetch_resting_orders_sync(self, ticker: Optional[str] = None):
        """Fetch all resting orders using the order manager's consolidated pagination method."""
        if hasattr(self.om, "fetch_resting_orders_sync"):
            res = self.om.fetch_resting_orders_sync(ticker=ticker)
            if isinstance(res, list):
                return res
            if res is None:
                return None
            return []
        return []

    def trigger_synchronous(self, ticker: Optional[str] = None):
        """Immediately cancel all known local and exchange resting orders synchronously."""
        logger.warning("Kill Switch Triggered (Sync). Canceling all resting orders...")
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

        target_ticker = ticker or self.ticker
        exchange_orders = self._fetch_resting_orders_sync(ticker=target_ticker)
        if exchange_orders is not None and target_ticker:
            exchange_orders = [o for o in exchange_orders if o.get("ticker") == target_ticker]

        client_to_exchange = {}
        if exchange_orders is not None:
            client_to_exchange = {
                o.get("client_order_id"): o.get("order_id")
                for o in exchange_orders
                if o.get("client_order_id") and o.get("order_id")
            }

        orders_to_cancel = {}  # kalshi_order_id -> client_order_id
        active_ids = list(self.om.active_orders.keys())

        for client_order_id in active_ids:
            order_info = self.om.active_orders.get(client_order_id) or {}
            if target_ticker and order_info.get("ticker") and order_info.get("ticker") != target_ticker:
                continue

            kalshi_order_id = order_info.get("kalshi_order_id")
            if not kalshi_order_id:
                if exchange_orders is not None:
                    if client_order_id in client_to_exchange:
                        kalshi_order_id = client_to_exchange[client_order_id]
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

            if kalshi_order_id:
                orders_to_cancel[kalshi_order_id] = client_order_id

        # Union: Add untracked exchange resting orders
        if exchange_orders is not None:
            for order in exchange_orders:
                oid = order.get("order_id")
                cid = order.get("client_order_id")
                if oid and oid not in orders_to_cancel:
                    orders_to_cancel[oid] = cid

        if not orders_to_cancel:
            if exchange_orders is None and not active_ids:
                logger.error("Failed to fetch exchange resting orders and no active local orders to kill.")
            else:
                logger.info("No active local or exchange resting orders to kill.")
            return

        for kalshi_order_id, client_order_id in list(orders_to_cancel.items()):
            ref = client_order_id or kalshi_order_id
            logger.warning(f"Canceling {ref} (Kalshi ID: {kalshi_order_id})...")
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
                    logger.info(f"Successfully killed {ref}")
                    if client_order_id:
                        self.om.active_orders.pop(client_order_id, None)
                elif resp.status_code == 404:
                    # Verified Kalshi ID returned 404: confirmed closed/filled on exchange
                    logger.info(f"Order {ref} ({kalshi_order_id}) already closed/filled on exchange.")
                    if client_order_id:
                        self.om.active_orders.pop(client_order_id, None)
                else:
                    logger.error(f"Failed to kill {ref}: {resp.status_code} - {resp.text}")
            except Exception as e:
                import traceback
                logger.error(f"Exception while killing {ref}: {e}\n{traceback.format_exc()}")

    async def trigger(self, ticker: Optional[str] = None):
        """Asynchronously triggers the kill switch using the core order manager."""
        logger.warning("Kill Switch Triggered (Async). Canceling all resting orders...")
        await send_alert("Kill Switch Triggered (Asynchronous). Withdrawing all quotes.")
        
        target_ticker = ticker or self.ticker
        exchange_orders = await asyncio.to_thread(self._fetch_resting_orders_sync, ticker=target_ticker)
        if exchange_orders is not None and target_ticker:
            exchange_orders = [o for o in exchange_orders if o.get("ticker") == target_ticker]

        client_to_exchange = {}
        if exchange_orders is not None:
            client_to_exchange = {
                o.get("client_order_id"): o.get("order_id")
                for o in exchange_orders
                if o.get("client_order_id") and o.get("order_id")
            }

        active_ids = list(self.om.active_orders.keys())
        tasks = []
        cids_being_cancelled = []
        kalshi_ids_covered = set()

        for cid in active_ids:
            order_info = self.om.active_orders.get(cid) or {}
            if target_ticker and order_info.get("ticker") and order_info.get("ticker") != target_ticker:
                continue

            kid = order_info.get("kalshi_order_id")
            if not kid:
                if exchange_orders is not None:
                    if cid in client_to_exchange:
                        kid = client_to_exchange[cid]
                        order_info["kalshi_order_id"] = kid
                    else:
                        logger.info(f"Order {cid} verified not resting on exchange.")
                        self.om.active_orders.pop(cid, None)
                        continue
                else:
                    logger.error(
                        f"Cannot safely cancel unverified order {cid}: "
                        "missing exchange order ID and resting order reconciliation failed."
                    )
                    continue

            if kid:
                kalshi_ids_covered.add(kid)
            tasks.append(self.om.cancel_order(cid))
            cids_being_cancelled.append(cid)

        # Union: Add untracked exchange resting orders
        if exchange_orders is not None:
            for order in exchange_orders:
                oid = order.get("order_id")
                cid = order.get("client_order_id")
                if not oid or oid in kalshi_ids_covered:
                    continue
                kalshi_ids_covered.add(oid)
                logger.warning(f"Canceling untracked exchange resting order: {oid} (client_id: {cid})")
                tasks.append(self.om._cancel_by_kalshi_id(oid, cid))
                cids_being_cancelled.append(cid or oid)

        if not tasks:
            if exchange_orders is None and not active_ids:
                logger.error("Failed to fetch exchange resting orders and no active local orders to kill.")
            else:
                logger.info("No active local or exchange resting orders to kill.")
            return

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for order_ref, res in zip(cids_being_cancelled, results):
            if isinstance(res, Exception):
                logger.error(f"Exception encountered attempting to kill {order_ref}: {res}")
            elif not res:
                logger.error(f"Failed to kill {order_ref} (API rejected)")
            else:
                logger.info(f"Successfully completed cancellation of {order_ref}")
