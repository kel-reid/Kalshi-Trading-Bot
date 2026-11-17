import asyncio
import threading
from datetime import datetime, timezone

import pytest
import requests

from data.fill_reconciliation import CURRENT, HISTORICAL, FillReconciler, ReconciliationError
from scripts.backfill_fills import main


class Response:
    def __init__(self, data=None, status=200, headers=None):
        self.data = data
        self.status_code = status
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self.data


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class Store:
    def __init__(self, fail=None):
        self.ids = set()
        self.fail = fail

    def record(self, fill, source):
        assert source == "rest"
        if fill["fill_id"] == self.fail:
            raise RuntimeError("database unavailable")
        self.ids.add(fill["fill_id"])
        return True


def page(fills=(), cursor=""):
    return Response({"fills": list(fills), "cursor": cursor})


def fill(identifier, when="2026-11-01T05:30:00Z"):
    return {"fill_id": identifier, "created_time": when}


def run(store, session, start=None, end=None, **kwargs):
    reconciler = FillReconciler(store, "https://example.invalid", lambda method, path: {"signed": method + path},
                                session=session, min_interval=0, **kwargs)
    return asyncio.run(reconciler.reconcile(start, end))


def test_all_pages_overlap_and_repeated_full_scan():
    store = Store()
    session = Session([page([fill("a")], "next"), page([fill("b")]),
                       page([fill("a"), fill("c")]),
                       page([fill("a"), fill("b")]), page([fill("a"), fill("c")])])
    first = run(store, session)
    second = run(store, session)
    assert (first.pages, first.fetched, first.persisted) == (3, 4, 4)
    assert second.persisted == 4
    assert store.ids == {"a", "b", "c"}
    assert not first.complete and first.warnings
    assert session.calls[0][0].endswith(CURRENT)
    assert session.calls[1][1]["params"]["cursor"] == "next"
    assert session.calls[2][0].endswith(HISTORICAL)
    assert session.calls[2][1]["headers"]["signed"] == "GET" + HISTORICAL
    assert "min_ts" not in session.calls[3][1]["params"]


def test_dst_range_and_exact_boundaries():
    start = "2026-10-31T00:00:00-04:00"
    end = "2026-11-02T00:00:00-05:00"
    rows = [fill("before", "2026-10-31T03:59:59.999999Z"),
            fill("start", "2026-10-31T04:00:00Z"),
            fill("first-130", "2026-11-01T01:30:00-04:00"),
            fill("second-130", "2026-11-01T01:30:00-05:00"),
            fill("last", "2026-11-02T04:59:59.999999Z"),
            fill("end", "2026-11-02T05:00:00Z")]
    store = Store()
    session = Session([page(rows), page()])
    result = run(store, session, start, end)
    assert store.ids == {"start", "first-130", "second-130", "last"}
    assert result.fetched == 6 and result.in_range == 4
    params = session.calls[0][1]["params"]
    assert params["min_ts"] == int(datetime(2026, 10, 31, 4, tzinfo=timezone.utc).timestamp()) - 1
    assert params["max_ts"] == int(datetime(2026, 11, 2, 5, tzinfo=timezone.utc).timestamp()) + 1


@pytest.mark.parametrize("start,end", [("2026-11-01T01:00:00", "2026-11-02T00:00:00Z"),
                                        ("2026-11-02T00:00:00Z", "2026-11-02T00:00:00Z")])
def test_bad_ranges_do_not_fetch(start, end):
    session = Session([])
    with pytest.raises(ValueError):
        run(Store(), session, start, end)
    assert not session.calls


def test_historical_page_failure_can_be_replayed():
    store = Store()
    session = Session([page([fill("a")]), page([fill("b")], "next"), Response(status=503),
                       page([fill("a")]), page([fill("b")], "next"), page([fill("c")])])
    with pytest.raises(ReconciliationError) as error:
        run(store, session, retries=0)
    assert error.value.report.persisted == 2
    result = run(store, session, retries=0)
    assert result.persisted == 3
    assert store.ids == {"a", "b", "c"}


