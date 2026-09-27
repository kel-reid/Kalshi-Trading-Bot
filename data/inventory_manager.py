"""
Inventory and Balance Manager

This module tracks USD balance and contract positions in real-time. It initialises 
the state via REST API hydration and maintains it by listening to WebSocket fill events. 
It also runs a background reconciliation loop to prevent state drift.
"""

import logging
import requests
import asyncio
import math
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
    KALSHI_ROUND_TRIPS_TOTAL,
    KALSHI_PORTFOLIO_VALUE_CENTS,
    KALSHI_POSITIONS_VALUE_CENTS,
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
        self.balance_cents: float = 0.0
        self.positions_value_cents: float = 0.0
        self._last_mid_prices: Dict[str, float] = {}
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
            try:
                data = response.json()
            except Exception as e:
                logger.error(f"Failed to parse balance JSON: {e}")
                return None
            if isinstance(data, dict) and "balance" in data and isinstance(data["balance"], (int, float)) and not isinstance(data["balance"], bool):
                if not math.isfinite(data["balance"]):
                    logger.error(f"Balance response contains non-finite balance: {data['balance']}")
                    return None
                pv = data.get("portfolio_value")
                if isinstance(pv, (int, float)) and not isinstance(pv, bool) and math.isfinite(pv):
                    self.positions_value_cents = float(pv)
                return int(round(data["balance"]))
            logger.error(f"Balance response missing or invalid 'balance' field: {data}")
            return None
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
            try:
                data = response.json()
            except Exception as e:
                logger.error(f"Failed to parse positions JSON: {e}")
                return None
            if isinstance(data, dict) and "market_positions" in data and isinstance(data["market_positions"], list):
                for entry in data["market_positions"]:
                    if not isinstance(entry, dict):
                        logger.error(f"Positions response contains malformed entry: {entry}")
                        return None
                    ticker = entry.get("ticker")
                    if not isinstance(ticker, str) or not ticker.strip():
                        logger.error(f"Positions response contains malformed entry missing valid ticker: {entry}")
                        return None
                    pos_val = entry.get("position_fp")
                    if pos_val is None:
                        pos_val = entry.get("position")
                    if pos_val is None:
                        logger.error(f"Positions response entry missing position field: {entry}")
                        return None
                    if isinstance(pos_val, bool):
                        logger.error(f"Positions response contains boolean position: {entry}")
                        return None
                    try:
                        pos_float = float(pos_val)
                        if not math.isfinite(pos_float) or not pos_float.is_integer():
                            logger.error(f"Positions response contains non-finite or non-integral position: {entry}")
                            return None
                    except (ValueError, TypeError):
                        logger.error(f"Positions response contains non-numeric position: {entry}")
                        return None
                return data["market_positions"]
            logger.error(f"Positions response missing or invalid 'market_positions' field: {data}")
            return None
        else:
            logger.error(f"Failed to fetch positions: {response.text}")
            return None

    def _apply_positions(self, market_positions: List[Dict[str, Any]], is_startup: bool = False) -> bool:
        """Applies REST positions to state, running on the event loop thread."""
        # Atomically validate and parse all entries before mutating state or PnL lots
        validated_entries = []
        for pos in market_positions:
            if not isinstance(pos, dict):
                logger.error(f"Malformed position entry: {pos}")
                return False
            ticker = pos.get("ticker")
            if not ticker or not isinstance(ticker, str) or not ticker.strip():
                logger.error(f"Malformed position entry missing valid ticker: {pos}")
                return False
            pos_val = pos.get("position_fp")
            if pos_val is None:
                pos_val = pos.get("position")
            if pos_val is None:
                logger.error(f"Position entry missing position field for {ticker}: {pos}")
                return False
            if isinstance(pos_val, bool):
                logger.error(f"Malformed boolean position for {ticker}: {pos}")
                return False
            try:
                pos_float = float(pos_val)
                if not math.isfinite(pos_float) or not pos_float.is_integer():
                    logger.error(f"Malformed non-finite or non-integral position value ({pos_val!r}) for {ticker}: {pos}")
                    return False
                position = int(pos_float)
            except (ValueError, TypeError):
                logger.error(f"Malformed position value ({pos_val!r}) for {ticker}: {pos}")
                return False
            validated_entries.append((ticker.strip(), position, pos))

        new_positions = {}
        for ticker, position, pos in validated_entries:
            if position != 0:
                new_positions[ticker] = position
                if is_startup:
                    exposure = pos.get("market_exposure")
                    cost_basis = None
                    if exposure is not None and not isinstance(exposure, bool):
                        try:
                            exp_val = float(exposure)
                            if math.isfinite(exp_val) and exp_val > 0 and position != 0:
                                cost_basis = round(exp_val / abs(position), 4)
                        except (ValueError, TypeError):
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
                KALSHI_PORTFOLIO_VALUE_CENTS.labels(ticker=t).set(self.get_portfolio_value(t))
                KALSHI_POSITIONS_VALUE_CENTS.labels(ticker=t).set(self.get_positions_value(t))
            except Exception as e:
                logger.debug(f"Prometheus metric update skipped for {t}: {e}")

        return True

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
        if self._fill_count != start_fill_count:
            logger.info(
                f"Skipping {'startup' if is_startup else 'periodic'} REST inventory reconciliation: {self._fill_count - start_fill_count} "
                f"fill(s) received during REST fetch window. Real-time WebSocket state is authoritative."
            )
            return False

        # On startup, verified ground truth for BOTH balance and positions is mandatory.
        # Boundary precondition check: reject immediately without mutating state if either is missing.
        if is_startup:
            if bal is None or market_positions is None:
                logger.error(
                    f"Startup portfolio hydration failed: missing required ground truth "
                    f"(balance={'ok' if bal is not None else 'failed'}, "
                    f"positions={'ok' if market_positions is not None else 'failed'}). "
                    f"Aborting without mutating portfolio state."
                )
                return False

        positions_applied = True
        if market_positions is not None:
            positions_applied = self._apply_positions(market_positions, is_startup=is_startup)

        # On startup, verified ground truth for BOTH balance and positions is mandatory.
        # Commit balance only after positions validate successfully to ensure atomic hydration.
        if is_startup:
            if not positions_applied:
                logger.error("Startup portfolio hydration failed: positions validation rejected payload.")
                return False
            self.balance_cents = bal
            logger.info(f"Hydrated Balance: {self.balance_cents} cents.")
            return True

        if bal is not None:
            self.balance_cents = bal
            logger.info(f"Hydrated Balance: {self.balance_cents} cents.")

        # For periodic reconciliation, return True if at least one snapshot succeeded and positions applied.
        return (bal is not None or market_positions is not None) and positions_applied

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
        action_raw = fill_msg.get("action")
        side_raw = fill_msg.get("side")
        count = fill_msg.get("count")
        price = fill_msg.get("price")

        # 1. Validate ticker
        if not ticker or not isinstance(ticker, str) or not ticker.strip():
            logger.warning(f"Dropping fill with missing or invalid ticker: {fill_msg}")
            return
        ticker = ticker.strip()

        # 2. Validate action and side
        action = action_raw.lower() if isinstance(action_raw, str) else ""
        side = side_raw.lower() if isinstance(side_raw, str) else ""
        if action not in ("buy", "sell") or side not in ("yes", "no"):
            logger.warning(f"Dropping fill with invalid action/side ({action_raw!r}, {side_raw!r}): {fill_msg}")
            return

        # 3. Validate and parse count (supports vendor count_fp string/float or legacy count)
        raw_count = fill_msg.get("count_fp") if fill_msg.get("count_fp") is not None else fill_msg.get("count")
        if raw_count is None or isinstance(raw_count, bool):
            logger.warning(f"Dropping fill with missing count: {fill_msg}")
            return

        try:
            count_f = float(raw_count)
        except (TypeError, ValueError):
            logger.warning(f"Dropping fill with non-numeric count ({raw_count!r}): {fill_msg}")
            return

        if not math.isfinite(count_f) or count_f <= 0:
            logger.warning(f"Dropping fill with invalid count ({raw_count!r}): {fill_msg}")
            return

        count = int(count_f) if count_f.is_integer() else round(count_f, 4)

        # 4. Resolve fill execution price from vendor dollar fields, generic price, or side-specific fields
        # Vendor WebSocket v2 provides yes_price_dollars / no_price_dollars as decimal dollar strings (e.g. "0.8700")
        price = None
        if side == "yes":
            if fill_msg.get("yes_price_dollars") is not None:
                try:
                    price = round(float(fill_msg["yes_price_dollars"]) * 100.0, 4)
                except (TypeError, ValueError):
                    pass
            elif fill_msg.get("no_price_dollars") is not None:
                try:
                    price = round(100.0 - (float(fill_msg["no_price_dollars"]) * 100.0), 4)
                except (TypeError, ValueError):
                    pass
            elif fill_msg.get("price_dollars") is not None:
                try:
                    price = round(float(fill_msg["price_dollars"]) * 100.0, 4)
                except (TypeError, ValueError):
                    pass
            elif fill_msg.get("price") is not None:
                price = fill_msg.get("price")
            elif fill_msg.get("yes_price") is not None:
                price = fill_msg.get("yes_price")
            elif fill_msg.get("no_price") is not None:
                opp = fill_msg.get("no_price")
                price = round(100.0 - opp, 4) if isinstance(opp, (int, float)) and not isinstance(opp, bool) and math.isfinite(opp) else None
        elif side == "no":
            if fill_msg.get("no_price_dollars") is not None:
                try:
                    price = round(float(fill_msg["no_price_dollars"]) * 100.0, 4)
                except (TypeError, ValueError):
                    pass
            elif fill_msg.get("yes_price_dollars") is not None:
                try:
                    price = round(100.0 - (float(fill_msg["yes_price_dollars"]) * 100.0), 4)
                except (TypeError, ValueError):
                    pass
            elif fill_msg.get("price_dollars") is not None:
                try:
                    price = round(float(fill_msg["price_dollars"]) * 100.0, 4)
                except (TypeError, ValueError):
                    pass
            elif fill_msg.get("price") is not None:
                price = fill_msg.get("price")
            elif fill_msg.get("no_price") is not None:
                price = fill_msg.get("no_price")
            elif fill_msg.get("yes_price") is not None:
                opp = fill_msg.get("yes_price")
                price = round(100.0 - opp, 4) if isinstance(opp, (int, float)) and not isinstance(opp, bool) and math.isfinite(opp) else None

        # Validate price (must be positive numeric in exchange range (0, 100) cents, not boolean, non-NaN/inf)
        if not isinstance(price, (int, float)) or isinstance(price, bool) or price <= 0 or price >= 100 or math.isnan(price) or math.isinf(price):
            logger.warning(f"Dropping fill with invalid/out-of-range price ({price!r}): {fill_msg}")
            return
        price = round(float(price), 4)

        # 5. Parse and validate fee safely (supports vendor fee_cost in dollars, fee_cents, or legacy fee in cents)
        if fill_msg.get("fee_cost") is not None:
            try:
                fee = round(float(fill_msg["fee_cost"]) * 100.0, 4)
            except (TypeError, ValueError):
                fee = None
        elif fill_msg.get("fee_dollars") is not None:
            try:
                fee = round(float(fill_msg["fee_dollars"]) * 100.0, 4)
            except (TypeError, ValueError):
                fee = None
        else:
            fee_val = fill_msg.get("fee_cents", fill_msg.get("fee", 0.0))
            try:
                fee = round(float(fee_val), 4)
            except (TypeError, ValueError):
                fee = None

        if fee is None or fee < 0.0 or math.isnan(fee) or math.isinf(fee):
            logger.warning(f"Dropping fill with invalid fee: {fill_msg}")
            return

        # All preconditions validated; state mutation and counter increment can now safely occur
        self._fill_count += 1
        fee = round(fee, 4)

        # Adjust balance based on action, preserving sub-cent precision to reconcile with PnLTracker
        if action == "buy":
            self.balance_cents = round(self.balance_cents - (price * count + fee), 4)
        elif action == "sell":
            self.balance_cents = round(self.balance_cents + (price * count - fee), 4)
            
        # Update positions
        # Standard convention: + for 'yes' shares, - for 'no' shares (or tracked separately)
        # We assume position is net 'yes' shares where + is YES and - is NO.
        delta = count if action == "buy" else -count
        
        if side == "no":
            delta = -delta # buying NO is equivalent to selling YES from a risk perspective
            
        current_pos = self.positions.get(ticker, 0)
        new_pos = round(current_pos + delta, 4)
        self.positions[ticker] = int(new_pos) if new_pos.is_integer() else new_pos
        
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
            KALSHI_PORTFOLIO_VALUE_CENTS.labels(ticker=ticker).set(self.get_portfolio_value(ticker))
            KALSHI_POSITIONS_VALUE_CENTS.labels(ticker=ticker).set(self.get_positions_value(ticker))

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
        self._last_mid_prices[ticker] = float(mid_price)
        unrealized = self.pnl_tracker.update_mid_price(ticker, mid_price)
        try:
            KALSHI_UNREALIZED_PNL_CENTS.labels(ticker=ticker).set(unrealized)
            KALSHI_POSITIONS_VALUE_CENTS.labels(ticker=ticker).set(self.get_positions_value(ticker))
            KALSHI_PORTFOLIO_VALUE_CENTS.labels(ticker=ticker).set(self.get_portfolio_value(ticker))
        except Exception as e:
            logger.debug(f"Prometheus unrealized/portfolio metric update skipped: {e}")
        return unrealized

    def get_positions_value(self, ticker: Optional[str] = None) -> float:
        """
        Returns estimated market value of open positions in cents.
        If a ticker is specified and has a known mid-price, computes live mark-to-market.
        Otherwise falls back to the REST portfolio_value or marked inventory across tickers.
        """
        if ticker and ticker in self.positions:
            pos = self.positions[ticker]
            mid = self._last_mid_prices.get(ticker)
            if mid is not None:
                if pos > 0:
                    return round(pos * mid, 4)
                elif pos < 0:
                    return round(abs(pos) * (100.0 - mid), 4)
                return 0.0
        # If no positions are held, positions value is 0.0
        if not self.positions or all(v == 0 for v in self.positions.values()):
            return 0.0
        return self.positions_value_cents

    def get_portfolio_value(self, ticker: Optional[str] = None) -> float:
        """Returns total portfolio value in cents (cash balance plus open positions market value)."""
        return round(self.balance_cents + self.get_positions_value(ticker), 4)

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
