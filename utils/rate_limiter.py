"""
Asynchronous Token Bucket Rate Limiter

This module implements a token bucket algorithm to throttle REST client calls. 
It ensures compliance with Kalshi API limit constraints (e.g. max 10 requests per second) 
by pausing execution tasks when tokens are exhausted.
"""

import asyncio
import threading
import time
import logging

logger = logging.getLogger(__name__)

class RateLimiter:
    """
    A dual async/sync Token Bucket rate limiter.
    """
    def __init__(self, rate: float, per: float):
        """
        :param rate: Number of tokens added per time interval
        :param per: Time interval in seconds
        """
        self.rate = rate
        self.per = per
        self.capacity = rate
        self._tokens = self.capacity
        self._last_update = time.monotonic()
        # Thread lock ensuring cross-thread and async safety when updating tokens
        self._sync_lock = threading.Lock()
        self._lock = asyncio.Lock()

    def _consume_token(self) -> float:
        """
        Attempts to consume a token under the sync lock.
        Returns 0.0 if token consumed, or the wait_time in seconds if not available.
        """
        with self._sync_lock:
            now = time.monotonic()
            elapsed = now - self._last_update
            new_tokens = elapsed * (self.rate / self.per)
            if new_tokens > 0:
                self._tokens = min(self.capacity, self._tokens + new_tokens)
                self._last_update = now

            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return 0.0

            wait_time = (1.0 - self._tokens) / (self.rate / self.per)
            return max(0.001, wait_time)

    async def acquire(self):
        """
        Wait until a token is available, then consume it asynchronously.
        """
        while True:
            wait_time = self._consume_token()
            if wait_time <= 0.0:
                return
            await asyncio.sleep(wait_time)

    def acquire_sync(self):
        """
        Wait until a token is available, then consume it synchronously.
        """
        while True:
            wait_time = self._consume_token()
            if wait_time <= 0.0:
                return
            time.sleep(wait_time)
