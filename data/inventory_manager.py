"""
Inventory and Balance Manager

This module tracks USD balance and contract positions in real-time. It initialises 
the state via REST API hydration and maintains it by listening to WebSocket fill events. 
It also runs a background reconciliation loop to prevent state drift.
"""

import logging
import requests
import asyncio
import certifi
from typing import Dict, Any, Optional, List

# Provide required imports from our new module

from config import BASE_URL
from auth.kalshi_auth import get_auth_headers
from utils.metrics import (
    measure_latency,
    BOT_PNL_CENTS,
    BOT_INVENTORY_NET_POSITION,
    KALSHI_REALIZED_PNL_CENTS,
    KALSHI_UNREALIZED_PNL_CENTS,
    KALSHI_FEES_PAID_CENTS,
    KALSHI_ROUND_TRIPS_TOTAL
)
from data.pnl_tracker import PnLTracker

logger = logging.getLogger("InventoryManager")

class InventoryManager:
    """
    Maintains real-time tracking of current position sizes and USD balance.
    Hydrates initial state via REST and updates via WebSocket 'fill' events.
    Integrates PnLTracker for FIFO trade matching and mark-to-market accounting.
    """
    def __init__(self, ws_client, pnl_tracker: Optional[PnLTracker] = None):
        self.ws_client = ws_client
        self.balance_cents: int = 0
        # self.positions[ticker] = position_size (positive means net 'yes', negative means net 'no' or just track absolute shares)
        # Kalshi usually tracks 'position' as an integer of contracts.
        self.positions: Dict[str, int] = {}
        self.pnl_tracker: PnLTracker = pnl_tracker or PnLTracker()
        self._fill_count: int = 0
        
        self.ws_client.add_message_handler(self._handle_message)

    def _fetch_balance(self) -> Optional[int]:
        """Fetch cash balance via REST."""
        sign_path = "/trade-api/v2/portfolio/balance"
        headers = get_auth_headers(method="GET", sign_path=sign_path)
        with measure_latency("GET", "/trade-api/v2/portfolio/balance"):
            response = requests.get(BASE_URL + sign_path, headers=headers, timeout=10, verify=certifi.where())
        if response.status_code == 200:
            return response.json().get("balance", 0)
        else:
            logger.error(f"Failed to fetch balance: {response.text}")
            return None

    def _fetch_positions(self) -> Optional[List[Dict[str, Any]]]:
        """Fetch positions via REST."""
        sign_path = "/trade-api/v2/portfolio/positions"
        headers = get_auth_headers(method="GET", sign_path=sign_path)
        with measure_latency("GET", "/trade-api/v2/portfolio/positions"):
            response = requests.get(BASE_URL + sign_path, headers=headers, params={"limit": 200}, timeout=10, verify=certifi.where())
        if response.status_code == 200:
            return response.json().get("market_positions", [])
        else:
            logger.error(f"Failed to fetch positions: {response.text}")
            return None

    def _apply_positions(self, market_positions: List[Dict[str, Any]], is_startup: bool = False):
        """Applies REST positions to state, running on the event loop thread."""
        new_positions = {}
        for pos in market_positions:
            ticker = pos.get("ticker")
            # Kalshi V2 API returns position as 'position_fp' (float string) or 'position' (int)
            position = int(float(pos.get("position_fp", pos.get("position", 0))))
            if ticker and position != 0:
                new_positions[ticker] = position
                if is_startup:
                    exposure = pos.get("market_exposure")
                    cost_basis = None
                    try:
                        if exposure is not None and float(exposure) != 0 and position != 0:
                            cost_basis = abs(float(exposure) / float(position))
                    except Exception:
                        cost_basis = None
                    self.pnl_tracker.seed_initial_inventory(ticker, position, cost_basis_cents=cost_basis)
                else:
                    self.pnl_tracker.reconcile_inventory(ticker, position)

        # For any previously tracked market that is now flat (not in REST active positions)
        if not is_startup:
            for active_ticker in list(self.positions.keys()):
                if active_ticker not in new_positions:
                    self.pnl_tracker.reconcile_inventory(active_ticker, 0)

        # Identify all tickers affected by this hydration/reconciliation
        all_affected_tickers = set(new_positions.keys()) | (set(self.positions.keys()) if not is_startup else set())

        self.positions = new_positions
        logger.info(f"Hydrated {len(self.positions)} active positions: {self.positions}")

        # Publish Prometheus metrics immediately so dashboards reflect state without waiting for fills/ticks
        for t in all_affected_tickers:
            pos_val = self.positions.get(t, 0)
            try:
                BOT_INVENTORY_NET_POSITION.labels(ticker=t).set(pos_val)
                BOT_PNL_CENTS.labels(ticker=t).set(self.balance_cents)
                KALSHI_REALIZED_PNL_CENTS.labels(ticker=t).set(self.pnl_tracker.get_realized_pnl(t))
                KALSHI_UNREALIZED_PNL_CENTS.labels(ticker=t).set(self.pnl_tracker.get_unrealized_pnl(t))
                KALSHI_FEES_PAID_CENTS.labels(ticker=t).set(self.pnl_tracker.get_total_fees(t))
            except Exception as e:
                logger.debug(f"Prometheus metric update skipped for {t}: {e}")

    def _hydrate_balance(self):
        """Fetch initial balance via REST synchronously."""
        bal = self._fetch_balance()
        if bal is not None:
            self.balance_cents = bal
            logger.info(f"Hydrated Balance: {self.balance_cents} cents.")

    def _hydrate_positions(self, is_startup: bool = False):
        """Fetch initial positions via REST synchronously."""
        market_positions = self._fetch_positions()
        if market_positions is not None:
            self._apply_positions(market_positions, is_startup=is_startup)

    async def hydrate(self, is_startup: bool = False) -> bool:
        """Run hydration asynchronously to avoid blocking the event loop."""
        logger.info(f"Hydrating inventory state from REST API (startup={is_startup})...")
        start_fill_count = self._fill_count
        bal = await asyncio.to_thread(self._fetch_balance)
        market_positions = await asyncio.to_thread(self._fetch_positions)

        # Concurrency guard: if a fill arrived while REST requests were in-flight,
        # the REST snapshot is stale and would overwrite real-time positions.
        if not is_startup and self._fill_count != start_fill_count:
            logger.info(
                f"Skipping periodic REST inventory reconciliation: {self._fill_count - start_fill_count} "
                f"fill(s) received during REST fetch window. Real-time WebSocket state is authoritative."
            )
            return False

        if bal is not None:
            self.balance_cents = bal
            logger.info(f"Hydrated Balance: {self.balance_cents} cents.")

        if market_positions is not None:
            self._apply_positions(market_positions, is_startup=is_startup)
            return True
        return False

    async def _sync_loop(self):
        """
        Continuously runs in the background. Every 5 minutes, 
        fetches the authoritative ground truth from the REST API 
        to correct any state drift from missed WebSocket messages.
        """
        logger.info("Starting background state drift reconciliation loop (5 min intervals).")
        while True:
            await asyncio.sleep(300) # 5 minutes
            try:
                logger.info("Running periodic inventory reconciliation to fix state drift...")
                await self.hydrate(is_startup=False)
            except Exception as e:
                logger.error(f"Error during periodic inventory reconciliation: {e}")

    async def subscribe(self):
        """Subscribe to the 'fill' channel to listen for my own execution updates."""
        await self.ws_client.subscribe(["fill"])

    async def _handle_message(self, message: Dict[str, Any]):
        msg_type = message.get("type")
        
        if msg_type == "fill":
            self._handle_fill(message.get("msg", {}))

    def _handle_fill(self, fill_msg: Dict[str, Any]):
        """
        Process a fill notification and update inventory.
        Typically on Kalshi a fill might look like:
        {
            "market_ticker": "KXINFL-23...",
            "action": "buy",
            "side": "yes",
            "count": 10,
            "price": 45
        }
        """
        ticker = fill_msg.get("market_ticker")
        action = fill_msg.get("action") # buy or sell
        side = fill_msg.get("side") # yes or no
        count = fill_msg.get("count", 0)
        price = fill_msg.get("price", 0) # in cents
        
        if not ticker or count == 0:
            return
            
        self._fill_count += 1
            
        # Update balance
        # If we buy, we spend (price * count) cents
        # If we sell, we receive (price * count) cents
        # Wait, selling "yes" might also include resolving risks or just earning the premium.
        # But for simple inventory tracking, we just track the position delta.
        fee = float(fill_msg.get("fee_cents", fill_msg.get("fee", 0.0)))
        fee_int = int(round(fee))

        # Adjust balance based on action (including fees)
        if action == "buy":
            self.balance_cents -= (price * count + fee_int)
        elif action == "sell":
            self.balance_cents += (price * count - fee_int)
            
        # Update positions
        # Standard convention: + for 'yes' shares, - for 'no' shares (or tracked separately)
        # Kalshi usually tracks them as positive positions of the specific side.
        # Assuming our simple market maker focuses on 'yes' contracts (or tracks them neutrally as 'yes' equivalents):
        # Let's track the actual balance of shares as reported by Kalshi portfolio (usually positive integer).
        # We will assume position is net 'yes' shares where + is YES and - is NO.
        delta = count if action == "buy" else -count
        
        if side == "no":
            delta = -delta # buying NO is equivalent to selling YES from a risk perspective
            
        current_pos = self.positions.get(ticker, 0)
        self.positions[ticker] = current_pos + delta
        
        # Track Realized and Unrealized PnL via FIFO lot matching
        pnl_impact = self.pnl_tracker.record_fill(
            ticker=ticker,
            action=action,
            side=side,
            count=count,
            price_cents=price,
            fee_cents=fee
        )

        # Update Prometheus metrics
        try:
            BOT_INVENTORY_NET_POSITION.labels(ticker=ticker).set(self.positions[ticker])
            BOT_PNL_CENTS.labels(ticker=ticker).set(self.balance_cents)
            KALSHI_REALIZED_PNL_CENTS.labels(ticker=ticker).set(self.pnl_tracker.get_realized_pnl(ticker))
            KALSHI_UNREALIZED_PNL_CENTS.labels(ticker=ticker).set(self.pnl_tracker.get_unrealized_pnl(ticker))
            KALSHI_FEES_PAID_CENTS.labels(ticker=ticker).set(self.pnl_tracker.get_total_fees(ticker))

            for outcome in pnl_impact.get("matched_outcomes", []):
                KALSHI_ROUND_TRIPS_TOTAL.labels(ticker=ticker, outcome=outcome).inc()
        except Exception as e:
            logger.debug(f"Prometheus metric update skipped: {e}")

        logger.info(
            f"Fill processed for {ticker}: {action} {count} {side} @ {price}c. "
            f"New Net Pos: {self.positions[ticker]} | Realized PnL: {self.pnl_tracker.get_realized_pnl(ticker):+.2f}c."
        )

    def update_orderbook_mid(self, ticker: str, mid_price: float) -> float:
        """
        Updates mark-to-market unrealized PnL against current mid price
        and emits Prometheus telemetry.
        """
        unrealized = self.pnl_tracker.update_mid_price(ticker, mid_price)
        try:
            KALSHI_UNREALIZED_PNL_CENTS.labels(ticker=ticker).set(unrealized)
        except Exception as e:
            logger.debug(f"Prometheus unrealized metric update skipped: {e}")
        return unrealized

    def get_realized_pnl(self, ticker: str) -> float:
        """Returns cumulative realized PnL in cents for a ticker."""
        return self.pnl_tracker.get_realized_pnl(ticker)

    def get_unrealized_pnl(self, ticker: str) -> float:
        """Returns current unrealized mark-to-market PnL in cents for a ticker."""
        return self.pnl_tracker.get_unrealized_pnl(ticker)

    def get_pnl_summary(self, ticker: str) -> Dict[str, Any]:
        """Returns a comprehensive PnL and trade attribution summary."""
        return self.pnl_tracker.get_market_summary(ticker)

    def get_position(self, ticker: str) -> int:
        return self.positions.get(ticker, 0)
        
    def get_balance(self) -> int:
        return self.balance_cents
