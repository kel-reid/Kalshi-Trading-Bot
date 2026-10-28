import math
import os
from pathlib import Path
from dotenv import load_dotenv

# Automatically load environment variables from a .env file if present
load_dotenv()

# Project root directory
PROJECT_ROOT = Path(__file__).resolve().parent

# Environment (Options: "demo" or "prod")
ENVIRONMENT = os.getenv("KALSHI_ENV", "prod")

# Base URLs
if ENVIRONMENT == "prod":
    BASE_URL = "https://api.elections.kalshi.com"
else:
    BASE_URL = "https://demo-api.kalshi.co"

# API Authentication configuration
API_KEY = os.getenv("KALSHI_API_KEY")

if not API_KEY:
    raise ValueError("KALSHI_API_KEY environment variable is not set!")

# Private key path
# Use a different default key file for production, or allow overriding via environment variable
default_key_filename = "kalshi_private_key_prod.pem" if ENVIRONMENT == "prod" else "kalshi_private_key_demo.pem"
PRIVATE_KEY_PATH = Path(os.getenv("KALSHI_PRIVATE_KEY_PATH", PROJECT_ROOT / default_key_filename))

# Verify private key file exists on startup to fail-fast (bypassed in pytest or if key is provided via env)
import sys
has_env_key = bool(os.getenv("KALSHI_PRIVATE_KEY"))
if "pytest" not in sys.modules and not has_env_key and not PRIVATE_KEY_PATH.exists():
    raise FileNotFoundError(
        f"Kalshi private key file not found at {PRIVATE_KEY_PATH}! "
        f"Please check your KALSHI_PRIVATE_KEY_PATH environment variable."
    )

# Alerting
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL")

def _get_float_env(name: str, default: float, allow_zero: bool = False) -> float:
    val = os.getenv(name)
    if val is None or not str(val).strip():
        return default
    try:
        f = float(val)
        if not math.isfinite(f):
            return default
        if allow_zero:
            if f < 0.0:
                return default
        else:
            if f <= 0.0:
                return default
        return f
    except (ValueError, TypeError):
        return default


def _get_int_env(name: str, default: int, allow_zero: bool = False) -> int:
    val = os.getenv(name)
    if val is None or not str(val).strip():
        return default
    try:
        i = int(str(val).strip())
    except (ValueError, TypeError) as err:
        expected = "a non-negative integer" if allow_zero else "a positive integer"
        raise ValueError(f"{name} must be {expected}, got {val!r}") from err
    if allow_zero:
        if i < 0:
            raise ValueError(f"{name} must be a non-negative integer, got {val!r}")
    else:
        if i <= 0:
            raise ValueError(f"{name} must be a positive integer, got {val!r}")
    return i



# Strategy Tuning Parameters
# Default: Gamma 1.2 (Risk Aversion), 7 cent minimum spread, $3.00 minimum dynamic order size
RISK_GAMMA = float(os.getenv("RISK_GAMMA", "1.2"))
MIN_SPREAD = int(os.getenv("MIN_SPREAD", "7"))
ORDER_SIZE = int(os.getenv("ORDER_SIZE", "1"))
ORDER_DOLLARS = max(1.0, _get_float_env("ORDER_DOLLARS", 3.0))
MAX_ORDER_CONTRACTS = _get_int_env("MAX_ORDER_CONTRACTS", 100)
MAX_HEDGE_INVENTORY = _get_int_env("MAX_HEDGE_INVENTORY", 250)

# Collateral-Constrained Sizing Ceiling on Low-Price Contracts
# Prevents excessive contract sizing on low-price contracts (< 25c) where short leg requires heavy NO collateral
MAX_LOW_PRICE_CONTRACTS = _get_int_env("MAX_LOW_PRICE_CONTRACTS", 10, allow_zero=True)
LOW_PRICE_THRESHOLD_CENTS = _get_int_env("LOW_PRICE_THRESHOLD_CENTS", 25, allow_zero=True)

# Aggressive Momentum Quote Skewing
# Increases inventory penalty gamma during multi-possession runs and adverse price drift
MOMENTUM_SKEW_ENABLED = os.getenv("MOMENTUM_SKEW_ENABLED", "true").lower() in ("true", "1", "yes")
MOMENTUM_GAMMA_FACTOR = _get_float_env("MOMENTUM_GAMMA_FACTOR", 0.5, allow_zero=True)
MAX_GAMMA_MULTIPLIER = max(1.0, _get_float_env("MAX_GAMMA_MULTIPLIER", 2.5))

# Quote Replacement Tolerance (Cents)
# Prevents quote replacement churn and API fee/rate-limit waste when midpoint shift is within tolerance
QUOTE_REPLACEMENT_TOLERANCE_CENTS = _get_int_env("QUOTE_REPLACEMENT_TOLERANCE_CENTS", 1, allow_zero=True)

