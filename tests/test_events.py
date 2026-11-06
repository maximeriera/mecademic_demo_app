"""Server-sent events: the bus, the publisher, and /api/events."""

import json
import logging
import threading

from core.EventBus import CLOSED, EventBus
from core.LivePublisher import LivePublisher, LiveSource

from conftest import wait_for


def test_bus_fans_out_to_every_subscriber():
    bus = EventBus()
    a, b = bus.subscribe(), bus.subscribe()
    bus.publish("status", '{"x": 1}')
    assert a.get(0.1) == ("status", '{"x": 1}')
    assert b.get(0.1) == ("status", '{"x": 1}')
    assert a.get(0.01) is None          # heartbeat timeout


def test_a_slow_subscriber_loses_its_oldest_events_only():
    bus = EventBus(queue_size=3)
    sub = bus.subscribe()
    for i in range(10):
        bus.publish("status", str(i))
    assert [sub.get(0.1)[1] for _ in range(3)] == ["7", "8", "9"]


def test_the_oldest_subscriber_is_evicted_at_the_cap():
    bus = EventBus(max_subscribers=2)
    first, _second = bus.subscribe(), bus.subscribe()
    bus.subscribe()
    assert bus.subscriber_count == 2
    assert first.get(0.1) is CLOSED


def test_close_wakes_a_waiting_stream_and_refuses_new_ones():
    bus = EventBus()
    sub = bus.subscribe()
    results = []
    thread = threading.Thread(target=lambda: results.append(sub.get(5.0)))
    thread.start()
    bus.close()
    thread.join(2)
    assert results == [CLOSED]
    assert bus.subscribe() is None


def test_publisher_sends_changes_only_and_only_with_subscribers():
    bus = EventBus()
    value = {"n": 0}
    calls = []

    def produce():
        calls.append(1)
        return dict(value)

    publisher = LivePublisher(bus, [LiveSource("status", produce, 0.01)], logging.getLogger("test"))
    publisher.start()
    try:
        threading.Event().wait(0.1)
        assert calls == []                      # nobody listening: no work
        sub = bus.subscribe()
        publisher.current()                     # the connect burst sets the baseline
        threading.Event().wait(0.15)
        assert sub.get(0.01) is None            # unchanged: nothing published
        value["n"] = 1
        wait_for(lambda: sub._queue.qsize() > 0, timeout=2)
        assert sub.get(0.1) == ("status", '{"n":1}')
    finally:
        publisher.stop()


def _read_events(response, count):
    """Parse the first *count* SSE events of a streamed Flask test response."""
    events, buffer = [], ""
    for chunk in response.response:
        buffer += chunk.decode() if isinstance(chunk, bytes) else chunk
        while "\n\n" in buffer:
            block, buffer = buffer.split("\n\n", 1)
            lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
            if "event" in lines:
                events.append((lines["event"], json.loads(lines["data"])))
                if len(events) == count:
                    return events
    return events


def test_events_stream_starts_with_a_full_snapshot(flask_app):
    client = flask_app.app.test_client()
    response = client.get("/api/events", buffered=False)
    try:
        assert response.mimetype == "text/event-stream"
        assert response.headers["X-Accel-Buffering"] == "no"
        events = dict(_read_events(response, 4))
        assert set(events) == {"status", "devices", "prod", "context"}
        assert events["status"]["status"] == "Off"
        assert [d["device_id"] for d in events["devices"]] == ["cell"]
    finally:
        response.close()


def test_notices_reach_the_stream(flask_app):
    client = flask_app.app.test_client()
    response = client.get("/api/events", buffered=False)
    try:
        _read_events(response, 4)               # skip the snapshot
        flask_app._publish_notice("error", "cell faulted")
        (event,) = _read_events(response, 1)
        assert event[0] == "notice" and event[1]["message"] == "cell faulted"
    finally:
        response.close()
