"""Synthetic robot X/Y, for running the custom view while the robot of the
default ``config.yaml`` is simulated.

During each PROD cycle the "robot" rasters the scan area — SCAN_LINES serpentine
passes along X, stepping in Y — spread over the cycle (its duration estimated
from the previous one). The power meter is real: its readings are painted onto
this synthetic path.

Delete this file once the workspace runs on a real robot.
"""

SCAN_LINES = 9
_FIRST_CYCLE_ESTIMATE_S = 2.5


class SimulatedScan:
    """Follows cycle events on the hub and computes where the scan is."""

    def __init__(self, hub, x_range, y_range):
        self._hub = hub
        self._x0, self._x1 = x_range
        self._y0, self._y1 = y_range
        # (cycle start t, expected duration s) — swapped as one tuple so the
        # sampler threads never see a half-updated scan.
        self._scan = None
        self._last_duration_s = _FIRST_CYCLE_ESTIMATE_S
        hub.on("cycle_start", self._on_cycle_start)
        hub.on("cycle_end", self._on_cycle_end)

    def _on_cycle_start(self, hub, event):
        self._scan = (event["t"], self._last_duration_s)

    def _on_cycle_end(self, hub, event):
        if "start_t" in event:
            self._last_duration_s = max(0.5, event["t"] - event["start_t"])

    def robot_xy(self):
        scan = self._scan
        if scan is None:
            return [self._x0, self._y0]
        start_t, duration_s = scan
        progress = min(1.0, max(0.0, (self._hub.now() - start_t) / duration_s))
        position = progress * SCAN_LINES
        line = min(SCAN_LINES - 1, int(position))
        along = position - line
        if line % 2:
            along = 1.0 - along
        return [
            self._x0 + along * (self._x1 - self._x0),
            self._y0 + line / (SCAN_LINES - 1) * (self._y1 - self._y0),
        ]
