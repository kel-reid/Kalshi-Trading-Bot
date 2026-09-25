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

def _get_float_env(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None or not str(val).strip():
        return default
    try:
        f = float(val)
        if not math.isfinite(f) or f <= 0.0:
            return default
        return f
    except (ValueError, TypeError):
        return default


def _get_int_env(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or not str(val).strip():
        return default
    try:
        i = int(str(val).strip())
    except (ValueError, TypeError) as err:
        raise ValueError(f"{name} must be a positive integer, got {val!r}") from err
    if i <= 0:
        raise ValueError(f"{name} must be a positive integer, got {val!r}")
    return i



# Strategy Tuning Parameters
# Default: Gamma 0.5 (Risk Aversion), 4 cent minimum spread, $1.00 minimum dynamic order size
RISK_GAMMA = float(os.getenv("RISK_GAMMA", "0.5"))
MIN_SPREAD = int(os.getenv("MIN_SPREAD", "4"))
ORDER_SIZE = int(os.getenv("ORDER_SIZE", "1"))
ORDER_DOLLARS = max(1.0, _get_float_env("ORDER_DOLLARS", 1.0))
MAX_ORDER_CONTRACTS = _get_int_env("MAX_ORDER_CONTRACTS", 100)
MAX_HEDGE_INVENTORY = _get_int_env("MAX_HEDGE_INVENTORY", 250)
TARGET_TICKER = os.getenv("TARGET_TICKER", "") # Can be injected to force a specific market


MAX_EXPIRATION_DAYS = _get_float_env("MAX_EXPIRATION_DAYS", 8.0) # Rolling window (days) for automated sports discovery

# Database Configurations (PostgreSQL)
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "kalshi_bot")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")

