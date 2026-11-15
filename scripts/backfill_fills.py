#!/usr/bin/env python3
"""Backfill [start, end) using offset-qualified ISO 8601 timestamps.

For Eastern time supply the actual UTC offsets, e.g. for the 2026 fall DST
transition: --start 2026-10-31T00:00:00-04:00 --end 2026-11-02T00:00:00-05:00.
Nothing connects to Kalshi or PostgreSQL unless --execute is supplied.
"""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from data.fill_reconciliation import FillReconciler, ReconciliationError, aware_datetime


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="inclusive ISO timestamp with UTC offset")
    parser.add_argument("--end", required=True, help="exclusive ISO timestamp with UTC offset")
    parser.add_argument("--execute", action="store_true", help="connect and persist fills")
    args = parser.parse_args(argv)
    try:
        start, end = aware_datetime(args.start), aware_datetime(args.end)
        if start >= end:
            raise ValueError("start must precede end")
    except ValueError as exc:
        parser.error(str(exc))
    if not args.execute:
        print(f"Dry run: [{start.isoformat()}, {end.isoformat()}); pass --execute to backfill.")
        return 0

    pool = None
    try:
        from psycopg2.pool import ThreadedConnectionPool
        from config import BASE_URL, DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
        from auth.kalshi_auth import get_auth_headers
        from data.fill_store import FillStore

        pool = ThreadedConnectionPool(1, 4, host=DB_HOST, port=DB_PORT,
                                      database=DB_NAME, user=DB_USER,
                                      password=DB_PASSWORD, connect_timeout=10)
        store = FillStore(pool)
        if not store.initialize():
            raise RuntimeError("fill store initialization failed")
        report = asyncio.run(FillReconciler(store, BASE_URL, get_auth_headers).reconcile(start, end))
        print(json.dumps(asdict(report)))
        return 0
    except ReconciliationError as exc:
        print(json.dumps({"error": str(exc), "report": asdict(exc.report)}), file=sys.stderr)
        return 1
    except Exception as exc:
        print(json.dumps({"error": str(exc), "complete": False}), file=sys.stderr)
        return 1
    finally:
        if pool is not None:
            pool.closeall()


if __name__ == "__main__":
    sys.exit(main())