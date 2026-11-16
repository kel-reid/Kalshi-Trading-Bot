import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from data.fill_store import FillStore, _COLUMNS, _normalize


class MemoryCursor:
    """Small DB-API double; PostgreSQL SQL syntax still needs integration testing."""

    def __init__(self):
        self.rows = []
        self.matches = []
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def _parse_conditions(self, sql, params):
        if not params:
            return []
        where_part = sql.split("WHERE (", 1)[1].rsplit(") FOR UPDATE", 1)[0]
        cond_strings = [c.strip() for c in where_part.split(" OR ")]
        return list(zip(cond_strings, params))

    def execute(self, sql, params=None):
        self.statements.append(sql)
        if sql.startswith("SELECT id, "):
            self.matches = []
            for row in self.rows:
                r_fill = row[1]["fill_id"]
                r_trade = row[1]["trade_id"]
                matched = False
                # Replicate the WHERE conditions precisely
                for cond, p in self._parse_conditions(sql, params):
                    if cond == "fill_id = %s" and r_fill == p:
                        matched = True
                    elif cond == "trade_id = %s" and r_trade == p:
                        matched = True
                    elif cond == "(trade_id = %s AND fill_id IS NULL)" and r_trade == p and r_fill is None:
                        matched = True
                if matched:
                    self.matches.append(row)
        elif sql.startswith("INSERT INTO"):
            values = dict(zip(_COLUMNS, params))
            self.rows.append((len(self.rows) + 1, values, json.loads(params[-1].dumps(params[-1].adapted))))
        elif sql.startswith("UPDATE"):
            assert "COALESCE(" in sql and "jsonb_array_elements(raw_events)" in sql
            row_id = params[-1]
            row = next(row for row in self.rows if row[0] == row_id)
            for name, value in zip(_COLUMNS, params):
                if row[1][name] is None or (name == "trade_id" and "trade_id = %s" in sql):
                    row[1][name] = value
            event = json.loads(params[-2].dumps(params[-2].adapted))[0]
            if event not in row[2]:
                row[2].append(event)

    def fetchall(self):
        return [(row[0], *(row[1][name] for name in _COLUMNS)) for row in self.matches]


@pytest.fixture
def store():
    cursor = MemoryCursor()
    conn = MagicMock()
    conn.autocommit = False
    conn.cursor.return_value = cursor
    pool = MagicMock()
    pool.getconn.return_value = conn
    return FillStore(pool), cursor, conn, pool


def test_fractional_no_fill_precision_and_legacy_cents():
    fill = {"fill_id": "f", "side": "no", "action": "buy", "count_fp": "0.12345678901234567890123456789",
            "yes_price_dollars": "0.71234567890123456789012345678", "fee_cost": "0.000000123456789",
            "is_taker": False, "subaccount": 0, "exchange_index": 0, "ts_ms": 1671899397123}
    values = _normalize(fill)
    assert values["quantity"] == Decimal(fill["count_fp"])
    assert values["yes_price_dollars"] == Decimal(fill["yes_price_dollars"])
    assert values["no_price_dollars"] is None
    assert values["fee_dollars"] == Decimal(fill["fee_cost"])
    assert values["is_taker"] is False
    assert values["subaccount"] == values["exchange_index"] == 0
    assert values["ts_ms"] == Decimal("1671899397123")
    assert _normalize({"fill_id": "f", "no_price": "12.12345678901234567890123456789"})["no_price_dollars"] == Decimal("0.1212345678901234567890123456789")


@pytest.mark.parametrize("reverse", [False, True])
def test_idempotency_and_rest_enrichment(store, reverse):
    db, cursor, conn, pool = store
    ws = {"trade_id": "trade", "order_id": "order", "count_fp": "0.25", "side": "no", "ts": 123}
    rest = {"fill_id": "fill", "trade_id": "trade", "order_id": "order", "fee_cost": "0.0025",
            "no_price_dollars": "0.6400", "created_time": "2026-01-01T00:00:00.123456Z"}
    assert db.initialize() and db.initialize()
    events = [(ws, "websocket"), (rest, "rest")]
    if reverse:
        events.reverse()
    for fill, source in events + events:
        assert db.record(fill, source)
    assert len(cursor.rows) == 1
    row = cursor.rows[0]
    assert row[1]["fill_id"] == "fill"
    assert row[1]["quantity"] == Decimal("0.25")
    assert row[1]["fee_dollars"] == Decimal("0.0025")
    assert row[1]["no_price_dollars"] == Decimal("0.6400")
    assert row[1]["created_time"] == rest["created_time"]
    assert len(row[2]) == 2
    assert conn.commit.call_count == 6
    assert pool.putconn.call_count == 6


@pytest.mark.parametrize("reverse", [False, True])
def test_websocket_trade_id_joins_rest_fill_id_without_order_id(store, reverse):
    db, cursor, _, _ = store
    ws = {"trade_id": "same-fill", "ts_ms": 1671899397123, "subaccount": 2,
          "outcome_side": "no", "book_side": "bid", "count_fp": "0.125"}
    rest = {"fill_id": "same-fill", "subaccount_number": 2,
            "created_time": "2026-01-01T00:00:00.123456Z",
            "yes_price_dollars": "0.7123", "no_price_dollars": "0.2877", "fee_cost": "0.0001"}
    events = [(ws, "websocket"), (rest, "rest")]
    if reverse:
        events.reverse()
    for fill, source in events + events:
        assert db.record(fill, source)
    assert len(cursor.rows) == 1
    row = cursor.rows[0][1]
    assert row["fill_id"] == row["trade_id"] == "same-fill"
    assert row["ts_ms"] == Decimal("1671899397123")
    assert row["created_time"] == rest["created_time"]
    assert row["subaccount"] == 2
    assert row["outcome_side"] == "no" and row["book_side"] == "bid"
    assert row["quantity"] == Decimal("0.125")
    assert row["yes_price_dollars"] == Decimal("0.7123")
    assert row["no_price_dollars"] == Decimal("0.2877")
    assert row["fee_dollars"] == Decimal("0.0001")
    assert len(cursor.rows[0][2]) == 2


