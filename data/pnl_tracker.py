"""
Real-Time PnL Tracker and Trade Attribution Engine

This module implements FIFO (First-In, First-Out) lot matching for executed contracts,
distinguishing between Realized PnL (locked in upon position closing) and
Unrealized PnL (mark-to-market against live orderbook mid-price), while tracking
exchange fees and session attribution.
"""

import time
import uuid
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any

logger = logging.getLogger("PnLTracker")


@dataclass
class InventoryLot:
    """Represents an open contract lot awaiting offset."""
    lot_id: str
    ticker: str
    action: str  # "buy" (long) or "sell" (short)
    price_cents: float
    count: int
    timestamp: float


@dataclass
class MarketPnL:
    """Maintains PnL and open lots for a single market ticker."""
    ticker: str
    open_lots: List[InventoryLot] = field(default_factory=list)
    realized_pnl_cents: float = 0.0
    unrealized_pnl_cents: float = 0.0
    total_fees_cents: float = 0.0
    round_trips_count: int = 0
    winning_trades_count: int = 0
    losing_trades_count: int = 0
    scratch_trades_count: int = 0
    total_volume_contracts: int = 0
    last_mid_price: Optional[float] = None
    rotation_session_id: str = field(default_factory=lambda: str(uuid.uuid4()))


