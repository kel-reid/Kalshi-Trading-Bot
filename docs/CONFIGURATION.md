# Configuration

The bot reads its settings from environment variables. Locally, it also loads a `.env` file if one is present (use `.env.example` as a template). In production, Doppler injects them at runtime.

## Credentials & Alerts

| Parameter | Required | Description |
| :--- | :--- | :--- |
| `KALSHI_API_KEY` | Yes | Kalshi API key ID. The bot refuses to start without it. |
| `KALSHI_PRIVATE_KEY` | One of these two | RSA private key contents, used to sign requests. Preferred in production, so the key never touches disk. |
| `KALSHI_PRIVATE_KEY_PATH` | One of these two | Path to the RSA private key file, for local development. |
| `ALERT_WEBHOOK_URL` | No | Slack or Discord webhook for safeguard and kill-switch alerts. |

## Trading & Sizing

| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `KALSHI_ENV` | `string` | `prod` | Exchange environment: `demo` or `prod`. |
| `TARGET_TICKER` | `string` | `""` | Target market ticker (e.g. `KXNFLGAME-26OCT04DALHOU-DAL`), league (`NFL`) or category. Empty triggers automatic in-season discovery. |
| `ORDER_SIZE` | `integer` | `1` | Fallback number of contracts to quote per side. |
| `ORDER_DOLLARS` | `float` | `1.0` | Minimum dollar allocation per quote, used to size orders dynamically. |
| `MAX_ORDER_CONTRACTS` | `integer` | `100` | Maximum contracts allowed in a single order. |
| `MAX_HEDGE_INVENTORY` | `integer` | `250` | Upper cap on the hedge threshold: `min(5 × quote size, MAX_HEDGE_INVENTORY)` with dollar sizing, or `min(5, MAX_HEDGE_INVENTORY)` with fixed sizing. |
| `MIN_SPREAD` | `integer` | `4` | Minimum spread between bid and ask, in cents. |
| `RISK_GAMMA` | `float` | `0.7` | Risk-aversion parameter (`γ`) controlling how strongly quotes skew against inventory. |

## Safeguards

| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `MIN_MID_PRICE` | `integer` | `10` | Lower price collar, in cents. Quoting stops below this mid price. |
| `MAX_MID_PRICE` | `integer` | `90` | Upper price collar, in cents. Quoting stops above this mid price. |
| `MAX_SESSION_FEES_CENTS` | `integer` | `250` | Session fees, in cents, that trigger liquidation and rotation. |
| `MAX_SESSION_LOSS_CENTS` | `integer` | `300` | Session net loss, in cents, that triggers liquidation and rotation. |
| `POST_FILL_PAUSE_SECONDS` | `float` | `3.0` | Pause after a fill before quoting again. |
| `PRICE_VELOCITY_THRESHOLD_CENTS` | `float` | `6.0` | Mid-price move, in cents, that trips the fast-market breaker. |
| `PRICE_VELOCITY_WINDOW_SECONDS` | `float` | `20.0` | Rolling window used to measure that move. |
| `PRICE_VELOCITY_QUIESCE_SECONDS` | `float` | `30.0` | How long quotes stay pulled after the breaker trips. |
| `MAX_EXPIRATION_DAYS` | `float` | `8.0` | Only markets expiring within this many days are eligible, to avoid locking up capital. |
| `EXPIRATION_BUFFER_MINUTES` | `integer` | `90` | Minutes before expiry when the bot stops quoting, liquidates and rotates. |

## Database

| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `DB_HOST` | `string` | `localhost` | PostgreSQL host (`db` inside Docker Compose). |
| `DB_PORT` | `integer` | `5432` | PostgreSQL port. |
| `DB_NAME` | `string` | `kalshi_bot` | Database name. |
| `DB_USER` | `string` | `postgres` | Database user. |
| `DB_PASSWORD` | `string` | `postgres` | Database password. Docker Compose requires this to be set explicitly. |

For the seasonal matrix and series precedence rules used by market discovery, see the [Sports Season Router specification](SPORTS_SEASON_ROUTER.md).
