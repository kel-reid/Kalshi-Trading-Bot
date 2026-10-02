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
# Default: Gamma 0.7 (Risk Aversion), 4 cent minimum spread, $1.00 minimum dynamic order size
RISK_GAMMA = float(os.getenv("RISK_GAMMA", "0.7"))
MIN_SPREAD = int(os.getenv("MIN_SPREAD", "4"))
ORDER_SIZE = int(os.getenv("ORDER_SIZE", "1"))
ORDER_DOLLARS = max(1.0, _get_float_env("ORDER_DOLLARS", 1.0))
MAX_ORDER_CONTRACTS = _get_int_env("MAX_ORDER_CONTRACTS", 100)
MAX_HEDGE_INVENTORY = _get_int_env("MAX_HEDGE_INVENTORY", 250)
TARGET_TICKER = os.getenv("TARGET_TICKER", "") # Can be injected to force a specific market

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
# Supports either EXPIRATION_BUFFER_MINUTES (Doppler/env) or MIN_TIME_TO_CLOSE_SECONDS (default 90 mins = 5400s).
if os.getenv("EXPIRATION_BUFFER_MINUTES"):
    EXPIRATION_BUFFER_MINUTES = _get_int_env("EXPIRATION_BUFFER_MINUTES", 90, allow_zero=True)
    MIN_TIME_TO_CLOSE_SECONDS = EXPIRATION_BUFFER_MINUTES * 60
else:
    MIN_TIME_TO_CLOSE_SECONDS = _get_int_env("MIN_TIME_TO_CLOSE_SECONDS", 5400, allow_zero=True)
    EXPIRATION_BUFFER_MINUTES = MIN_TIME_TO_CLOSE_SECONDS // 60

MAX_EXPIRATION_DAYS = _get_float_env("MAX_EXPIRATION_DAYS", 8.0) # Rolling window (days) for automated sports discovery

# Fee Churn & Session Stop-Loss Safeguards (Cents)
# Cease quoting, liquidate inventory, and rotate away if fees or net loss on a single market session exceed these thresholds.
MAX_SESSION_FEES_CENTS = _get_int_env("MAX_SESSION_FEES_CENTS", 250)
MAX_SESSION_LOSS_CENTS = _get_int_env("MAX_SESSION_LOSS_CENTS", 300)

# Post-Fill Adverse Selection Protection (Seconds)
# Pause quoting after an execution fill to allow orderbook stabilization during rapid information jumps.
POST_FILL_PAUSE_SECONDS = _get_float_env("POST_FILL_PAUSE_SECONDS", 3.0, allow_zero=True)

# Database Configurations (PostgreSQL)
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "kalshi_bot")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")

