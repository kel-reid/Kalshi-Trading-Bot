"""
Orderbook Manager

This module tracks and maintains the real-time L2 order book (bids and asks) 
for the selected prediction markets. It processes full orderbook snapshots 
and incremental delta updates received from the Kalshi WebSocket feeds.
"""

import logging
from typing import Dict, Any, Optional

logger = logging.getLogger("OrderbookManager")


def _parse_dollar_price_to_cents(price: Any) -> Optional[float]:
    """Parse Kalshi v2 dollar-denominated price (e.g. '0.3200', '1.0000', 0.5) to exact cents."""
    if price is None:
        return None
    try:
        val = float(str(price).strip())
        return round(val * 100.0, 4)
    except (ValueError, TypeError):
        return None


def _parse_legacy_price_to_cents(price: Any) -> Optional[float]:
    """Parse legacy cent-denominated price (e.g. 50, 50.0, '50') to exact cents."""
    if price is None:
        return None
    try:
        return float(str(price).strip())
    except (ValueError, TypeError):
        return None


def _normalize_price_to_cents(price: Any, is_dollars: Optional[bool] = None) -> Optional[float]:
    """
    Normalize a price value to cents with sub-cent precision.
    When is_dollars is explicitly provided, avoids ambiguous magnitude heuristics.
    If is_dollars is None, treats prices <= 1.0 with decimal points or < 1.0 as dollar-denominated,
    correctly treating '1.0000' as $1.00 = 100.0 cents while preserving legacy cents like 50.0.
    """
    if price is None:
        return None
    if is_dollars is True:
        return _parse_dollar_price_to_cents(price)
    elif is_dollars is False:
        return _parse_legacy_price_to_cents(price)

    # Heuristic fallback for legacy test callers where schema flag is unspecified
    try:
        p_str = str(price).strip()
        p_val = float(p_str)
        if p_val <= 1.0 and ("." in p_str or p_val < 1.0):
            return round(p_val * 100.0, 4)
        return p_val
    except (ValueError, TypeError):
        return None


def _normalize_qty(qty: Any) -> float:
    """Normalize orderbook quantity to float, preserving fractional sizes without loss."""
    if qty is None:
        return 0.0
    try:
        val = float(qty)
        return round(val, 6) if val > 0 else 0.0
    except (ValueError, TypeError):
        return 0.0


class OrderbookManager:
    """
    Maintains a real-time L2 order book for tracked Kalshi markets.
    Supports both Kalshi v2 dollar-formatted feeds (yes_dollars_fp, price_dollars)
    and legacy cent feeds, preserving sub-cent price precision and fractional quantities.
    """
    def __init__(self, ws_client):
        self.ws_client = ws_client
        # Format: self.books[ticker][side][price_cents] = quantity
        # side: "yes" or "no"
        self.books: Dict[str, Dict[str, Dict[float, float]]] = {}
        
        # Register the message handler with the websocket client
        self.ws_client.add_message_handler(self._handle_message)

    async def subscribe(self, tickers: list[str]):
        """Subscribe to orderbook deltas for the given tickers."""
        # Initialize empty books for new tickers
        for ticker in tickers:
            if ticker not in self.books:
                self.books[ticker] = {"yes": {}, "no": {}}
        
        await self.ws_client.subscribe(["orderbook_delta"], tickers)

    async def unsubscribe(self, tickers: list[str]):
        """Unsubscribe from orderbook deltas for the given tickers and clean up cached books."""
        for ticker in tickers:
            self.books.pop(ticker, None)
        await self.ws_client.unsubscribe(["orderbook_delta"], tickers)

    async def _handle_message(self, message: Dict[str, Any]):
        msg_type = message.get("type")
        
        if msg_type == "orderbook_snapshot":
            self._handle_snapshot(message.get("msg", {}))
        elif msg_type == "orderbook_delta":
            self._handle_delta(message.get("msg", {}))

    def _handle_snapshot(self, msg: Dict[str, Any]):
        ticker = msg.get("market_ticker")
        if not ticker:
            return
            
        if ticker not in self.books:
            self.books[ticker] = {"yes": {}, "no": {}}
            
        logger.info(f"Received orderbook snapshot for {ticker}")
        
        # Load snapshot bids/asks ("yes" and "no", or v2 "yes_dollars_fp" and "no_dollars_fp")
        for side in ["yes", "no"]:
            dollar_levels = msg.get(f"{side}_dollars_fp") or msg.get(f"{side}_dollars")
            if dollar_levels is not None:
                levels = dollar_levels
                is_dollars = True
            else:
                levels = msg.get(side, [])
                is_dollars = False

            side_book: Dict[float, float] = {}
            for item in levels:
                if len(item) >= 2:
                    price_cents = _normalize_price_to_cents(item[0], is_dollars=is_dollars)
                    qty = _normalize_qty(item[1])
                    if price_cents is not None and qty > 0 and 0.0 < price_cents <= 100.0:
                        side_book[price_cents] = qty
            self.books[ticker][side] = side_book

    def _handle_delta(self, msg: Dict[str, Any]):
        ticker = msg.get("market_ticker")
        if not ticker or ticker not in self.books:
            return
            
        side = msg.get("side") # "yes" or "no"
        if side not in self.books[ticker]:
            return

        is_dollars = "price_dollars" in msg
        raw_price = msg.get("price_dollars") if is_dollars else msg.get("price")
        raw_delta = msg.get("delta_fp") if msg.get("delta_fp") is not None else msg.get("delta")
        
        if raw_price is None or raw_delta is None:
            return

        price_cents = _normalize_price_to_cents(raw_price, is_dollars=is_dollars)
        if price_cents is None or not (0.0 < price_cents <= 100.0):
            return

        try:
            delta = float(raw_delta)
        except (ValueError, TypeError):
            return
            
        current_qty = self.books[ticker][side].get(price_cents, 0.0)
        new_qty = round(current_qty + delta, 6)
        
        if new_qty <= 1e-9:
            self.books[ticker][side].pop(price_cents, None)
        else:
            self.books[ticker][side][price_cents] = new_qty

    def get_best_bid(self, ticker: str) -> Optional[tuple[float, float]]:
        """
        Returns the best bid for 'yes' shares as (price, quantity).
        The best bid is the highest price someone is willing to buy 'yes' shares at.
        """
        if ticker not in self.books or not self.books[ticker]["yes"]:
            return None
        # Highest price on 'yes' side
        best_price = max(self.books[ticker]["yes"].keys())
        return best_price, self.books[ticker]["yes"][best_price]

    def get_best_ask(self, ticker: str) -> Optional[tuple[float, float]]:
        """
        Returns the best ask for 'yes' shares as (price, quantity).
        On Kalshi, buying 'no' at price P is equivalent to selling 'yes' at 100 - P.
        So to find the best ask for YES, we find the highest NO bid price P, 
        and the YES ask price is 100 - P.
        """
        if ticker not in self.books or not self.books[ticker]["no"]:
            return None
            
        # Highest price on 'no' side
        highest_no_bid = max(self.books[ticker]["no"].keys())
        implied_ask_price = round(100.0 - highest_no_bid, 4)
        
        return implied_ask_price, self.books[ticker]["no"][highest_no_bid]
