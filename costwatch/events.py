"""Async fan-out bus for SSE clients.

The poller publishes one payload per tick. Each connected dashboard owns its
own bounded queue. New subscribers receive the most-recent cached payload
immediately so the UI doesn't sit blank for a poll interval.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional


class EventBus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._last: Optional[Any] = None

    @property
    def last(self) -> Optional[Any]:
        return self._last

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=10)
        self._subs.add(q)
        if self._last is not None:
            await q.put(self._last)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    async def publish(self, payload: Any) -> None:
        self._last = payload
        for q in list(self._subs):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # slow consumer; drop and continue. SSE clients catch up on next tick.
                pass
