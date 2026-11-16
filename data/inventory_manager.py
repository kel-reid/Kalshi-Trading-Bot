"""
Inventory and Balance Manager

This module tracks USD balance and contract positions in real-time. It initialises 
the state via REST API hydration and maintains it by listening to WebSocket fill events. 
It also runs a background reconciliation loop to prevent state drift.
"""

import logging
import requests
import asyncio
import os
import math
import time
import certifi
from decimal import Decimal
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
from data.fill_spool import FillSpool
from data.websocket_client import FatalIngestionError

logger = logging.getLogger("InventoryManager")

class InventoryManager:
    """
    Maintains real-time tracking of current position sizes and USD balance.
    Hydrates initial state via REST and updates via WebSocket 'fill' events.
    Integrates PnLTracker for FIFO trade matching and mark-to-market accounting.
    """
    def __init__(self, ws_client, pnl_tracker: Optional[PnLTracker] = None, fill_store=None):
        self.ws_client = ws_client
        self.fill_store = fill_store
        self._fill_spool = None
        self._spool_lock = asyncio.Lock()
        self._fill_wakeup = asyncio.Event()
        self._fill_stopping = False
        self._fill_failure = None
        self._fill_worker = None
        self._fill_retry_interval = 1.0
        self._fill_init_task = None
        self._seen_fill_ids = {}
        self.balance_cents: float = 0.0
        self.positions_value_cents: float = 0.0
        self._last_mid_prices: Dict[str, float] = {}
        # self.positions[ticker] = position_size (positive means net 'yes', negative means net 'no' or just track absolute shares)
        # Kalshi usually tracks 'position' as an integer of contracts.
        self.positions: Dict[str, int] = {}
        self.pnl_tracker: PnLTracker = pnl_tracker or PnLTracker()
        self._fill_count: int = 0
        self._pending_fills: int = 0
        self._last_fill_time: float = 0.0
        self._last_fill_times: Dict[str, float] = {}
        
        self.ws_client.add_message_handler(self._handle_message)

    def get_last_fill_time(self, ticker: Optional[str] = None) -> float:
        """Returns the unix timestamp of the most recent fill for the ticker (or globally)."""
        if ticker:
            return self._last_fill_times.get(ticker, 0.0)
        return self._last_fill_time

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

    def _fetch_ticker_positions(self, ticker: str) -> Optional[Dict[int, Decimal]]:
        """Fetch every shard for one ticker in the primary subaccount, or fail closed."""
        path = "/trade-api/v2/portfolio/positions"
        params = {"ticker": ticker, "limit": 200}
        positions = {}
        cursors = set()
        try:
            while True:
                headers = get_auth_headers(method="GET", sign_path=path)
                with measure_latency("GET", path):
                    response = requests.get(BASE_URL + path, headers=headers, params=params, timeout=10, verify=certifi.where())
                if response.status_code != 200:
                    return None
                data = response.json()
                if not isinstance(data, dict) or not isinstance(data.get("market_positions"), list):
                    return None
                cursor = data.get("cursor")
                if not isinstance(cursor, str):
                    return None
                for entry in data["market_positions"]:
                    if not isinstance(entry, dict) or entry.get("ticker") != ticker:
                        return None
                    shard = entry.get("exchange_index")
                    raw = entry.get("position_fp", entry.get("position"))
                    if isinstance(shard, bool) or not isinstance(shard, int) or shard < 0 or shard in positions or isinstance(raw, bool):
                        return None
                    value = Decimal(str(raw))
                    if not value.is_finite():
                        return None
                    positions[shard] = value
                if not cursor:
                    return positions
                if cursor in cursors or len(cursors) >= 100:
                    return None
                cursors.add(cursor)
                params["cursor"] = cursor
        except Exception as exc:
            logger.error(f"Failed to verify positions for {ticker}: {exc}")
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
                        if not math.isfinite(pos_float):
                            logger.error(f"Positions response contains non-finite position: {entry}")
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
                if not math.isfinite(pos_float):
                    logger.error(f"Malformed non-finite position value ({pos_val!r}) for {ticker}: {pos}")
                    return False
                position = int(pos_float) if pos_float.is_integer() else round(pos_float, 4)
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
                KALSHI_PORTFOLIO_VALUE_CENTS.set(self.get_portfolio_value(t))
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
        start_pending_fills = self._pending_fills
        bal = await asyncio.to_thread(self._fetch_balance)
        market_positions = await asyncio.to_thread(self._fetch_positions)

        # Concurrency guard: if a fill arrived while REST requests were in-flight,
        # or if fill ingestion was pending when hydration started or finished,
        # the REST snapshot is stale and would overwrite real-time positions.
        if (
            self._fill_count != start_fill_count
            or self._pending_fills > 0
            or start_pending_fills > 0
        ):
            logger.info(
                f"Skipping {'startup' if is_startup else 'periodic'} REST inventory reconciliation: fill ingestion "
                f"in-flight ({self._fill_count - start_fill_count} committed, pending={self._pending_fills}). "
                f"Real-time WebSocket state is authoritative."
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
        if self.fill_store is not None:
            if self._fill_spool is not None:
                raise RuntimeError("fill ingestion already started")
            path = os.environ.get("FILL_SPOOL_PATH", "data/fill_spool.sqlite3")
            try:
                self._fill_spool = await asyncio.to_thread(FillSpool, path)
                if await self._spool_call(self._fill_spool.dirty):
                    raise RuntimeError("spool requires reconciliation")
                await self._spool_call(self._fill_spool.set_dirty)
            except Exception as exc:
                self._fill_failure = exc
                if self._fill_spool is not None:
                    await self._spool_call(self._fill_spool.close)
                    self._fill_spool = None
                raise FatalIngestionError("fill spool requires reconciliation or repair before restart") from exc
            self._fill_init_task = asyncio.create_task(self._initialize_fill_store(self.fill_store))
            self._fill_worker = asyncio.create_task(self._persist_fills())
        await self.ws_client.subscribe(["fill"])

    async def _initialize_fill_store(self, store):
        try:
            if not await asyncio.to_thread(store.initialize):
                logger.error("Fill database initialization failed; live persistence will retry")
        except Exception:
            logger.exception("Fill database initialization failed; live persistence will retry")

    async def _spool_call(self, operation, *args):
        async with self._spool_lock:
            task = asyncio.create_task(asyncio.to_thread(operation, *args))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    async def _persist_fills(self):
        try:
            await asyncio.shield(self._fill_init_task)
            cursor = 0
            while not self._fill_stopping:
                row = await self._spool_call(self._fill_spool.pending, cursor)
                if row is None:
                    cursor = 0
                    self._fill_wakeup.clear()
                    try:
                        await asyncio.wait_for(self._fill_wakeup.wait(), self._fill_retry_interval)
                    except asyncio.TimeoutError:
                        pass
                    continue
                row_id, fill = row
                try:
                    success = await asyncio.to_thread(self.fill_store.record, fill, "websocket")
                except Exception:
                    logger.exception("WebSocket fill persistence failed")
                    success = False
                if success:
                    await self._spool_call(self._fill_spool.acknowledge, row_id)
                else:
                    logger.error("WebSocket fill persistence failed; retained in spool")
                    if not self._fill_stopping:
                        try:
                            await asyncio.to_thread(self.fill_store.initialize)
                        except Exception:
                            logger.exception("Fill database initialization failed")
                if self._fill_stopping:
                    break
                cursor = row_id
        except Exception as exc:
            self._fill_failure = exc
            logger.exception("Fatal fill spool failure")
            await self.ws_client.fail_ingestion(exc)

    async def close_fill_ingestion(self, clean=False, timeout: float = 5.0):
        """Stop replay and release the spool after WebSocket ingestion has stopped."""
        self._fill_stopping = True
        self._fill_wakeup.set()
        drained = True
        if self._fill_worker is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._fill_worker), timeout=timeout)
            except (asyncio.TimeoutError, TimeoutError):
                logger.error("Timed out waiting for fill worker to drain; spool remains dirty.")
                drained = False
                clean = False
        if self._fill_spool is not None:
            if clean and drained and self._fill_failure is None and self.ws_client.__dict__.get("_ingestion_failure") is None:
                await self._spool_call(self._fill_spool.clear_dirty)
            if drained:
                await self._spool_call(self._fill_spool.close)
                self._fill_spool = None
            else:
                logger.warning("Retaining active spool file descriptor due to undrained worker.")
        return drained and clean

    async def close_spool_deferred(self):
        """Close spool after deferred worker finish."""
        if self._fill_spool is not None:
            try:
                await self._spool_call(self._fill_spool.close)
            finally:
                self._fill_spool = None

    async def _handle_message(self, message: Dict[str, Any]):
        msg_type = message.get("type")
        
        if msg_type == "fill":
            fill = message.get("msg", {})
            if self._fill_failure is not None or self._fill_stopping:
                raise FatalIngestionError("fill ingestion unavailable")
            self._pending_fills += 1
            try:
                if self.fill_store is not None:
                    if self._fill_spool is None or self._fill_failure or self._fill_stopping:
                        raise FatalIngestionError("fill spool unavailable")
                    try:
                        await self._spool_call(self._fill_spool.append, fill)
                    except asyncio.CancelledError as exc:
                        self._fill_failure = exc
                        raise
                    except Exception as exc:
                        self._fill_failure = exc
                        raise FatalIngestionError("failed to durably spool fill") from exc
                    self._fill_wakeup.set()
                try:
                    if not isinstance(fill, dict):
                        raise ValueError("fill payload must be an object")
                    identities = []
                    for key in ("fill_id", "trade_id"):
                        value = fill.get(key)
                        if value is not None:
                            if not isinstance(value, str) or not value.strip():
                                raise ValueError(f"invalid {key}")
                            identities.append((key, value))
                    if not any(key == "trade_id" for key, _ in identities):
                        raise ValueError("WebSocket fill requires trade_id")
                    content = {key: value for key, value in fill.items() if key not in ("fill_id", "trade_id")}
                    existing = [self._seen_fill_ids[identity] for identity in identities if identity in self._seen_fill_ids]
                    if existing:
                        if any(previous["content"] != content or any(
                            key in previous["ids"] and previous["ids"][key] != value
                            for key, value in identities
                        ) for previous in existing) or any(previous is not existing[0] for previous in existing):
                            raise ValueError("conflicting fill identity")
                        existing[0]["ids"].update(identities)
                        self._seen_fill_ids.update({identity: existing[0] for identity in identities})
                        return
                    count_before = self._fill_count
                    self._handle_fill(fill)
                    if self._fill_count == count_before:
                        raise ValueError("invalid inventory fill")
                    record = {"content": content, "ids": dict(identities)}
                    self._seen_fill_ids.update({identity: record for identity in identities})
                except Exception as exc:
                    self._fill_failure = exc
                    raise FatalIngestionError("failed to process inventory fill") from exc
            finally:
                self._pending_fills -= 1

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
        now = time.time()
        self._last_fill_time = now
        self._last_fill_times[ticker] = now
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
            KALSHI_PORTFOLIO_VALUE_CENTS.set(self.get_portfolio_value(ticker))
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
            KALSHI_PORTFOLIO_VALUE_CENTS.set(self.get_portfolio_value(ticker))
        except Exception as e:
            logger.debug(f"Prometheus unrealized/portfolio metric update skipped: {e}")
        return unrealized

    def _ticker_value(self, ticker: str) -> Optional[float]:
        """Calculates live mark-to-market value in cents for a single ticker's inventory."""
        pos = self.positions.get(ticker, 0)
        mid = self._last_mid_prices.get(ticker)
        if pos == 0:
            return 0.0
        if mid is None:
            return None
        return round(pos * mid, 4) if pos > 0 else round(abs(pos) * (100.0 - mid), 4)

    def get_positions_value(self, ticker: Optional[str] = None) -> float:
        """
        Returns estimated market value of open positions in cents.
        If a ticker is specified and has a known mid-price, computes live mark-to-market.
        Otherwise falls back to the REST portfolio_value or marked inventory across tickers.
        """
        if ticker:
            val = self._ticker_value(ticker)
            if val is not None:
                return val
            return 0.0

        # If no positions are held across any ticker, positions value is 0.0
        if not self.positions or all(v == 0 for v in self.positions.values()):
            return 0.0

        vals = [self._ticker_value(t) for t in self.positions]
        if any(v is None for v in vals):
            return self.positions_value_cents
        return round(sum(vals), 4)

    def get_portfolio_value(self, ticker: Optional[str] = None) -> float:
        """Returns total portfolio value in cents (cash balance plus open positions market value)."""
        return round(self.balance_cents + self.get_positions_value(), 4)

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
        """Returns the net inventory position for a given ticker (+ for YES, - for NO)."""
        return self.positions.get(ticker, 0)

    def is_flat(self, ticker: str) -> bool:
        """
        Returns True if net position is effectively flat (< 0.5 contracts),
        accounting for fractional REST reporting artifacts or sub-contract residuals.
        """
        return abs(self.get_position(ticker)) < 0.5
        
    def get_balance(self) -> int:
        """Returns current available cash balance in cents."""
        return self.balance_cents
