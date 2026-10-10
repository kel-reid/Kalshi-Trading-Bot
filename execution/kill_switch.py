"""
Emergency Safety Kill Switch

This module provides emergency procedures to immediately cancel all resting orders 
and cease market making. It supports both asynchronous cancellation in the event 
of software crashes and synchronous cancellation for manual developer triggers (like Ctrl+C).
"""

import logging
import asyncio
from typing import Optional, List, Dict, Any
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

    def _delete_order_sync(self, kalshi_order_id: str, client_order_id: Optional[str] = None):
        """Helper to synchronously send DELETE request for an order."""
        ref = client_order_id or kalshi_order_id
        logger.warning(f"Canceling {ref} (Kalshi ID: {kalshi_order_id})...")
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
                logger.info(f"Order {ref} ({kalshi_order_id}) already closed/filled on exchange.")
                if client_order_id:
                    self.om.active_orders.pop(client_order_id, None)
            else:
                logger.error(f"Failed to kill {ref}: {resp.status_code} - {resp.text}")
        except Exception as e:
            import traceback
            logger.error(f"Exception while killing {ref}: {e}\n{traceback.format_exc()}")

    def trigger_synchronous(self, ticker: Optional[str] = None):
        """Immediately cancel all known local and exchange resting orders synchronously."""
        logger.warning("Kill Switch Triggered (Sync). Canceling all resting orders...")
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
        active_ids = list(self.om.active_orders.keys())
        cancelled_kalshi_ids = set()
        unverified_ids = []

        # 1. Immediately cancel all known orders with verified exchange IDs
        for client_order_id in active_ids:
            order_info = self.om.active_orders.get(client_order_id) or {}
            if target_ticker and order_info.get("ticker") and order_info.get("ticker") != target_ticker:
                continue

            kalshi_order_id = order_info.get("kalshi_order_id")
            if kalshi_order_id:
                self._delete_order_sync(kalshi_order_id, client_order_id)
                cancelled_kalshi_ids.add(kalshi_order_id)
            else:
                unverified_ids.append(client_order_id)

        # 2. Fetch exchange resting orders to reconcile unverified and discover untracked orders
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

        # 3. Reconcile and cancel unverified orders
        for client_order_id in unverified_ids:
            order_info = self.om.active_orders.get(client_order_id) or {}
            if exchange_orders is not None:
                if client_order_id in client_to_exchange:
                    kalshi_order_id = client_to_exchange[client_order_id]
                    order_info["kalshi_order_id"] = kalshi_order_id
                    if kalshi_order_id not in cancelled_kalshi_ids:
                        self._delete_order_sync(kalshi_order_id, client_order_id)
                        cancelled_kalshi_ids.add(kalshi_order_id)
                else:
                    logger.info(f"Order {client_order_id} verified not resting on exchange.")
                    self.om.active_orders.pop(client_order_id, None)
            else:
                logger.error(
                    f"Cannot safely cancel unverified order {client_order_id}: "
                    "missing exchange order ID and resting order reconciliation failed."
                )

        # 4. Cancel untracked exchange resting orders
        if exchange_orders is not None:
            for order in exchange_orders:
                oid = order.get("order_id")
                cid = order.get("client_order_id")
                if oid and oid not in cancelled_kalshi_ids:
                    self._delete_order_sync(oid, cid)
                    cancelled_kalshi_ids.add(oid)

        if not cancelled_kalshi_ids:
            if exchange_orders is None and not active_ids:
                logger.error("Failed to fetch exchange resting orders and no active local orders to kill.")
            else:
                logger.info("No active local or exchange resting orders to kill.")

    async def trigger(self, ticker: Optional[str] = None):
        """Asynchronously triggers the kill switch using the core order manager."""
        logger.warning("Kill Switch Triggered (Async). Canceling all resting orders...")
        await send_alert("Kill Switch Triggered (Asynchronous). Withdrawing all quotes.")
        
        target_ticker = ticker or self.ticker
        active_ids = list(self.om.active_orders.keys())
        tasks = []
        cids_being_cancelled = []
        kalshi_ids_covered = set()
        unverified_cids = []

        # 1. Immediately dispatch cancellations for known verified orders without waiting for fetch
        for cid in active_ids:
            order_info = self.om.active_orders.get(cid) or {}
            if target_ticker and order_info.get("ticker") and order_info.get("ticker") != target_ticker:
                continue

            kid = order_info.get("kalshi_order_id")
            if kid:
                kalshi_ids_covered.add(kid)
                tasks.append(asyncio.create_task(self.om.cancel_order(cid)))
                cids_being_cancelled.append(cid)
            else:
                unverified_cids.append(cid)

        # 2. Concurrently fetch exchange resting orders in background thread
        fetch_task = asyncio.create_task(
            asyncio.to_thread(self._fetch_resting_orders_sync, ticker=target_ticker)
        )

        exchange_orders = await fetch_task
        if exchange_orders is not None and target_ticker:
            exchange_orders = [o for o in exchange_orders if o.get("ticker") == target_ticker]

        client_to_exchange = {}
        if exchange_orders is not None:
            client_to_exchange = {
                o.get("client_order_id"): o.get("order_id")
                for o in exchange_orders
                if o.get("client_order_id") and o.get("order_id")
            }

        # 3. Reconcile unverified orders against fetched exchange orders
        for cid in unverified_cids:
            order_info = self.om.active_orders.get(cid) or {}
            if exchange_orders is not None:
                if cid in client_to_exchange:
                    kid = client_to_exchange[cid]
                    order_info["kalshi_order_id"] = kid
                    kalshi_ids_covered.add(kid)
                    tasks.append(asyncio.create_task(self.om.cancel_order(cid)))
                    cids_being_cancelled.append(cid)
                else:
                    logger.info(f"Order {cid} verified not resting on exchange.")
                    self.om.active_orders.pop(cid, None)
            else:
                logger.error(
                    f"Cannot safely cancel unverified order {cid}: "
                    "missing exchange order ID and resting order reconciliation failed."
                )

        # 4. Union: Add untracked exchange resting orders
        if exchange_orders is not None:
            for order in exchange_orders:
                oid = order.get("order_id")
                cid = order.get("client_order_id")
                if not oid or oid in kalshi_ids_covered:
                    continue
                kalshi_ids_covered.add(oid)
                logger.warning(f"Canceling untracked exchange resting order: {oid} (client_id: {cid})")
                tasks.append(asyncio.create_task(self.om._cancel_by_kalshi_id(oid, cid)))
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