@pytest.mark.parametrize("reverse", [False, True])
def test_rest_trade_id_distinct_from_websocket_identifier(store, reverse):
    db, cursor, _, _ = store
    ws = {"trade_id": "fill-id"}
    rest = {"fill_id": "fill-id", "trade_id": "rest-trade-id"}
    events = [(ws, "websocket"), (rest, "rest")]
    if reverse:
        events.reverse()
    for fill, source in events + events:
        assert db.record(fill, source)
    assert len(cursor.rows) == 1
    assert cursor.rows[0][1]["fill_id"] == "fill-id"
    assert cursor.rows[0][1]["trade_id"] == "rest-trade-id"


def test_conflicting_identity_is_rejected(store):
    db, cursor, conn, _ = store
    assert db.record({"fill_id": "f", "trade_id": "t", "order_id": "o"}, "rest")
    assert not db.record({"fill_id": "f", "trade_id": "different", "order_id": "o"}, "rest")
    assert len(cursor.rows) == 1
    conn.rollback.assert_called_once()


def test_conflicting_orders_for_same_trade_are_rejected(store):
    db, cursor, _, _ = store
    assert db.record({"trade_id": "t", "order_id": "o1"}, "websocket")
    assert not db.record({"trade_id": "t", "order_id": "o2"}, "websocket")
    assert len(cursor.rows) == 1


def test_conflicting_values_are_logged_and_audited_without_overwriting(store, caplog):
    db, cursor, _, _ = store
    assert db.record({"fill_id": "f", "count_fp": "1.25"}, "rest")
    assert db.record({"fill_id": "f", "count_fp": "1.50"}, "websocket")
    assert cursor.rows[0][1]["quantity"] == Decimal("1.25")
    assert len(cursor.rows[0][2]) == 2
    assert "Conflicting quantity" in caplog.text


def test_autocommit_connection_is_rejected(store):
    db, _, conn, pool = store
    conn.autocommit = True
    assert not db.record({"fill_id": "f"}, "rest")
    conn.cursor.assert_not_called()
    pool.putconn.assert_called_once_with(conn)


@pytest.mark.parametrize("fill", [{}, {"order_id": "o"}, {"fill_id": "f", "count_fp": "NaN"},
                                  {"fill_id": "f", "fee_cost": True}, {"fill_id": "f", "count_fp": "-1"}])
def test_bad_data_does_not_reach_database(store, fill):
    db, _, _, pool = store
    assert not db.record(fill, "rest")
    pool.getconn.assert_not_called()


def test_no_pool_and_database_errors_are_reported(store, caplog):
    assert not FillStore(None).initialize()
    assert not FillStore(None).record({"fill_id": "f"}, "rest")
    db, _, conn, pool = store
    conn.commit.side_effect = RuntimeError("commit failed")
    assert not db.record({"fill_id": "f"}, "rest")
    conn.rollback.assert_called_once()
    pool.putconn.assert_called_once_with(conn)
    assert "Fill persistence failed" in caplog.text


def test_identity_namespaces_and_cross_field_distinct_fills(store):
    db, cursor, _, _ = store
    fill1 = {"trade_id": "same", "fill_id": "different", "market_ticker": "T", "count_fp": "1.0"}
    fill2 = {"trade_id": "second", "fill_id": "same", "market_ticker": "T", "count_fp": "1.0"}
    assert db.record(fill1, "rest")
    assert db.record(fill2, "rest")
    assert len(cursor.rows) == 2
    assert cursor.rows[0][1]["trade_id"] == "same"
    assert cursor.rows[0][1]["fill_id"] == "different"
    assert cursor.rows[1][1]["trade_id"] == "second"
    assert cursor.rows[1][1]["fill_id"] == "same"


def test_trade_only_replay_matches_same_field_precedence(store):
    db, cursor, _, _ = store
    fill1 = {"trade_id": "same", "fill_id": "different", "market_ticker": "T", "count_fp": "1.0"}
    fill2 = {"trade_id": "second", "fill_id": "same", "market_ticker": "T", "count_fp": "1.0"}
    assert db.record(fill1, "rest")
    assert db.record(fill2, "rest")
    assert len(cursor.rows) == 2

    # Replay with trade_id="same" and no fill_id should match fill1 on same-field precedence
    # rather than failing with multiple matches against fill2's fill_id alias.
    replay = {"trade_id": "same", "market_ticker": "T", "count_fp": "1.0"}
    assert db.record(replay, "websocket")
    assert len(cursor.rows) == 2
    row1 = cursor.rows[0]
    row2 = cursor.rows[1]
    assert row1[1]["trade_id"] == "same"
    assert row1[1]["fill_id"] == "different"
    assert len(row1[2]) == 2
    assert row2[1]["trade_id"] == "second"
    assert row2[1]["fill_id"] == "same"
    assert len(row2[2]) == 1

