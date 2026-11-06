"""
core/LivePublisher.py
---------------------
Turns the state the UI used to poll into server-sent events.

Each source is a producer (the same function a REST route uses) and a
period. While at least one page is subscribed, a single thread calls each
producer at its period and publishes the result **only when it changed** —
so the live step (whose elapsed time moves) streams at 10 Hz while a task
runs, and an idle cell sends nothing but heartbeats. With no subscriber the
thread does no work at all.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

from .EventBus import EventBus


@dataclass
class LiveSource:
    name: str                          # SSE event name
    produce: Callable[[], Any]         # JSON-serialisable payload
    period_s: float
    _next: float = field(default=0.0, repr=False)
    _last: str | None = field(default=None, repr=False)
    _failed: bool = field(default=False, repr=False)


class LivePublisher:
    TICK_S = 0.05

    def __init__(self, bus: EventBus, sources: List[LiveSource], logger):
        self.bus = bus
        self.sources = sources
        self.logger = logger
        self._lock = threading.Lock()          # producers vs. current()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="LivePublisher", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _serialise(self, source: LiveSource) -> str | None:
        try:
            payload = json.dumps(source.produce(), default=str, separators=(",", ":"))
        except Exception as e:
            if not source._failed:
                source._failed = True
                self.logger.error("Live source '%s' failed: %s", source.name, e, exc_info=True)
            return None
        if source._failed:
            source._failed = False
            self.logger.info("Live source '%s' recovered.", source.name)
        return payload

    def current(self) -> Dict[str, str]:
        """Every source's payload, freshly produced: the burst a new page receives on connect."""
        out = {}
        with self._lock:
            for source in self.sources:
                payload = self._serialise(source)
                if payload is not None:
                    source._last = payload
                    out[source.name] = payload
        return out

    def _run(self) -> None:
        while not self._stop.wait(self.TICK_S):
            if self.bus.subscriber_count == 0:
                continue
            now = time.monotonic()
            with self._lock:
                for source in self.sources:
                    if now < source._next:
                        continue
                    source._next = now + source.period_s
                    payload = self._serialise(source)
                    if payload is None or payload == source._last:
                        continue
                    source._last = payload
                    self.bus.publish(source.name, payload)