class PnLTracker:
    """
    Tracks real-time Realized and Unrealized PnL across markets using FIFO lot matching.
    """

    def __init__(self):
        self.markets: Dict[str, MarketPnL] = {}

    def get_or_create_market(self, ticker: str) -> MarketPnL:
        """Retrieves or initializes PnL state for a market ticker."""
        if ticker not in self.markets:
            self.markets[ticker] = MarketPnL(ticker=ticker)
        return self.markets[ticker]

    def reset_market_session(self, ticker: str, new_session_id: Optional[str] = None) -> str:
        """Starts a new tracking session for a market (e.g. upon rotation)."""
        market = self.get_or_create_market(ticker)
        market.rotation_session_id = new_session_id or str(uuid.uuid4())
        return market.rotation_session_id

    @staticmethod
    def normalize_fill(action: str, side: str, price_cents: float) -> Tuple[str, float]:
        """
        Normalizes contract fills to YES-equivalent direction and pricing.
        - Buy YES at P -> Buy YES at P
        - Sell YES at P -> Sell YES at P
        - Buy NO at P  -> Sell YES at (100 - P)
        - Sell NO at P -> Buy YES at (100 - P)
        """
        norm_action = action.lower()
        norm_side = side.lower()
        price = float(price_cents)

        if norm_side == "no":
            norm_action = "sell" if norm_action == "buy" else "buy"
            norm_price = 100.0 - price
        else:
            norm_price = price

        return norm_action, norm_price

    def record_fill(
        self,
        ticker: str,
        action: str,
        side: str,
        count: int,
        price_cents: float,
        fee_cents: float = 0.0,
        timestamp: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Processes a fill through the FIFO queue, matching offsetting inventory
        and computing realized gain/loss.

        Returns a dictionary summarizing the fill execution impact.
        """
        if count <= 0:
            return {}

        now = timestamp or time.time()
        market = self.get_or_create_market(ticker)
        market.total_volume_contracts += count
        market.total_fees_cents += float(fee_cents)

        norm_action, norm_price = self.normalize_fill(action, side, price_cents)
        remaining_count = count
        realized_delta = 0.0

        # Opposing action that would be closed by this fill:
        # If this fill is 'sell', it closes open 'buy' lots.
        # If this fill is 'buy', it closes open 'sell' lots.
        target_close_action = "buy" if norm_action == "sell" else "sell"

        # FIFO matching against open lots
        new_open_lots: List[InventoryLot] = []
        for lot in market.open_lots:
            if remaining_count > 0 and lot.action == target_close_action:
                matched_count = min(remaining_count, lot.count)

                if lot.action == "buy":
                    # We were long at lot.price_cents, now selling at norm_price
                    trade_pnl = (norm_price - lot.price_cents) * matched_count
                else:
                    # We were short at lot.price_cents, now buying back at norm_price
                    trade_pnl = (lot.price_cents - norm_price) * matched_count

                realized_delta += trade_pnl
                market.round_trips_count += 1
                if trade_pnl > 0.0001:
                    market.winning_trades_count += 1
                elif trade_pnl < -0.0001:
                    market.losing_trades_count += 1
                else:
                    market.scratch_trades_count += 1

                lot.count -= matched_count
                remaining_count -= matched_count

                if lot.count > 0:
                    new_open_lots.append(lot)
            else:
                new_open_lots.append(lot)

        market.open_lots = new_open_lots

        # Deduct fees from realized gain
        realized_delta -= float(fee_cents)
        market.realized_pnl_cents += realized_delta

        # If any contracts remain after closing existing lots, open a new lot
        if remaining_count > 0:
            new_lot = InventoryLot(
                lot_id=str(uuid.uuid4())[:8],
                ticker=ticker,
                action=norm_action,
                price_cents=norm_price,
                count=remaining_count,
                timestamp=now
            )
            market.open_lots.append(new_lot)

        # Recalculate unrealized PnL if mid-price is known
        if market.last_mid_price is not None:
            self.update_mid_price(ticker, market.last_mid_price)

        logger.info(
            f"PnL Fill [{ticker}]: {action.upper()} {count} {side.upper()} @ {price_cents}c | "
            f"Realized Delta: {realized_delta:+.2f}c | Cum Realized: {market.realized_pnl_cents:+.2f}c | "
            f"Open Lots: {len(market.open_lots)} ({sum(l.count for l in market.open_lots)} contracts)"
        )

        return {
            "ticker": ticker,
            "matched_contracts": count - remaining_count,
            "new_contracts": remaining_count,
            "realized_delta_cents": realized_delta,
            "cumulative_realized_cents": market.realized_pnl_cents,
            "total_fees_cents": market.total_fees_cents,
            "open_lots_count": len(market.open_lots),
        }

    def update_mid_price(self, ticker: str, mid_price: float) -> float:
        """
        Updates mark-to-market unrealized PnL for open lots against current mid-price.
        Returns the updated unrealized PnL in cents.
        """
        market = self.get_or_create_market(ticker)
        market.last_mid_price = float(mid_price)

        unrealized = 0.0
        for lot in market.open_lots:
            if lot.action == "buy":
                # Long position marked to mid
                unrealized += (mid_price - lot.price_cents) * lot.count
            elif lot.action == "sell":
                # Short position marked to mid
                unrealized += (lot.price_cents - mid_price) * lot.count

        market.unrealized_pnl_cents = unrealized
        return unrealized

    def get_realized_pnl(self, ticker: str) -> float:
        """Returns cumulative realized PnL in cents for a market."""
        return self.get_or_create_market(ticker).realized_pnl_cents

    def get_unrealized_pnl(self, ticker: str) -> float:
        """Returns current unrealized mark-to-market PnL in cents for a market."""
        return self.get_or_create_market(ticker).unrealized_pnl_cents

    def get_total_fees(self, ticker: str) -> float:
        """Returns cumulative fees paid in cents for a market."""
        return self.get_or_create_market(ticker).total_fees_cents

    def get_open_inventory(self, ticker: str) -> int:
        """
        Returns net open contract inventory.
        Positive = net long YES, Negative = net short YES.
        """
        market = self.get_or_create_market(ticker)
        net_pos = 0
        for lot in market.open_lots:
            if lot.action == "buy":
                net_pos += lot.count
            elif lot.action == "sell":
                net_pos -= lot.count
        return net_pos

    def get_market_summary(self, ticker: str) -> Dict[str, Any]:
        """Returns a complete performance summary for a market."""
        market = self.get_or_create_market(ticker)
        total_pnl = market.realized_pnl_cents + market.unrealized_pnl_cents
        net_inventory = self.get_open_inventory(ticker)

        return {
            "ticker": ticker,
            "realized_pnl_cents": round(market.realized_pnl_cents, 2),
            "unrealized_pnl_cents": round(market.unrealized_pnl_cents, 2),
            "total_pnl_cents": round(total_pnl, 2),
            "total_fees_cents": round(market.total_fees_cents, 2),
            "net_inventory": net_inventory,
            "open_lots_count": len(market.open_lots),
            "round_trips_count": market.round_trips_count,
            "winning_trades": market.winning_trades_count,
            "losing_trades": market.losing_trades_count,
            "scratch_trades": market.scratch_trades_count,
            "volume_contracts": market.total_volume_contracts,
            "rotation_session_id": market.rotation_session_id,
            "last_mid_price": market.last_mid_price,
        }