def test_store_failure_is_not_treated_as_duplicate():
    store = Store(fail="b")
    with pytest.raises(ReconciliationError, match="database unavailable") as error:
        run(store, Session([page([fill("a"), fill("b")]), page()]))
    assert error.value.report.persisted == 1
    store.fail = None
    result = run(store, Session([page([fill("a"), fill("b")]), page()]))
    assert result.persisted == 2
    assert store.ids == {"a", "b"}


@pytest.mark.parametrize("bad", [page([], "loop"), Response({"fills": [], "cursor": 123}),
                                   Response({"fills": []})])
def test_invalid_or_looping_cursor_fails(bad):
    session = Session([page([], "loop"), bad])
    with pytest.raises(ReconciliationError):
        run(Store(), session)


def test_retries_rate_limit_and_connection_errors(monkeypatch):
    sleeps = []
    monkeypatch.setattr(FillReconciler, "_wait", staticmethod(lambda stop, delay: sleeps.append(delay)))
    session = Session([Response(status=429, headers={"Retry-After": "2"}), requests.Timeout(),
                       page([fill("a")]), page()])
    result = run(Store(), session, retries=2)
    assert result.persisted == 1 and len(session.calls) == 4
    assert 2 in sleeps


def test_historical_is_required_even_when_current_succeeds():
    with pytest.raises(ReconciliationError) as error:
        run(Store(), Session([page(), Response(status=404)]), retries=0)
    assert error.value.report.pages == 1 and not error.value.report.complete


def test_missing_timestamp_fails_explicit_range():
    with pytest.raises(ReconciliationError, match="UTC offset"):
        run(Store(), Session([page([{"fill_id": "a"}])]), "2026-10-31T00:00:00-04:00")


def test_ts_only_timestamp_succeeds_explicit_range():
    # 2026-11-01T04:00:00Z epoch is 1793505600
    store = Store()
    ts_fill = {"fill_id": "ts_only", "ts": 1793505600}
    result = run(store, Session([page([ts_fill]), page()]), "2026-10-31T00:00:00-04:00", "2026-11-02T00:00:00-05:00")
    assert result.persisted == 1
    assert store.ids == {"ts_only"}


def test_cli_dry_run_does_not_import_store_or_connect(capsys):
    assert main(["--start", "2026-10-31T00:00:00-04:00", "--end", "2026-11-02T00:00:00-05:00"]) == 0
    assert "Dry run" in capsys.readouterr().out


def test_store_success_means_committed_including_duplicates():
    class CommittedStore:
        def record(self, fill, source):
            return True

    result = run(CommittedStore(), Session([page([fill("a")]), page([fill("a")])]))
    assert result.persisted == 2


def test_store_false_fails_reconciliation():
    class FailedStore:
        def record(self, fill, source):
            return False

    with pytest.raises(ReconciliationError, match="persist") as error:
        run(FailedStore(), Session([page([fill("a")]), page()]))
    assert error.value.report.persisted == 0


@pytest.mark.parametrize("failure", ["exception", "false"])
def test_persistence_failure_does_not_block_later_pages_or_tiers_and_retries(failure):
    class PartiallyFailingStore(Store):
        def record(self, row, source):
            if row["fill_id"] == "bad" and self.fail:
                if failure == "exception":
                    raise RuntimeError("database unavailable")
                return False
            return super().record(row, source)

    store = PartiallyFailingStore(fail="enabled")
    responses = lambda: [page([fill("bad"), fill("same-page")], "next"),
                         page([fill("later-page")]), page([fill("historical")])]
    session = Session(responses() + responses())
    with pytest.raises(ReconciliationError) as error:
        run(store, session)
    report = error.value.report
    assert (report.pages, report.fetched, report.in_range, report.persisted) == (3, 4, 4, 3)
    assert report.failed == 1 and not report.complete
    assert store.ids == {"same-page", "later-page", "historical"}
    assert session.calls[2][0].endswith(HISTORICAL)
    store.fail = None
    result = run(store, session)
    assert result.failed == 0 and result.persisted == 4
    assert store.ids == {"bad", "same-page", "later-page", "historical"}
    assert "cursor" not in session.calls[3][1]["params"]


