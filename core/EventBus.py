"""
core/EventBus.py
----------------
Fan-out of server-sent events (``/api/events``) to every open page.

One bounded queue per subscriber. A slow client never blocks the publisher
or the other clients: when its queue is full its oldest event is dropped
(the next ``status``/``devices``/``prod`` event supersedes it anyway). Each
SSE client holds one server thread, so subscribers are capped; at the cap the
oldest subscriber is evicted, which also frees threads left by clients that
vanished without closing (a reloaded tab whose socket is not yet detected).
"""

from __future__ import annotations

import queue
import threading
from typing import Any, List, Tuple

#: Returned by :meth:`Subscription.get` once the subscription is closed.
CLOSED = object()


class Subscription:
    def __init__(self, maxsize: int):
        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=maxsize)
        self.closed = False

    def _put(self, item: Any) -> None:
        while True:
            try:
                self._queue.put_nowait(item)
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()   # drop the oldest
                except queue.Empty:
                    pass

    def get(self, timeout: float) -> Tuple[str, str] | None | object:
        """Next ``(event, data)``, ``None`` on timeout, or :data:`CLOSED`."""
        if self.closed and self._queue.empty():
            return CLOSED
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return CLOSED if self.closed else None

    def close(self) -> None:
        self.closed = True
        self._put(CLOSED)


class EventBus:
    """Thread-safe publish/subscribe for pre-serialised SSE payloads."""

    def __init__(self, max_subscribers: int = 20, queue_size: int = 100):
        self.max_subscribers = max_subscribers
        self.queue_size = queue_size
        self._lock = threading.Lock()
        self._subscribers: List[Subscription] = []
        self._closed = False

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def subscribe(self) -> Subscription | None:
        """A new subscription, or None once the bus is closed (process exit)."""
        sub = Subscription(self.queue_size)
        evicted = None
        with self._lock:
            if self._closed:
                return None
            if len(self._subscribers) >= self.max_subscribers:
                evicted = self._subscribers.pop(0)
            self._subscribers.append(sub)
        if evicted is not None:
            evicted.close()
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subscribers:
                self._subscribers.remove(sub)

    def publish(self, event: str, data: str) -> None:
        """Queue ``data`` (a JSON string) as SSE event ``event`` for every subscriber."""
        with self._lock:
            subscribers = list(self._subscribers)
        for sub in subscribers:
            sub._put((event, data))

    def close(self) -> None:
        """Wake and end every stream; refuse new subscribers."""
        with self._lock:
            self._closed = True
            subscribers, self._subscribers = self._subscribers, []
        for sub in subscribers:
            sub.close()
