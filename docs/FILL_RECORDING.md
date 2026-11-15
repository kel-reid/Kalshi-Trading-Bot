# Fill recording and backfill

The bot stores individual exchange fills in PostgreSQL `kalshi_fills`, including
fractional quantity, YES and NO prices in dollars, fees in dollars, maker/taker
status, account and shard when supplied, identifiers, timestamps, and raw events.
Records are merged by exchange fill identity. Missing fields remain null; the
exchange REST copy can enrich a WebSocket copy.

After the fill subscription starts, a background scan reads all accessible live
and historical fills. It repeats after a WebSocket reconnect and hourly, or
retries after a failure. Each scan starts again from the beginning so interrupted
scans can be replayed. This can be slow for large accounts and consumes REST
capacity. Check `FillSync` and `FillStore` logs for failures. Exchange pagination
is not a snapshot, permissions can restrict the account, and older records might
not be available; a completed scan does not prove that the database is complete.

Live WebSocket fills are committed to a local SQLite spool before inventory is
updated. Replay deletes a spool entry only after PostgreSQL reports a commit;
a crash between those operations causes an idempotent retry. Failed records are
retained and retried, including permanently invalid records. Monitor logs and
disk capacity; a stalled database call delays replay and shutdown. The spool is
not size-limited and disk exhaustion is a fatal ingestion error, not a guarantee
that the triggering fill was saved.

Set `FILL_SPOOL_PATH` to a local persistent filesystem path. Compose mounts the
`fill_spool` volume at `/var/lib/kalshi-fill-spool`; preserve it across container
replacement and do not use `docker compose down -v` unless loss is acceptable.
The image creates the mountpoint owned by UID/GID 10001; Docker populates a new
named volume from it. Existing volumes and bind mounts must be writable by that
UID/GID, and volume drivers that disable copy-up require explicit provisioning.
The Compose image must be rebuilt and deployed to include this change. The
default spool and SQLite sidecars under `data/` are excluded from Docker's build
context; exclude any custom spool path separately, preferably outside the context.
Only one process may own a spool path. SQLite durability depends on the storage
and filesystem honoring sync and locking; do not share the spool over an
unverified network filesystem. Back up and monitor the volume separately.

Lifecycle integration is required: supervise the WebSocket task and stop quoting,
cancel outstanding orders, and surface fatal ingestion errors immediately; do
not silently restart trading. Stop and await WebSocket ingestion before calling
`close_fill_ingestion(clean=True)` only after a confirmed clean lifecycle; use
`close_fill_ingestion()` or `close_fill_ingestion(clean=False)` after any failure,
cancellation, disconnect requiring reconciliation, or uncertain shutdown. Then
close the database pool. The strategy supervises ingestion and follows this
shutdown sequence, but exchange checks and fill delivery are not atomic. Any disconnect,
crash before spool commit, or storage failure can lose received or unreceived
WebSocket-only fields. Reconcile fills and inventory with REST before resuming;
REST coverage is not guaranteed, so this is not a complete audit trail.

The spool is marked dirty before subscription and cleared only when explicitly
requested on clean shutdown without a reported ingestion failure. Cancellation
while appending propagates cancellation and retains the latch even if the append
committed. An unclean exit or fatal fill processing
error prevents the next subscription. After stopping all owners, investigating
and reconciling fills, inventory, balance and outstanding orders, an operator may
open `FillSpool` on the persistent path, call `clear_dirty()`, and close it. This
does not reconcile anything automatically. Never clear it while the bot is running.
Loss or replacement of the volume loses the latch. Failures before the dirty mark
commits cannot be recorded; a filesystem that lies about sync also defeats it.

For offline recovery, stop every bot using the volume, retain a backup including
SQLite sidecars, inspect pending records and reconcile against exchange fills,
positions, balances and open orders. With the volume mounted and the same path
configured, the following command clears only the latch after reconciliation:

```bash
python -c 'import os; from data.fill_spool import FillSpool; s = FillSpool(os.environ["FILL_SPOOL_PATH"]); print("dirty:", s.dirty(), "first pending:", s.pending()); s.clear_dirty(); s.close()'
```

Pending rows are not removed and replay on the next start. Inspect all rows
before clearing; a permanently invalid row can remain stuck. Do not run this
command against the wrong account, volume or environment.

Inventory deduplication tracks `fill_id` and `trade_id` in separate namespaces
in memory for the current manager lifetime only. The [WebSocket documentation](https://docs.kalshi.com/websockets/user-fills) shows `trade_id`, while the
[REST documentation](https://docs.kalshi.com/api-reference/portfolio/get-fills)
shows both fields; it does not establish they are interchangeable. Both fields
on one event link those identities. WebSocket `trade_id` is required; missing or
invalid IDs and conflicting reused
IDs fail closed. Differences in any other payload field also fail closed, even
if the difference is harmless. Redelivery across restarts is not deduplicated.
The database spool still records each event.

To backfill the October 31 and November 1, 2026 Eastern trading days, run this
in an environment configured with the intended Kalshi credentials and PostgreSQL
connection. The offsets account for the November 1 daylight-saving transition.
First omit `--execute` to validate the range without connecting; adding it
creates the table if needed and writes retrieved fills:

```bash
python scripts/backfill_fills.py --start 2026-10-31T00:00:00-04:00 --end 2026-11-02T00:00:00-05:00 --execute
```

The command fetches fills only; it does not start the bot or place orders. It
returns a nonzero status on API or database failure. A partial run can be safely
repeated. It reports rows processed, not necessarily new rows. An API key may
restrict which subaccounts are visible; verify the intended environment and
account before using the results.

Example query, using the exchange event time when available:

```sql
SELECT fill_id, trade_id, order_id, market_ticker, side, action,
       outcome_side, book_side, quantity, yes_price_dollars,
       no_price_dollars, fee_dollars, is_taker, subaccount, exchange_index,
       COALESCE(created_time::timestamptz, to_timestamp(ts_ms / 1000),
                to_timestamp(ts)) AS executed_at
FROM kalshi_fills
ORDER BY executed_at;
```

The existing `scripts/generate_daily_report.py` does **not** use this table; its
fill counts and round trips are inferred from order statuses and are not exchange
fill-level reporting. Fills alone also do not establish realized account P&L:
carried-in lots, settlements, fees and other account activity must be reconciled.