def test_fetch_failure_after_store_failure_preserves_both_in_report():
    session = Session([page([fill("bad"), fill("good")], "next"), Response(status=503)])
    with pytest.raises(ReconciliationError, match="503") as error:
        run(Store(fail="bad"), session, retries=0)
    report = error.value.report
    assert (report.failed, report.persisted, report.pages) == (1, 1, 1)
    assert any("database unavailable" in warning for warning in report.warnings)
    assert not report.complete and len(session.calls) == 2


def test_cancellation_stops_paginated_scan_and_holds_lock_until_worker_exits():
    entered = threading.Event()
    release = threading.Event()
    entries = []

    class BlockingSession(Session):
        def get(self, url, **kwargs):
            entries.append(url)
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test timed out")
            return super().get(url, **kwargs)

    async def exercise():
        store = Store()
        session = BlockingSession([page([fill("a")], "next"), page([fill("b")]), page()])
        reconciler = FillReconciler(store, "https://example.invalid", lambda *_: {},
                                    session=session, min_interval=0)
        first = asyncio.create_task(reconciler.reconcile())
        second = None
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            first.cancel()
            await asyncio.sleep(0.05)
            first.cancel()
            second = asyncio.create_task(reconciler.reconcile())
            await asyncio.sleep(0.05)
            assert not first.done()
            assert not second.done()
            assert len(entries) == 1
            assert not session.calls
        finally:
            if second is not None:
                second.cancel()
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        if second is not None:
            with pytest.raises(asyncio.CancelledError):
                await second
        assert len(session.calls) == 1
        assert not store.ids

    asyncio.run(exercise())


def test_cancellation_interrupts_retry_backoff():
    requested = threading.Event()

    class RetrySession(Session):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            requested.set()
            return response

    async def exercise():
        session = RetrySession([Response(status=429, headers={"Retry-After": "2"}), page(), page()])
        reconciler = FillReconciler(Store(), "https://example.invalid", lambda *_: {},
                                    session=session, min_interval=0)
        task = asyncio.create_task(reconciler.reconcile())
        assert await asyncio.to_thread(requested.wait, 1)
        await asyncio.sleep(0.05)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=0.3)
        try:
            assert task in done
        finally:
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert task.cancelled()
        assert len(session.calls) == 1

    asyncio.run(exercise())


def test_cancellation_interrupts_rate_limit_wait():
    first_page = threading.Event()

    class SignalingSession(Session):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            first_page.set()
            return response

    async def exercise():
        session = SignalingSession([page([], "next"), page(), page()])
        reconciler = FillReconciler(Store(), "https://example.invalid", lambda *_: {},
                                    session=session, min_interval=2)
        task = asyncio.create_task(reconciler.reconcile())
        assert await asyncio.to_thread(first_page.wait, 1)
        await asyncio.sleep(0.05)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=0.3)
        try:
            assert task in done
        finally:
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert task.cancelled()
        assert len(session.calls) == 1

    asyncio.run(exercise())


def test_cancellation_stops_between_records():
    entered = threading.Event()
    release = threading.Event()

    class BlockingStore(Store):
        def record(self, fill, source):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test timed out")
            return super().record(fill, source)

    async def exercise():
        store = BlockingStore()
        session = Session([page([fill("a"), fill("b")]), page()])
        reconciler = FillReconciler(store, "https://example.invalid", lambda *_: {},
                                    session=session, min_interval=0)
        task = asyncio.create_task(reconciler.reconcile())
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            await asyncio.sleep(0.05)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.ids == {"a"}
        assert len(session.calls) == 1

    asyncio.run(exercise())