"""
Order Execution Manager

This module processes order placements and cancellations via the Kalshi REST API. 
It rate-limits REST requests to 10/s, records active quote states, and persists 
all transaction records (order IDs, parameters, and statuses) in the PostgreSQL database.
"""

from config import BASE_URL, DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
from auth.kalshi_auth import get_auth_headers
from contextlib import closing

import logging
import requests
import asyncio
import uuid
import certifi
import psycopg2
import datetime
from typing import Dict, Any, List, Optional
from utils.rate_limiter import RateLimiter
from utils.metrics import measure_latency, ORDERS_PLACED_TOTAL, ORDER_ERRORS_TOTAL

logger = logging.getLogger("OrderManager")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

class OrderManager:
    """
    Handles placing, replacing, and canceling discrete Limit orders via Kalshi REST API.
    Maintains a local lightweight record of active order IDs.
    """
    def __init__(self):
        # Maps client_order_id -> Order Dict
        self.active_orders: Dict[str, Dict[str, Any]] = {}

        # Initialize PostgreSQL database connection and schema
        self._init_db()

        # Kalshi Rate Limit: 10 requests per second
        self.rate_limiter = RateLimiter(rate=10, per=1.0)

    def _get_connection(self, allow_retries: bool = False):
        """Helper to establish a PostgreSQL database connection with optional retry logic."""
        import time
        max_retries = 10 if allow_retries else 1
        delay = 2
        for attempt in range(max_retries):
            try:
                conn = psycopg2.connect(
                    host=DB_HOST,
                    port=DB_PORT,
                    database=DB_NAME,
                    user=DB_USER,
                    password=DB_PASSWORD,
                    connect_timeout=5
                )
                return conn
            except psycopg2.OperationalError as e:
                if attempt == max_retries - 1:
                    if allow_retries:
                        logger.critical(f"Database connection failed after {max_retries} attempts: {e}")
                    else:
                        logger.error(f"Database connection failed: {e}")
                    raise
                logger.warning(f"Database not ready yet (attempt {attempt+1}/{max_retries}). Retrying in {delay}s...")
                time.sleep(delay)

    def _init_db(self):
        """Initializes the PostgreSQL database table for order tracking."""
        with closing(self._get_connection(allow_retries=True)) as conn:
            with conn.cursor() as cursor:
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS orders (
                        client_order_id VARCHAR(255) PRIMARY KEY,
                        kalshi_order_id VARCHAR(255),
                        ticker VARCHAR(255),
                        action VARCHAR(255),
                        side VARCHAR(255),
                        price INTEGER,
                        count INTEGER,
                        status VARCHAR(255),
                        created_at TIMESTAMPTZ,
                        updated_at TIMESTAMPTZ
                    )
                ''')
                conn.commit()

    def _update_db_order_status(self, client_order_id: str, status: str, kalshi_order_id: str = None, order_details: dict = None):
        """Updates or inserts an order record into the PostgreSQL database. Fails fast without blocking."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        try:
            with closing(self._get_connection(allow_retries=False)) as conn:
                with conn.cursor() as cursor:
                    # Check if order exists
                    cursor.execute("SELECT client_order_id FROM orders WHERE client_order_id = %s", (client_order_id,))
                    exists = cursor.fetchone()
                    
                    if exists:
                        if kalshi_order_id:
                            cursor.execute('''
                                UPDATE orders 
                                SET status = %s, kalshi_order_id = %s, updated_at = %s
                                WHERE client_order_id = %s
                            ''', (status, kalshi_order_id, now, client_order_id))
                        else:
                            cursor.execute('''
                                UPDATE orders 
                                SET status = %s, updated_at = %s
                                WHERE client_order_id = %s
                            ''', (status, now, client_order_id))
                    elif order_details:
                        cursor.execute('''
                            INSERT INTO orders (
                                client_order_id, kalshi_order_id, ticker, action, side, 
                                price, count, status, created_at, updated_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ''', (
                            client_order_id, 
                            kalshi_order_id, 
                            order_details.get("ticker"), 
                            order_details.get("action"), 
                            order_details.get("side"), 
                            order_details.get("price"), 
                            order_details.get("count"), 
                            status, 
                            now, 
                            now
                        ))
                    conn.commit()
        except Exception as e:
            logger.error(f"Failed to write order status to database (client_id: {client_order_id}, status: {status}): {e}")

    async def place_order(self, ticker: str, side: str, action: str, count: int, price: int) -> Optional[str]:
        """
        Place a new order.
        side: "yes" or "no"
        action: "buy" or "sell"
        count: number of contracts
        price: limit price in cents (1-99)
        Returns the client_order_id if successful, None otherwise.
        """
        client_order_id = str(uuid.uuid4())
        
        # Map legacy (action, side) parameters to V2 order book (side) parameter
        # In V2 events/orders, side represents YES contract order book interaction:
        # - buying YES is a "bid"
        # - selling YES (or buying NO) is an "ask"
        side = side.lower()
        action = action.lower()
        if side not in ("yes", "no"):
            raise ValueError(f"Invalid side: {side!r}. Expected 'yes' or 'no'.")
        if action not in ("buy", "sell"):
            raise ValueError(f"Invalid action: {action!r}. Expected 'buy' or 'sell'.")
        if side == "yes":
            v2_side = "bid" if action == "buy" else "ask"
        else:
            v2_side = "ask" if action == "buy" else "bid"

        payload = {
            "side": v2_side,
            "count": str(count),
            "type": "limit",
            "ticker": ticker,
            "client_order_id": client_order_id,
            "price": f"{price / 100:.2f}",
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross"
        }
        
        sign_path = "/trade-api/v2/portfolio/events/orders"
        
        max_retries = 3
        base_delay = 1.0
        
        for attempt in range(max_retries + 1):
            # 1. Wait for token to enforce overall rate limit
            await self.rate_limiter.acquire()
            
            # 2. Perform REST request without blocking the async loop
            response = await asyncio.to_thread(self._post_request, sign_path, payload)
            
            if response is not None:
                if response.status_code == 201:
                    logger.info(f"Order Placed: {action} {count} {side} @ {price}c for {ticker} (ID: {client_order_id})")
                    ORDERS_PLACED_TOTAL.labels(ticker=ticker, action=action, side=side).inc()
                    
                    self.active_orders[client_order_id] = {
                        "ticker": ticker,
                        "side": side,
                        "action": action,
                        "count": count,
                        "price": price,
                        "kalshi_order_id": response.json().get("order", {}).get("order_id")
                    }
                    
                    # Persist to database
                    self._update_db_order_status(
                        client_order_id, 
                        "resting", 
                        kalshi_order_id=self.active_orders[client_order_id]["kalshi_order_id"],
                        order_details=self.active_orders[client_order_id]
                    )
                    
                    return client_order_id
                
                elif response.status_code == 429:
                    if attempt < max_retries:
                        delay = base_delay * (2 ** attempt)
                        logger.warning(f"Rate limited (429) placing order. Retrying in {delay}s (Attempt {attempt+1}/{max_retries})")
                        await asyncio.sleep(delay)
                        continue # Retry
                    else:
                        logger.error(f"Failed to place order after {max_retries} retries due to rate limits.")
                        ORDER_ERRORS_TOTAL.labels(type="place_rate_limit").inc()
                        return None
                
            err_text = response.text if response is not None else "No response"
            logger.error(f"Failed to place order: {payload}. Error: {err_text}")
            ORDER_ERRORS_TOTAL.labels(type="place_api_error").inc()
            return None

    async def cancel_order(self, client_order_id: str) -> bool:
        """
        Cancel an existing order by its ID.
        Returns True if successfully cancelled/already cancelled, False on error.
        """
        if client_order_id not in self.active_orders:
            logger.warning(f"Order {client_order_id} not found in active orders.")
            # Note: order might have filled, or tracker lost it.
            # We could still try to cancel it if we really wanted to.
        
        order_to_cancel = self.active_orders.get(client_order_id)
        if order_to_cancel and order_to_cancel.get("kalshi_order_id"):
            # V2 API uses the actual order_id returned by Kalshi, not client_order_id, for cancellations
            order_id = order_to_cancel["kalshi_order_id"]
        else:
            # Fallback (Kalshi V2 usually expects `order_id` in the URL path)
            order_id = client_order_id
            
        sign_path = f"/trade-api/v2/portfolio/events/orders/{order_id}"
        
        max_retries = 3
        base_delay = 1.0
        
        for attempt in range(max_retries + 1):
            # 1. Wait for token to enforce overall rate limit
            await self.rate_limiter.acquire()
            
            response = await asyncio.to_thread(self._delete_request, sign_path)
            
            if response is not None:
                if response.status_code in [200, 204]:
                    logger.info(f"Order Cancelled: {client_order_id}")
                    self.active_orders.pop(client_order_id, None)
                    self._update_db_order_status(client_order_id, "cancelled")
                    return True
                elif response.status_code == 404:
                    # Order might already be filled or cancelled
                    logger.info(f"Order {client_order_id} not found on server (may be filled/cancelled already).")
                    self.active_orders.pop(client_order_id, None)
                    self._update_db_order_status(client_order_id, "cancelled_or_filled_404")
                    return True
                elif response.status_code == 429:
                    if attempt < max_retries:
                        delay = base_delay * (2 ** attempt)
                        logger.warning(f"Rate limited (429) canceling order. Retrying in {delay}s (Attempt {attempt+1}/{max_retries})")
                        await asyncio.sleep(delay)
                        continue # Retry
                    else:
                        logger.error(f"Failed to cancel order after {max_retries} retries due to rate limits.")
                        ORDER_ERRORS_TOTAL.labels(type="cancel_rate_limit").inc()
                        return False
            
            err_text = response.text if response is not None else "No response"
            logger.error(f"Failed to cancel order {client_order_id}. Error: {err_text}")
            ORDER_ERRORS_TOTAL.labels(type="cancel_api_error").inc()
            return False

    def _post_request(self, sign_path: str, payload: dict) -> Optional[requests.Response]:
        """Synchronous wrapper for POST requests"""
        try:
            # get_auth_headers needs the path and method
            headers = get_auth_headers(method="POST", sign_path=sign_path)
            # Kalshi API expects content-type to be application/json
            headers["Content-Type"] = "application/json"
            
            import json
            json_payload = json.dumps(payload, separators=(',', ':'))
            
            logger.info(f"POST {BASE_URL + sign_path} payload: {json_payload}")
            with measure_latency("POST", sign_path):
                resp = requests.post(
                    BASE_URL + sign_path,
                    data=json_payload,
                    headers=headers,
                    timeout=10,
                    verify=certifi.where()
                )
            logger.info(f"POST Response: {resp.status_code} - {resp.text}")
            return resp
        except Exception as e:
            import traceback
            logger.error(f"POST Request Exception: {e}\n{traceback.format_exc()}")
            return None

    def _delete_request(self, sign_path: str) -> Optional[requests.Response]:
        """Synchronous wrapper for DELETE requests"""
        try:
            headers = get_auth_headers(method="DELETE", sign_path=sign_path)
            with measure_latency("DELETE", "/trade-api/v2/portfolio/events/orders"):
                return requests.delete(
                    BASE_URL + sign_path,
                    headers=headers,
                    timeout=10,
                    verify=certifi.where()
                )
        except Exception as e:
            logger.error(f"DELETE Request Exception: {e}")
            return None

    async def _cancel_by_kalshi_id(self, order_id: str, client_order_id: Optional[str]):
        """Helper method to cancel an order directly by Kalshi order ID (used during recovery)."""
        cancel_path = f"/trade-api/v2/portfolio/events/orders/{order_id}"
        resp = await asyncio.to_thread(self._delete_request, cancel_path)
        if resp and resp.status_code in [200, 204]:
            logger.info(f"Successfully cancelled orphaned order {cancel_path}")
            if client_order_id:
                self._update_db_order_status(client_order_id, "cancelled", kalshi_order_id=order_id)
        else:
            err = resp.text if resp else "No response"
            logger.error(f"Failed to cancel orphaned order {cancel_path}: {err}")

    def get_tracked_active_orders(self, ticker: str = None) -> List[Dict[str, Any]]:
        """Return list of active orders we are currently tracking, optionally filtered by ticker."""
        orders = []
        for cid, details in self.active_orders.items():
            if not ticker or details.get("ticker") == ticker:
                order_copy = dict(details)
                order_copy["client_order_id"] = cid
                orders.append(order_copy)
        return orders

    async def sync_and_recover_state(self):
        """
        Fetches all resting orders from the REST API and cancels them.
        This ensures that when the bot starts, it doesn't leave orphaned orders
        from a previous crashed run.
        """
        logger.info("Starting state recovery and reconciliation...")
        
        sign_path = "/trade-api/v2/portfolio/orders"
        query_params = {"status": "resting", "limit": 100}
        
        try:
            # Need to build query string for signature if it contains params, but for Kalshi V2,
            # the signature is usually just on the path. We will use the requests library to handle params.
            # But get_auth_headers needs the path without params for the signature, let's keep it simple.
            headers = get_auth_headers(method="GET", sign_path=sign_path)
            
            # Wrap the cross-thread call to still capture overall latency
            with measure_latency("GET", "/trade-api/v2/portfolio/orders"):
                response = await asyncio.to_thread(
                    requests.get,
                    BASE_URL + sign_path,
                    headers=headers,
                    params=query_params,
                    timeout=10,
                    verify=certifi.where()
                )
                
            if response.status_code == 200:
                orders_list = response.json().get("orders", [])
                logger.info(f"Found {len(orders_list)} resting orders on Kalshi.")
                
                cancel_tasks = []
                for order in orders_list:
                    order_id = order.get("order_id")
                    client_order_id = order.get("client_order_id")
                    logger.info(f"Preparing to cancel orphaned order: {order_id} (Client ID: {client_order_id})")
                    
                    # We can use the REST API directly to cancel by Kalshi order_id to be safe
                    cancel_tasks.append(self._cancel_by_kalshi_id(order_id, client_order_id))
                    
                if cancel_tasks:
                    logger.info(f"Executing {len(cancel_tasks)} cancellation tasks...")
                    await asyncio.gather(*cancel_tasks, return_exceptions=True)
                else:
                    logger.info("No orphaned resting orders found. State is clean.")
            else:
                logger.error(f"Failed to fetch resting orders during recovery. Status: {response.status_code}, Response: {response.text}")
                
            # Now hunt for combo orders (order_groups)
            group_path = "/trade-api/v2/portfolio/order_groups"
            group_headers = get_auth_headers(method="GET", sign_path=group_path)
            
            with measure_latency("GET", group_path):
                group_resp = await asyncio.to_thread(
                    requests.get,
                    BASE_URL + group_path,
                    headers=group_headers,
                    params={"status": "resting", "limit": 100},
                    timeout=10,
                    verify=certifi.where()
                )
                
            if group_resp.status_code == 200:
                groups_list = group_resp.json().get("order_groups", [])
                logger.info(f"Found {len(groups_list)} resting combo orders (order groups) on Kalshi.")
                
                group_tasks = []
                for group in groups_list:
                    group_id = group.get("order_group_id")
                    logger.info(f"Preparing to cancel orphaned combo order group: {group_id}")
                    
                    # Group cancellation endpoint is DELETE /trade-api/v2/portfolio/order_groups/{order_group_id}
                    cancel_path = f"/trade-api/v2/portfolio/order_groups/{group_id}"
                    
                    async def cancel_group(path, gid):
                        headers = get_auth_headers(method="DELETE", sign_path=path)
                        res = await asyncio.to_thread(
                            requests.delete,
                            BASE_URL + path,
                            headers=headers,
                            timeout=10,
                            verify=certifi.where()
                        )
                        if res.status_code in [200, 204]:
                            logger.info(f"Successfully killed combo order {gid}")
                        else:
                            logger.error(f"Failed to kill combo order {gid}: {res.status_code} - {res.text}")
                    
                    group_tasks.append(cancel_group(cancel_path, group_id))
                
                if group_tasks:
                    logger.info(f"Executing {len(group_tasks)} combo cancellation tasks...")
                    await asyncio.gather(*group_tasks, return_exceptions=True)
                    
            logger.info("State recovery and reconciliation complete.")
                
        except Exception as e:
            import traceback
            logger.error(f"Exception during state recovery: {e}\n{traceback.format_exc()}")
