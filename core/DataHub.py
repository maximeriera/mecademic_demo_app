"""
core/DataHub.py
---------------
In-memory store for the dynamic data behind a workspace's optional custom view.

Three kinds of writers, all independent of each other:

* **sources** — one sampler thread per source, each at its own rate, reading a
  device while the task thread is busy commanding it (``prod_cycle`` blocks in
  ``WaitIdle()`` during a move, so it cannot sample anything itself);
* **task code** — ``context.publish()`` / ``context.mark()`` from a cycle;
* **analysis** — callbacks registered with :meth:`DataHub.on`, run on one
  dedicated worker so slow or failing analysis never delays or faults the cell.

Every sample is stamped on one common clock (seconds since the hub's epoch), so
channels from different devices can be joined with :meth:`DataHub.align`.
Memory only: each channel is a bounded ring buffer.

Readers poll incrementally with a cursor (a sequence number assigned when a
sample is stored), not with a time: a sampler stamps a reading at the middle of
a possibly slow read, so it can land *after* a poll with an *earlier* ``t``.
"""

import logging
import queue
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

EVENTS_CHANNEL = "events"

Sample = Tuple[float, Any]


class _Source:
    __slots__ = ("name", "read", "rate_hz", "errors", "last_error", "_last_logged")

    def __init__(self, name: str, read: Callable[[], Any], rate_hz: float):
        self.name = name
        self.read = read
        self.rate_hz = rate_hz
        self.errors = 0
        self.last_error: Optional[str] = None
        self._last_logged = 0.0


