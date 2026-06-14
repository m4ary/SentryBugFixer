"""Tiny pub/sub broker bridging the fixer's worker threads to async WebSocket clients.

Fix jobs run in background threads; WebSocket handlers live on the asyncio event loop.
`publish()` is thread-safe (uses ``loop.call_soon_threadsafe``); subscribers are
per-job asyncio queues created inside the loop by the WebSocket handler.
"""

import asyncio
import json


class LogBroker:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self._subs: dict[str, set[asyncio.Queue]] = {}

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def subscribe(self, job_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(job_id, set()).add(queue)
        return queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue) -> None:
        subs = self._subs.get(job_id)
        if subs:
            subs.discard(queue)
            if not subs:
                self._subs.pop(job_id, None)

    def publish(self, job_id: str, message: dict) -> None:
        """Push a message to all subscribers of a job. Safe to call from any thread."""
        subs = self._subs.get(job_id)
        if not subs or self.loop is None:
            return
        data = json.dumps(message)
        for queue in list(subs):
            try:
                self.loop.call_soon_threadsafe(queue.put_nowait, data)
            except RuntimeError:
                pass


broker = LogBroker()
