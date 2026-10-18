"""
Dynamic Volatility & Fee-Aware Spread Engine (Avellaneda-Stoikov Microstructure)

Calculates optimal bid-ask spreads dynamically based on:
1. Exact exchange transaction fee breakeven curves (Kalshi quadratic fee model).
2. Real-time realized midpoint price volatility over sliding time windows.
3. Orderbook depth and liquidity density (fill intensity).

Provides deterministic mathematical methods with zero side-effects to guarantee
conservative spread floors that preserve positive expected value after round-trip fees.
"""

from collections import deque
import math
import time
from typing import Deque, Optional, Tuple


# Default Kalshi fee rate (0.07 * P * (1 - P)) for standard retail contracts
DEFAULT_KALSHI_FEE_RATE = 0.07

# Default target edge in cents retained after round-trip fees
DEFAULT_TARGET_EDGE_CENTS = 2.0

# Minimum and maximum allowable dynamic spreads in cents
DEFAULT_MIN_DYNAMIC_SPREAD_CENTS = 2
DEFAULT_MAX_DYNAMIC_SPREAD_CENTS = 15

# Sliding window for realized volatility calculation (seconds)
DEFAULT_VOLATILITY_WINDOW_SECONDS = 30.0

# Minimum number of samples required to calculate rolling volatility
MIN_VOLATILITY_SAMPLES = 3

# Baseline default volatility (cents) when historical observations are insufficient
DEFAULT_BASELINE_VOLATILITY_CENTS = 1.0


def calculate_fee_cents(
    price_cents: float,
    fee_rate: float = DEFAULT_KALSHI_FEE_RATE,
) -> float:
    """
    Calculate the estimated single-leg exchange transaction fee in cents per contract.

    Kalshi's standard fee formula for prediction contracts scales quadratically
    with price P in [0, 1]:
        fee_dollars = fee_rate * P * (1.0 - P)
    In cents (where price_cents in [1, 99]):
        fee_cents = fee_rate * price_cents * (100.0 - price_cents) / 100.0

    Bounds price strictly to [1.0, 99.0]. Returns 0.0 for invalid or non-finite inputs.
    """
    if not isinstance(price_cents, (int, float)) or isinstance(price_cents, bool):
        return 0.0
    if not math.isfinite(price_cents) or price_cents <= 0.0 or price_cents >= 100.0:
        return 0.0

    rate = fee_rate if (isinstance(fee_rate, (int, float)) and math.isfinite(fee_rate) and fee_rate > 0.0) else DEFAULT_KALSHI_FEE_RATE
    bounded_p = max(1.0, min(99.0, float(price_cents)))
    raw_fee = rate * bounded_p * (100.0 - bounded_p) / 100.0
    return max(0.0, round(raw_fee, 4))


def calculate_spread_floor_cents(
    mid_price_cents: float,
    target_edge_cents: float = DEFAULT_TARGET_EDGE_CENTS,
    fee_rate: float = DEFAULT_KALSHI_FEE_RATE,
    min_floor_cents: int = DEFAULT_MIN_DYNAMIC_SPREAD_CENTS,
) -> int:
    """
    Calculate the minimum spread in integer cents required to clear round-trip
    exchange transaction fees plus a target profit edge:
        spread_floor = ceil(round_trip_fee + target_edge)

    Kalshi rounds transaction fees up to the next integer cent per execution fill leg.
    Guarantees that any filled round-trip (buy at bid, sell at ask) locks in
    net positive expected value >= target_edge_cents after fees.
    """
    if not isinstance(mid_price_cents, (int, float)) or isinstance(mid_price_cents, bool) or not math.isfinite(mid_price_cents):
        return max(1, min_floor_cents)

    edge = target_edge_cents if (isinstance(target_edge_cents, (int, float)) and math.isfinite(target_edge_cents) and target_edge_cents >= 0.0) else DEFAULT_TARGET_EDGE_CENTS
    one_way_fee = calculate_fee_cents(mid_price_cents, fee_rate=fee_rate)
    # Account for Kalshi integer cent fee rounding on each fill leg
    per_leg_fee = math.ceil(one_way_fee)
    round_trip_fee = 2 * per_leg_fee

    calculated_floor = math.ceil(round_trip_fee + edge)
    return max(min_floor_cents, calculated_floor)


