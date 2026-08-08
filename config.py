import os
from pathlib import Path
from dotenv import load_dotenv

# Automatically load environment variables from a .env file if present
load_dotenv()

# Project root directory
PROJECT_ROOT = Path(__file__).resolve().parent

# Environment (Options: "demo" or "prod")
ENVIRONMENT = os.getenv("KALSHI_ENV", "demo")

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

# Strategy Tuning Parameters
# Default: Gamma 0.5 (Risk Aversion), 4 cent minimum spread, 1 contract order size
RISK_GAMMA = float(os.getenv("RISK_GAMMA", "0.5"))
MIN_SPREAD = int(os.getenv("MIN_SPREAD", "4"))
ORDER_SIZE = int(os.getenv("ORDER_SIZE", "1"))
TARGET_TICKER = os.getenv("TARGET_TICKER", "") # Can be injected to force a specific market

# Database Configurations (PostgreSQL)
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "kalshi_bot")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")