# Automated Inter-Shard Capital Sweep (Bidirectional)
# Automatically sweeps idle capital from Shard 0 to active sports shard (Shard 3)
# and sweeps surplus capital back from Shard 3 to Shard 0 on rotation, shutdown, or when idle.
AUTO_SWEEP_SHARD0_ENABLED = os.getenv("AUTO_SWEEP_SHARD0_ENABLED", "true").lower() in ("true", "1", "yes")
AUTO_SWEEP_REVERSE_ENABLED = os.getenv("AUTO_SWEEP_REVERSE_ENABLED", "true").lower() in ("true", "1", "yes")
SHARD0_RESERVE_DOLLARS = _get_float_env("SHARD0_RESERVE_DOLLARS", 1.0, allow_zero=True)
SHARD_RETAIN_DOLLARS = _get_float_env("SHARD_RETAIN_DOLLARS", 10.0, allow_zero=True)

_raw_target = os.getenv("TARGET_TICKER") or os.getenv("TARGET_PREFERENCE") or ""
TARGET_TICKER = _raw_target  # Can be injected to force a specific market or league
TARGET_PREFERENCE = _raw_target

# Dynamic Volatility & Fee-Aware Spread Engine (Phase 1 & Phase 2)
# Replaces static MIN_SPREAD with real-time Avellaneda-Stoikov volatility spread
# bounded from below by the exact round-trip Kalshi fee floor + target edge.
DYNAMIC_SPREAD_ENABLED = os.getenv("DYNAMIC_SPREAD_ENABLED", "true").lower() in ("true", "1", "yes")
TARGET_EDGE_CENTS = _get_float_env("TARGET_EDGE_CENTS", 1.5, allow_zero=True)
MIN_DYNAMIC_SPREAD_CENTS = _get_int_env("MIN_DYNAMIC_SPREAD_CENTS", 2)
MAX_DYNAMIC_SPREAD_CENTS = _get_int_env("MAX_DYNAMIC_SPREAD_CENTS", 15)
VOLATILITY_WINDOW_SECONDS = _get_float_env("VOLATILITY_WINDOW_SECONDS", 30.0)
KALSHI_FEE_RATE = _get_float_env("KALSHI_FEE_RATE", 0.07)

# Extreme Price Collar Safeguards (Cents)
# In binary markets ($0-$1.00), quoting outside safe collars (e.g. < 10c or > 90c) introduces
# asymmetric adverse selection and settlement-at-zero holding risk.
MIN_MID_PRICE = _get_int_env("MIN_MID_PRICE", 10)
MAX_MID_PRICE = _get_int_env("MAX_MID_PRICE", 90)
if MIN_MID_PRICE < 1 or MAX_MID_PRICE > 99 or MIN_MID_PRICE >= MAX_MID_PRICE:
    raise ValueError(
        f"Invalid price collar configuration: MIN_MID_PRICE ({MIN_MID_PRICE}) "
        f"must be strictly less than MAX_MID_PRICE ({MAX_MID_PRICE}) and within [1, 99]."
    )

# Expiration Cutoff Safeguard (Seconds / Minutes)
# Cease quoting, liquidate inventory, and rotate away when market close_time is within this buffer.
# Supports either EXPIRATION_BUFFER_MINUTES (Doppler/env) or MIN_TIME_TO_CLOSE_SECONDS (default 45 mins = 2700s).
if os.getenv("EXPIRATION_BUFFER_MINUTES"):
    EXPIRATION_BUFFER_MINUTES = _get_int_env("EXPIRATION_BUFFER_MINUTES", 45, allow_zero=True)
    MIN_TIME_TO_CLOSE_SECONDS = EXPIRATION_BUFFER_MINUTES * 60
else:
    MIN_TIME_TO_CLOSE_SECONDS = _get_int_env("MIN_TIME_TO_CLOSE_SECONDS", 2700, allow_zero=True)
    EXPIRATION_BUFFER_MINUTES = MIN_TIME_TO_CLOSE_SECONDS // 60

# Sport-Specific Expiration Cutoff Buffers (Minutes)
# Real-world sports durations differ widely (NBA: ~2h, MLB: ~2.5h, NFL: ~3.25h, NCAAF: ~3.5-4h).
# Dedicated buffers prevent premature mid-game shutdowns in fast sports (e.g. NBA 4th quarter).
EXPIRATION_BUFFER_MINUTES_NBA = _get_int_env("EXPIRATION_BUFFER_MINUTES_NBA", 15, allow_zero=True)
EXPIRATION_BUFFER_MINUTES_MLB = _get_int_env("EXPIRATION_BUFFER_MINUTES_MLB", 30, allow_zero=True)
EXPIRATION_BUFFER_MINUTES_NFL = _get_int_env("EXPIRATION_BUFFER_MINUTES_NFL", 40, allow_zero=True)
EXPIRATION_BUFFER_MINUTES_NCAAF = _get_int_env("EXPIRATION_BUFFER_MINUTES_NCAAF", 45, allow_zero=True)

