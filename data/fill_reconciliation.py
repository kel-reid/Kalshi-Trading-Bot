"""Read-only Kalshi fill reconciliation; no orders or checkpoints are created.

Kalshi's live and historical lists are not a snapshot, and access may be scoped
to a subaccount. Even a successful scan cannot prove completeness. Each scan
starts at the beginning of both accessible tiers so a later scan repairs gaps.
See https://docs.kalshi.com/getting_started/historical_data and
https://docs.kalshi.com/api-reference/portfolio/get-fills.
"""

import asyncio
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests


CURRENT = "/trade-api/v2/portfolio/fills"
HISTORICAL = "/trade-api/v2/historical/fills"


def aware_datetime(value):
    """Parse an ISO 8601 timestamp or POSIX epoch, rejecting timestamps without an offset."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                return datetime.fromtimestamp(float(value), tz=timezone.utc)
            except ValueError:
                pass
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a UTC offset")
    return value.astimezone(timezone.utc)


@dataclass
class ReconciliationReport:
    pages: int = 0
    fetched: int = 0
    in_range: int = 0
    persisted: int = 0
    complete: bool = False
    warnings: list = field(default_factory=lambda: [
        "Completeness cannot be proven: API tiers are not a snapshot, access may be scoped, and data may be unavailable."
    ])
    failed: int = 0


class ReconciliationError(RuntimeError):
    def __init__(self, message, report):
        super().__init__(message)
        self.report = report


class _ScanCancelled(Exception):
    pass


class FillReconciler:
    """Use await reconcile(start=None, end=None); synchronous store calls run off-loop.

    store must implement record(fill: dict, source: str) -> bool, returning
    True for any successful commit (including duplicates), and be initialized
    by the caller. A full scan is the default. Explicit ranges are
    [start, end) in absolute time. A failed run raises ReconciliationError with
    partial counts in .report; already committed fills remain safe to replay.
    Cancellation stops between operations and waits for any in-flight call.
    Use one reconciler per store to serialize scans.
    """

    def __init__(self, store, base_url, auth_headers, *, session=None,
                 limit=1000, retries=3, min_interval=0.1, timeout=15, on_fill=None):
        if not 1 <= limit <= 1000 or retries < 0 or min_interval < 0 or timeout <= 0:
            raise ValueError("invalid pagination, retry, rate or timeout settings")
        self.store = store
        self.base_url = base_url.rstrip("/")
        self.auth_headers = auth_headers
        self.session = session or requests.Session()
        self.limit = limit
        self.retries = retries
        self.min_interval = min_interval
        self.timeout = timeout
        self.on_fill = on_fill
        self._last_request = None
        self._lock = asyncio.Lock()

    async def reconcile(self, start=None, end=None):
        start = aware_datetime(start) if start is not None else None
        end = aware_datetime(end) if end is not None else None
        if start is not None and end is not None and start >= end:
            raise ValueError("start must precede end")
        async with self._lock:
            stop = threading.Event()
            task = asyncio.create_task(asyncio.to_thread(self._scan, start, end, stop))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                stop.set()
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not task.cancelled():
                    task.exception()
                raise

    @staticmethod
    def _check_stop(stop):
        if stop.is_set():
            raise _ScanCancelled()

    @staticmethod
    def _wait(stop, delay):
        if stop.wait(delay):
            raise _ScanCancelled()

    def _request(self, path, params, stop):
        for attempt in range(self.retries + 1):
            self._check_stop(stop)
            if self._last_request is not None:
                self._wait(stop, max(0, self.min_interval - (time.monotonic() - self._last_request)))
            try:
                headers = self.auth_headers("GET", path)
                self._check_stop(stop)
                self._last_request = time.monotonic()
                response = self.session.get(
                    self.base_url + path, headers=headers,
                    params=params, timeout=self.timeout,
                )
            except (requests.Timeout, requests.ConnectionError):
                self._check_stop(stop)
                if attempt == self.retries:
                    raise
                self._wait(stop, min(30, 2 ** attempt))
                continue
            self._check_stop(stop)
            if response.status_code == 429 or 500 <= response.status_code <= 599:
                if attempt < self.retries:
                    delay = min(30, 2 ** attempt)
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            delay = float(retry_after)
                        except ValueError:
                            try:
                                delay = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
                            except (TypeError, ValueError, OverflowError):
                                pass
                    self._wait(stop, max(0, min(60, delay)))
                    continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("unreachable retry state")

    def _scan(self, start, end, stop):
        report = ReconciliationReport()
        first_persistence_error = None
        params = {"limit": self.limit}
        if start is not None:
            params["min_ts"] = math.floor(start.timestamp()) - 1
        if end is not None:
            params["max_ts"] = math.ceil(end.timestamp()) + 1
        try:
            for path in (CURRENT, HISTORICAL):
                self._check_stop(stop)
                cursor = None
                seen = set()
                while True:
                    self._check_stop(stop)
                    query = dict(params)
                    if cursor is not None:
                        query["cursor"] = cursor
                    page = self._request(path, query, stop)
                    if not isinstance(page, dict) or not isinstance(page.get("fills"), list) or "cursor" not in page:
                        raise ValueError(f"invalid response from {path}")
                    next_cursor = page["cursor"]
                    if next_cursor is not None and not isinstance(next_cursor, str):
                        raise ValueError(f"invalid cursor from {path}")
                    if next_cursor and next_cursor in seen:
                        raise ValueError(f"cursor loop from {path}")
                    report.pages += 1
                    for fill in page["fills"]:
                        self._check_stop(stop)
                        report.fetched += 1
                        if not isinstance(fill, dict):
                            raise ValueError(f"invalid fill from {path}")
                        if start is not None or end is not None:
                            raw_ts = fill.get("created_time")
                            if raw_ts is None:
                                raw_ts = fill.get("ts")
                            timestamp = aware_datetime(raw_ts)
                            if (start is not None and timestamp < start) or (end is not None and timestamp >= end):
                                continue
                        report.in_range += 1
                        self._check_stop(stop)
                        try:
                            if not self.store.record(fill, source="rest"):
                                raise RuntimeError("fill persistence failed")
                        except Exception as exc:
                            report.failed += 1
                            if first_persistence_error is None:
                                first_persistence_error = exc
                                report.warnings.append(f"Fill persistence failed: {exc}")
                            continue
                        report.persisted += 1
                        if self.on_fill is not None:
                            try:
                                self.on_fill(fill)
                            except Exception as exc:
                                report.failed += 1
                                if first_persistence_error is None:
                                    first_persistence_error = exc
                                    report.warnings.append(f"Fill accounting callback failed: {exc}")
                                raise
                    if not next_cursor:
                        break
                    seen.add(next_cursor)
                    cursor = next_cursor
            self._check_stop(stop)
        except Exception as exc:
            raise ReconciliationError(f"fill reconciliation failed: {exc}", report) from exc
        if first_persistence_error is not None:
            raise ReconciliationError(
                f"fill reconciliation failed: {report.failed} fill persistence failure(s): {first_persistence_error}",
                report,
            ) from first_persistence_error
        return report