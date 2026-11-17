"""Background, replay-safe exchange fill reconciliation."""

import asyncio
import logging
import time

from auth.kalshi_auth import get_auth_headers
from config import BASE_URL
from data.fill_reconciliation import FillReconciler


logger = logging.getLogger("FillSync")


class FillSync:
    def __init__(self, store, websocket, *, reconciler=None, interval=3600, retry_interval=60, poll_interval=1):
        self.store = store
        self.websocket = websocket
        self.reconciler = reconciler or FillReconciler(store, BASE_URL, get_auth_headers, min_interval=1.0)
        self.interval = interval
        self.retry_interval = retry_interval
        self.poll_interval = poll_interval

    async def run(self):
        last_generation = None
        next_scan = 0.0
        while True:
            generation = self.websocket.connection_generation
            now = time.monotonic()
            if self.websocket.is_connected and (generation != last_generation or now >= next_scan):
                try:
                    if not await asyncio.to_thread(self.store.initialize):
                        raise RuntimeError("fill database initialization failed")
                    report = await self.reconciler.reconcile()
                    logger.info("Fill reconciliation visited %s pages and persisted %s records; completeness is not guaranteed",
                                report.pages, report.persisted)
                    last_generation = generation
                    next_scan = time.monotonic() + self.interval
                except Exception:
                    logger.exception("Fill reconciliation failed; retrying")
                    # A failed generation still needs a retry, but not a tight loop.
                    last_generation = generation
                    next_scan = time.monotonic() + self.retry_interval
            await asyncio.sleep(self.poll_interval)