class DataHub:
    """Thread-safe named ring buffers on a common clock, plus samplers and analysis."""

    DEFAULT_MAXLEN = 2000
    EVENTS_MAXLEN = 1000
    _ERROR_LOG_INTERVAL_S = 10.0

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self._lock = threading.Lock()
        self._epoch = time.monotonic()
        self._epoch_utc = datetime.now(timezone.utc).isoformat()

        # Each buffer holds (seq, t, value); seq is global and strictly increasing.
        self._channels: Dict[str, deque] = {}
        self._seq = 0
        self._sources: Dict[str, _Source] = {}
        self._callbacks: Dict[str, List[Callable[["DataHub", Dict[str, Any]], None]]] = {}
        self._open_marks: Dict[str, float] = {}
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()

        # A fresh Event per run: a sampler stuck in a blocking read past stop()
        # still sees *its* run as stopped, even after a later start().
        self._run_stop: Optional[threading.Event] = None
        self._threads: List[threading.Thread] = []

        self.setup_error: Optional[str] = None
        self._analysis_errors = 0
        self._last_analysis_error: Optional[str] = None

    # --- Clock ------------------------------------------------------------

    def now(self) -> float:
        """Current time on the hub's clock (seconds since its epoch)."""
        return time.monotonic() - self._epoch

    # --- Writing ------------------------------------------------------------

    def _append_locked(self, channel: str, t: float, value: Any, maxlen: int) -> None:
        buf = self._channels.get(channel)
        if buf is None:
            buf = deque(maxlen=maxlen)
            self._channels[channel] = buf
        self._seq += 1
        buf.append((self._seq, t, value))

    def publish(self, channel: str, value: Any, t: Optional[float] = None) -> None:
        """Append one value to ``channel``. ``t`` defaults to now."""
        if channel == EVENTS_CHANNEL:
            raise ValueError(f"'{EVENTS_CHANNEL}' is reserved; use mark() for events.")
        with self._lock:
            self._append_locked(channel, self.now() if t is None else t, value, self.DEFAULT_MAXLEN)

    def mark(self, name: str, **data: Any) -> None:
        """Record a timestamped event and hand it to any analysis registered for it.

        An event named ``<x>_end`` carries ``start_t``, the time of the latest
        ``<x>_start`` — so ``cycle_end`` knows where its cycle began, and a
        workspace can bracket any segment the same way (``scan_start``/``scan_end``).
        """
        with self._lock:
            t = self.now()
            event: Dict[str, Any] = {"name": name, "t": t, "data": data}
            if name.endswith("_start"):
                self._open_marks[name[:-len("_start")]] = t
            elif name.endswith("_end"):
                start_t = self._open_marks.pop(name[:-len("_end")], None)
                if start_t is not None:
                    event["start_t"] = start_t
            self._append_locked(EVENTS_CHANNEL, t, event, self.EVENTS_MAXLEN)
            wanted = name in self._callbacks and self._run_stop is not None
        if wanted:
            self._queue.put(event)

    # --- Reading ------------------------------------------------------------

    def latest(self, channel: str, default: Any = None) -> Any:
        """Most recent value of ``channel``, or ``default`` if it has none."""
        with self._lock:
            buf = self._channels.get(channel)
            return buf[-1][2] if buf else default

    def read(self, channels: Iterable[str],
             after: Optional[int] = None) -> Tuple[int, Dict[str, List[Sample]]]:
        """Samples stored after cursor ``after`` (all if ``None``) for each channel.

        Returns ``(cursor, data)``: pass ``cursor`` back as ``after`` next time.
        """
        out: Dict[str, List[Sample]] = {}
        with self._lock:
            for channel in channels:
                buf = self._channels.get(channel)
                newer: List[Sample] = []
                for seq, t, value in reversed(buf or ()):
                    if after is not None and seq <= after:
                        break
                    newer.append((t, value))
                newer.reverse()
                out[channel] = newer
            return self._seq, out

    def window(self, channel: str, t0: Optional[float], t1: Optional[float] = None) -> List[Sample]:
        """Samples of ``channel`` with ``t0 <= t <= t1`` (open-ended where ``None``)."""
        with self._lock:
            buf = self._channels.get(channel)
            if not buf:
                return []
            return [
                (t, value) for _, t, value in buf
                if (t0 is None or t >= t0) and (t1 is None or t <= t1)
            ]

    @staticmethod
    def align(ref: List[Sample], other: List[Sample]) -> List[Tuple[Any, float]]:
        """Pair each ``ref`` value with ``other`` linearly interpolated at the same ``t``.

        Both inputs are time-ordered sample lists (as returned by :meth:`window`);
        ``other`` must be numeric. ``ref`` samples outside ``other``'s time span
        are dropped. Typical use: ``align(robot_x, sensor)`` → ``[(x, sensor), ...]``.
        """
        pairs: List[Tuple[Any, float]] = []
        n = len(other)
        if not ref or n == 0:
            return pairs
        first_t, last_t = other[0][0], other[-1][0]
        j = 0
        for t, ref_value in ref:
            if t < first_t or t > last_t:
                continue
            while j + 1 < n and other[j + 1][0] < t:
                j += 1
            t_a, v_a = other[j]
            if j + 1 < n and other[j + 1][0] > t_a:
                t_b, v_b = other[j + 1]
                frac = (t - t_a) / (t_b - t_a)
                pairs.append((ref_value, float(v_a) + frac * (float(v_b) - float(v_a))))
            else:
                pairs.append((ref_value, float(v_a)))
        return pairs

    # --- Workspace registration (from custom_view.setup) ---------------------

    def add_source(self, name: str, read: Callable[[], Any], rate_hz: float,
                   history_s: float = 120.0) -> None:
        """Sample ``read()`` into channel ``name`` at ``rate_hz`` on its own thread.

        ``read`` runs concurrently with the task thread, so it must only touch a
        device whose read path is thread-safe (e.g. mecademicpy's cached
        ``GetRtTargetCartPos()`` — never ``synchronous_update=True``), or a device
        this source owns exclusively; task code then reads the value with
        ``context.latest(name)`` instead of querying the device itself.

        Returning ``None`` records nothing. Exceptions are counted and logged at
        most every few seconds; sampling carries on.
        """
        if rate_hz <= 0:
            raise ValueError(f"Source '{name}': rate_hz must be > 0.")
        if name == EVENTS_CHANNEL:
            raise ValueError(f"'{EVENTS_CHANNEL}' is reserved.")
        with self._lock:
            if self._run_stop is not None:
                raise RuntimeError("Sources must be added before the hub is started.")
            if name in self._sources:
                raise ValueError(f"Source '{name}' is already registered.")
            self._sources[name] = _Source(name, read, rate_hz)
            self._channels[name] = deque(maxlen=max(1, int(rate_hz * history_s)))

    def on(self, event_name: str, fn: Callable[["DataHub", Dict[str, Any]], None]) -> None:
        """Run ``fn(hub, event)`` on the analysis worker each time ``event_name`` is marked."""
        with self._lock:
            self._callbacks.setdefault(event_name, []).append(fn)

    # --- Lifecycle ------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._run_stop is not None

    def start(self) -> None:
        """Start one thread per source, plus the analysis worker if anything is registered."""
        with self._lock:
            if self._run_stop is not None:
                return
            stop = threading.Event()
            self._run_stop = stop
            sources = list(self._sources.values())
            has_callbacks = bool(self._callbacks)
        threads = [
            threading.Thread(target=self._run_source, args=(source, stop),
                             name=f"DataHub-{source.name}", daemon=True)
            for source in sources
        ]
        if has_callbacks:
            threads.append(threading.Thread(target=self._run_analysis, args=(stop,),
                                            name="DataHub-analysis", daemon=True))
        self._threads = threads
        for thread in threads:
            thread.start()
        self.logger.info("DataHub started: %d source(s), analysis=%s.", len(sources), has_callbacks)

    def stop(self, timeout: float = 2.0) -> None:
        """Signal every thread of the current run and wait up to ``timeout`` in total."""
        with self._lock:
            stop, self._run_stop = self._run_stop, None
        if stop is None:
            return
        stop.set()
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                self.logger.warning("DataHub thread %s did not stop within %.1fs (blocked read?).",
                                    thread.name, timeout)
        self._threads = []
        self.logger.info("DataHub stopped.")

    def clear(self) -> None:
        """Drop sources, callbacks, buffers and errors. Stops the hub first if running."""
        self.stop()
        with self._lock:
            self._channels.clear()
            self._sources.clear()
            self._callbacks.clear()
            self._open_marks.clear()
            self._queue = queue.Queue()
            self.setup_error = None
            self._analysis_errors = 0
            self._last_analysis_error = None

    def describe(self) -> Dict[str, Any]:
        """Manifest of the hub's state, for the API."""
        with self._lock:
            channels = {}
            for name, buf in self._channels.items():
                source = self._sources.get(name)
                channels[name] = {
                    "count": len(buf),
                    "last_t": buf[-1][1] if buf else None,
                    "rate_hz": source.rate_hz if source else None,
                    "errors": source.errors if source else 0,
                    "last_error": source.last_error if source else None,
                }
            return {
                "active": self._run_stop is not None,
                "setup_error": self.setup_error,
                "epoch_utc": self._epoch_utc,
                "now": self.now(),
                "channels": channels,
                "analysis_errors": self._analysis_errors,
                "last_analysis_error": self._last_analysis_error,
            }

    # --- Threads ------------------------------------------------------------

    def _run_source(self, source: _Source, stop: threading.Event) -> None:
        period = 1.0 / source.rate_hz
        next_tick = time.monotonic()
        while not stop.is_set():
            t_before = time.monotonic()
            try:
                value = source.read()
            except Exception as e:
                self._record_source_error(source, e)
            else:
                # Stamp mid-call: halves the bias a slow socket read would add.
                t = (t_before + time.monotonic()) / 2 - self._epoch
                if value is not None and not stop.is_set():
                    with self._lock:
                        self._append_locked(source.name, t, value, self.DEFAULT_MAXLEN)
            next_tick += period
            delay = next_tick - time.monotonic()
            if delay < 0:
                # Overran: drop the missed ticks rather than bursting to catch up.
                next_tick = time.monotonic()
                delay = 0.0
            stop.wait(delay)

    def _record_source_error(self, source: _Source, error: Exception) -> None:
        now = time.monotonic()
        with self._lock:
            source.errors += 1
            source.last_error = f"{type(error).__name__}: {error}"
            should_log = now - source._last_logged >= self._ERROR_LOG_INTERVAL_S
            if should_log:
                source._last_logged = now
            count = source.errors
        if should_log:
            self.logger.warning("DataHub source '%s' read failed (%d error(s) so far): %s",
                                source.name, count, source.last_error)

    def _run_analysis(self, stop: threading.Event) -> None:
        events = self._queue
        while not stop.is_set():
            try:
                event = events.get(timeout=0.2)
            except queue.Empty:
                continue
            with self._lock:
                callbacks = list(self._callbacks.get(event["name"], ()))
            for fn in callbacks:
                try:
                    fn(self, event)
                except Exception as e:
                    with self._lock:
                        self._analysis_errors += 1
                        self._last_analysis_error = f"{event['name']}: {type(e).__name__}: {e}"
                    self.logger.warning("DataHub analysis for '%s' failed: %s", event["name"], e,
                                        exc_info=True)