SPORT_EXPIRATION_BUFFERS_MINUTES = {
    "NBA": EXPIRATION_BUFFER_MINUTES_NBA,
    "MLB": EXPIRATION_BUFFER_MINUTES_MLB,
    "NFL": EXPIRATION_BUFFER_MINUTES_NFL,
    "NCAAF": EXPIRATION_BUFFER_MINUTES_NCAAF,
}


def get_expiration_buffer_seconds(ticker: Optional[str] = None) -> int:
    """
    Returns the appropriate expiration cutoff buffer in seconds based on market ticker.
    Matches sport prefixes (e.g. KXNBA -> 15m, KXMLB -> 30m, KXNFL -> 40m, KXNCAAF -> 45m),
    falling back to MIN_TIME_TO_CLOSE_SECONDS for non-sports or unspecified tickers.
    """
    if ticker:
        ticker_upper = ticker.upper()
        # Check NCAAF first before NFL since KXNCAAF contains NCAAF
        if "KXNCAAF" in ticker_upper or "_NCAAF" in ticker_upper:
            return SPORT_EXPIRATION_BUFFERS_MINUTES["NCAAF"] * 60
        for league in ("NBA", "MLB", "NFL"):
            if f"KX{league}" in ticker_upper or f"_{league}" in ticker_upper:
                return SPORT_EXPIRATION_BUFFERS_MINUTES[league] * 60
    return MIN_TIME_TO_CLOSE_SECONDS

MAX_EXPIRATION_DAYS = _get_float_env("MAX_EXPIRATION_DAYS", 8.0) # Rolling window (days) for automated sports discovery

# Trading Blackout Safeguards (Late-Game & Binary Danger Zone)
# Prevents entering toxic inventory traps during late-game blowouts and extreme collar approaches.
BLACKOUT_ENABLED = os.getenv("BLACKOUT_ENABLED", "true").lower() in ("true", "1", "yes")
EXPIRATION_BLACKOUT_SECONDS = _get_int_env("EXPIRATION_BLACKOUT_SECONDS", 1800, allow_zero=True)
BLACKOUT_DANGER_PRICE_LOW = _get_int_env("BLACKOUT_DANGER_PRICE_LOW", 20, allow_zero=True)
BLACKOUT_DANGER_PRICE_HIGH = _get_int_env("BLACKOUT_DANGER_PRICE_HIGH", 80, allow_zero=True)

# Fee Churn & Session Stop-Loss Safeguards (Cents)
# Cease quoting, liquidate inventory, and rotate away if fees or net loss on a single market session exceed these thresholds.
MAX_SESSION_FEES_CENTS = _get_int_env("MAX_SESSION_FEES_CENTS", 750)
MAX_SESSION_LOSS_CENTS = _get_int_env("MAX_SESSION_LOSS_CENTS", 900)

# Post-Fill Adverse Selection Protection (Seconds)
# Pause quoting after an execution fill to allow orderbook stabilization during rapid information jumps.
POST_FILL_PAUSE_SECONDS = _get_float_env("POST_FILL_PAUSE_SECONDS", 8.0, allow_zero=True)

# Fast Market / Price Velocity Circuit Breaker Safeguards
# Automatically withdraw resting quotes when orderbook midpoint shifts faster than threshold over rolling window.
PRICE_VELOCITY_THRESHOLD_CENTS = _get_float_env("PRICE_VELOCITY_THRESHOLD_CENTS", 4.0, allow_zero=True)
PRICE_VELOCITY_WINDOW_SECONDS = _get_float_env("PRICE_VELOCITY_WINDOW_SECONDS", 20.0, allow_zero=True)
PRICE_VELOCITY_QUIESCE_SECONDS = _get_float_env("PRICE_VELOCITY_QUIESCE_SECONDS", 30.0, allow_zero=True)

# Scheduled Slate Re-Evaluation & Market Dwell Safeguards (Seconds)
# Periodically re-evaluate whether current market is off-slate/distant while inventory is flat.
SLATE_REEVALUATION_INTERVAL_SECONDS = _get_int_env("SLATE_REEVALUATION_INTERVAL_SECONDS", 1800)
MIN_MARKET_DWELL_SECONDS = _get_int_env("MIN_MARKET_DWELL_SECONDS", 1800, allow_zero=True)

# Database Configurations (PostgreSQL)
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "kalshi_bot")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")