class RollingVolatilityEstimator:
    """
    Sliding-window realized volatility estimator for orderbook midpoint prices.
    Maintains a time-indexed deque of observations over a configurable window.
    """

    def __init__(
        self,
        window_seconds: float = DEFAULT_VOLATILITY_WINDOW_SECONDS,
        min_samples: int = MIN_VOLATILITY_SAMPLES,
        baseline_volatility: float = DEFAULT_BASELINE_VOLATILITY_CENTS,
    ):
        self.window_seconds = max(5.0, float(window_seconds)) if math.isfinite(window_seconds) else DEFAULT_VOLATILITY_WINDOW_SECONDS
        self.min_samples = max(2, int(min_samples))
        self.baseline_volatility = max(0.1, float(baseline_volatility)) if math.isfinite(baseline_volatility) else DEFAULT_BASELINE_VOLATILITY_CENTS
        self._history: Deque[Tuple[float, float]] = deque()  # (timestamp, mid_price)

    def update(self, mid_price: float, timestamp: Optional[float] = None) -> None:
        """Record a new midpoint observation and prune expired entries."""
        if not isinstance(mid_price, (int, float)) or isinstance(mid_price, bool) or not math.isfinite(mid_price):
            return

        now = timestamp if (timestamp is not None and math.isfinite(timestamp)) else time.time()
        self._history.append((now, float(mid_price)))
        self._prune(now)

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()

    def sample_count(self) -> int:
        """Return the current number of valid samples in the window."""
        return len(self._history)

    def get_realized_volatility(self, now: Optional[float] = None) -> float:
        """
        Calculate sample standard deviation of consecutive midpoint price changes in cents.
        Falls back to baseline_volatility if observations are insufficient.
        """
        if now is not None and math.isfinite(now):
            self._prune(now)

        n = len(self._history)
        if n < self.min_samples:
            return self.baseline_volatility

        prices = [p for _, p in self._history]
        diffs = [b - a for a, b in zip(prices, prices[1:])]
        m = len(diffs)
        if m < 2:
            return self.baseline_volatility

        mean_d = sum(diffs) / m
        variance = sum((d - mean_d) ** 2 for d in diffs) / (m - 1)
        stdev = math.sqrt(variance)
        return max(0.1, round(stdev, 4))

    def reset(self) -> None:
        """Clear all historical observations."""
        self._history.clear()


def calculate_optimal_spread(
    mid_price_cents: float,
    realized_volatility_cents: float,
    gamma: float = 1.2,
    book_depth: int = 10,
    target_edge_cents: float = DEFAULT_TARGET_EDGE_CENTS,
    fee_rate: float = DEFAULT_KALSHI_FEE_RATE,
    min_spread_cents: int = DEFAULT_MIN_DYNAMIC_SPREAD_CENTS,
    max_spread_cents: int = DEFAULT_MAX_DYNAMIC_SPREAD_CENTS,
) -> int:
    """
    Compute optimal dynamic spread (in whole cents) combining the Avellaneda-Stoikov
    closed-form spread with the exact fee breakeven floor:

        s^* = gamma * sigma^2 * tau + (2 / gamma) * ln(1 + gamma / kappa)

    where:
    - gamma: risk aversion parameter (> 0)
    - sigma: realized midpoint volatility in cents
    - kappa: orderbook liquidity density / fill arrival intensity
    - Spread is bounded from below by calculate_spread_floor_cents(mid_price)
    - Spread is bounded from above by max_spread_cents
    """
    # 1. Base fee floor check
    spread_floor = calculate_spread_floor_cents(
        mid_price_cents=mid_price_cents,
        target_edge_cents=target_edge_cents,
        fee_rate=fee_rate,
        min_floor_cents=min_spread_cents,
    )

    safe_gamma = gamma if (isinstance(gamma, (int, float)) and math.isfinite(gamma) and gamma > 0.0) else 1.2
    safe_sigma = realized_volatility_cents if (isinstance(realized_volatility_cents, (int, float)) and math.isfinite(realized_volatility_cents) and realized_volatility_cents >= 0.0) else DEFAULT_BASELINE_VOLATILITY_CENTS
    safe_depth = max(1, int(book_depth)) if (isinstance(book_depth, (int, float)) and math.isfinite(book_depth)) else 10

    # Model liquidity arrival intensity kappa as proportional to visible orderbook depth
    kappa = max(0.1, safe_depth * 0.1)

    # Normalized horizon tau (sub-minute tick step = 1.0)
    tau = 1.0

    # Avellaneda-Stoikov optimal spread components:
    # 1) Volatility risk premium: gamma * sigma^2 * tau
    volatility_term = safe_gamma * (safe_sigma ** 2) * tau

    # 2) Liquidity / order arrival compensation: (2 / gamma) * ln(1 + gamma / kappa)
    arrival_term = (2.0 / safe_gamma) * math.log(1.0 + (safe_gamma / kappa))

    raw_as_spread = volatility_term + arrival_term
    as_spread_cents = math.ceil(raw_as_spread)

    # Take the maximum between the fee floor and the volatility-driven A-S spread
    optimal_spread = max(spread_floor, as_spread_cents)

    # The fee floor takes precedence over a conflicting upper bound
    return max(
        min_spread_cents,
        spread_floor,
        min(optimal_spread, max_spread_cents),
    )
