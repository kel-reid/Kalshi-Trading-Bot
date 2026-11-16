"""PostgreSQL persistence for individual Kalshi fills."""

import json
import logging
from decimal import Decimal, InvalidOperation

from psycopg2.extras import Json


logger = logging.getLogger("FillStore")

_COLUMNS = (
    "fill_id", "trade_id", "order_id", "client_order_id", "market_ticker",
    "side", "action", "outcome_side", "book_side", "purchased_side",
    "is_taker", "subaccount", "exchange_index", "quantity",
    "yes_price_dollars", "no_price_dollars", "price_dollars", "fee_dollars",
    "created_time", "ts", "ts_ms", "post_position",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kalshi_fills (
    id BIGSERIAL PRIMARY KEY,
    fill_id TEXT UNIQUE,
    trade_id TEXT,
    order_id TEXT,
    client_order_id TEXT,
    market_ticker TEXT,
    side TEXT,
    action TEXT,
    outcome_side TEXT,
    book_side TEXT,
    purchased_side TEXT,
    is_taker BOOLEAN,
    subaccount BIGINT,
    exchange_index BIGINT,
    quantity NUMERIC,
    yes_price_dollars NUMERIC,
    no_price_dollars NUMERIC,
    price_dollars NUMERIC,
    fee_dollars NUMERIC,
    created_time TEXT,
    ts NUMERIC,
    ts_ms NUMERIC,
    post_position NUMERIC,
    raw_events JSONB NOT NULL DEFAULT '[]'::jsonb,
    CHECK (fill_id IS NOT NULL OR trade_id IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS kalshi_fills_trade_id_idx
    ON kalshi_fills (trade_id) WHERE trade_id IS NOT NULL;
"""


def _first(fill, *names):
    for name in names:
        value = fill.get(name)
        if value is not None and value != "":
            return value
    return None


def _decimal(value, name, cents=False):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric, not boolean")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"invalid {name}: {value!r}") from None
    if not result.is_finite():
        raise ValueError(f"non-finite {name}: {value!r}")
    if cents:
        parts = result.as_tuple()
        result = Decimal((parts.sign, parts.digits, parts.exponent - 2))
    return result


def _normalize(fill):
    if not isinstance(fill, dict):
        raise ValueError("fill must be a dictionary")
    result = {}
    for name in ("fill_id", "trade_id", "order_id", "client_order_id", "side",
                 "action", "outcome_side", "book_side", "purchased_side", "created_time"):
        value = _first(fill, name)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"invalid {name}")
        result[name] = value
    result["market_ticker"] = _first(fill, "market_ticker", "ticker")
    if result["market_ticker"] is not None and not isinstance(result["market_ticker"], str):
        raise ValueError("invalid market_ticker")
    if not result["fill_id"] and not result["trade_id"]:
        raise ValueError("fill requires fill_id or trade_id")
    result["is_taker"] = _first(fill, "is_taker")
    if result["is_taker"] is not None and not isinstance(result["is_taker"], bool):
        raise ValueError("invalid is_taker")
    for name, aliases in (("subaccount", ("subaccount_number", "subaccount")),
                          ("exchange_index", ("exchange_index",))):
        value = _first(fill, *aliases)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError(f"invalid {name}")
        result[name] = value
    for name, aliases in (
        ("quantity", ("count_fp", "count")),
        ("yes_price_dollars", ("yes_price_dollars",)),
        ("no_price_dollars", ("no_price_dollars",)),
        ("price_dollars", ("price_dollars",)),
        ("fee_dollars", ("fee_cost", "fee_dollars")),
        ("ts", ("ts",)), ("ts_ms", ("ts_ms",)),
        ("post_position", ("post_position_fp",)),
    ):
        result[name] = _decimal(_first(fill, *aliases), name)
    for name, aliases in (
        ("yes_price_dollars", ("yes_price",)), ("no_price_dollars", ("no_price",)),
        ("price_dollars", ("price",)), ("fee_dollars", ("fee_cents", "fee")),
    ):
        if result[name] is None:
            result[name] = _decimal(_first(fill, *aliases), name, cents=True)
    if result["quantity"] is not None and result["quantity"] <= 0:
        raise ValueError("quantity must be positive")
    for name in ("yes_price_dollars", "no_price_dollars", "price_dollars"):
        if result[name] is not None and not Decimal(0) <= result[name] <= Decimal(1):
            raise ValueError(f"{name} out of range")
    if result["fee_dollars"] is not None and result["fee_dollars"] < 0:
        raise ValueError("fee must not be negative")
    return result


def _json_dumps(value):
    def default(obj):
        if isinstance(obj, Decimal):
            return str(obj)
        raise TypeError(f"not JSON serializable: {type(obj).__name__}")
    return json.dumps(value, default=default, allow_nan=False)


class FillStore:
    """Uses a caller-owned psycopg2 threaded pool; failures return False and are logged."""

    def __init__(self, db_pool):
        self.db_pool = db_pool

    def _transaction(self, operation):
        conn = None
        try:
            if self.db_pool is None:
                raise RuntimeError("fill database pool unavailable")
            conn = self.db_pool.getconn()
            if conn.autocommit:
                raise RuntimeError("fill persistence requires a transactional connection")
            with conn.cursor() as cursor:
                operation(cursor)
            conn.commit()
            return True
        except Exception:
            logger.exception("Fill persistence failed")
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    logger.exception("Fill database rollback failed")
            return False
        finally:
            if conn is not None:
                try:
                    self.db_pool.putconn(conn)
                except Exception:
                    logger.exception("Failed to return fill database connection")

    def initialize(self):
        """Create the schema idempotently. Return False if unavailable or unsuccessful."""
        return self._transaction(lambda cursor: cursor.execute(_SCHEMA))

    def record(self, fill: dict, source: str) -> bool:
        """Persist or enrich a fill. True means committed, including a duplicate."""
        try:
            if not isinstance(source, str) or not source.strip():
                raise ValueError("source must be a nonempty string")
            values = _normalize(fill)
            event = {"source": source, "fill": fill}
            _json_dumps(event)
        except (ValueError, TypeError):
            logger.exception("Invalid fill for persistence")
            return False

        def write(cursor):
            keys = {"identity:" + value for value in (values["fill_id"], values["trade_id"]) if value}
            for key in sorted(keys):
                cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))

            columns_sql = ", ".join(_COLUMNS)
            same_field_conditions = []
            same_field_params = []
            if values["fill_id"] is not None:
                same_field_conditions.append("fill_id = %s")
                same_field_params.append(values["fill_id"])
            if values["trade_id"] is not None:
                same_field_conditions.append("trade_id = %s")
                same_field_params.append(values["trade_id"])

            rows = []
            if same_field_conditions:
                where_sql = " OR ".join(same_field_conditions)
                cursor.execute(
                    f"SELECT id, {columns_sql} FROM kalshi_fills WHERE ({where_sql}) FOR UPDATE",  # noqa: S608
                    tuple(same_field_params),
                )
                rows = cursor.fetchall()

            if not rows:
                alias_conditions = []
                alias_params = []
                if values["fill_id"] is not None:
                    alias_conditions.append("(trade_id = %s AND fill_id IS NULL)")
                    alias_params.append(values["fill_id"])
                if values["fill_id"] is None and values["trade_id"] is not None:
                    alias_conditions.append("fill_id = %s")
                    alias_params.append(values["trade_id"])
                if alias_conditions:
                    alias_where_sql = " OR ".join(alias_conditions)
                    cursor.execute(
                        f"SELECT id, {columns_sql} FROM kalshi_fills WHERE ({alias_where_sql}) FOR UPDATE",  # noqa: S608
                        tuple(alias_params),
                    )
                    rows = cursor.fetchall()

            if len(rows) > 1:
                raise ValueError("fill identifiers refer to different existing rows")
            audit = Json([event], dumps=_json_dumps)
            if not rows:
                cursor.execute(
                    "INSERT INTO kalshi_fills (" + ", ".join(_COLUMNS) + ", raw_events) VALUES (" +
                    ", ".join(["%s"] * len(_COLUMNS)) + ", %s::jsonb)",
                    tuple(values[name] for name in _COLUMNS) + (audit,),
                )
                return
            row = rows[0]
            existing = dict(zip(_COLUMNS, row[1:]))
            for name in ("fill_id", "order_id"):
                if existing[name] is not None and values[name] is not None and existing[name] != values[name]:
                    raise ValueError(f"conflicting {name} for existing fill")
            replace_trade_id = False
            if existing["trade_id"] is not None and values["trade_id"] is not None and existing["trade_id"] != values["trade_id"]:
                if values["fill_id"] == existing["trade_id"]:
                    replace_trade_id = True
                elif existing["fill_id"] != values["trade_id"]:
                    raise ValueError("conflicting trade_id for existing fill")
            for name in _COLUMNS:
                if name == "trade_id" and (replace_trade_id or existing["fill_id"] == values["trade_id"]):
                    continue
                if existing[name] is not None and values[name] is not None and existing[name] != values[name]:
                    logger.warning("Conflicting %s for fill %s; retaining existing value", name, row[0])
            cursor.execute(
                "UPDATE kalshi_fills SET " + ", ".join(
                    f"{name} = %s" if name == "trade_id" and replace_trade_id else f"{name} = COALESCE({name}, %s)"
                    for name in _COLUMNS
                ) +
                ", raw_events = CASE WHEN EXISTS (SELECT 1 FROM jsonb_array_elements(raw_events) AS event "
                "WHERE event = %s::jsonb) THEN raw_events "
                "ELSE raw_events || %s::jsonb END WHERE id = %s",
                tuple(values[name] for name in _COLUMNS) + (Json(event, dumps=_json_dumps), audit, row[0]),
            )
        return self._transaction(write)