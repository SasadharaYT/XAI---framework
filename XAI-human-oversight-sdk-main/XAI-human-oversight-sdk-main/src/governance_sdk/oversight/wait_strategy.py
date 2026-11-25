"""Polling strategy used by sync and async approval waits."""

import asyncio
import time
from collections.abc import Callable


class PollingWaitStrategy:
    """Bounded exponential polling, with both blocking and async sleep methods."""

    def __init__(self, interval: float, maximum: float) -> None:
        self.interval = interval
        self.maximum = maximum

    def sync_wait(self, deadline: float, poll: Callable[[], object]) -> object:
        """Poll until terminal state or deadline."""
        delay = self.interval
        while True:
            value = poll()
            if value is not None:
                return value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(delay, remaining))
            delay = min(delay * 1.5, self.maximum)

    async def async_wait(self, deadline: float, poll: Callable[[], object]) -> object:
        """Poll without blocking the event loop."""
        delay = self.interval
        while True:
            value = await poll()
            if value is not None:
                return value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 1.5, self.maximum)

