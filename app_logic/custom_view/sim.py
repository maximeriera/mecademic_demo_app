"""Synthetic robot X/Y and height-sensor signals, for running the custom view on
the simulated devices of the default ``config.yaml``.

During each PROD cycle the "robot" rasters the scan area — SCAN_LINES serpentine
passes along X, stepping in Y — spread over the cycle (its duration estimated
from the previous one), and the "sensor" reads the height of the part under
it. Every DEFECT_EVERY_N-th cycle the part carries a bump at a random spot,
which :func:`custom_view.analyse_cycle` should reject.

Delete this file once the workspace runs on real hardware.
"""

import math
import random

SCAN_LINES = 9
DEFECT_EVERY_N = 4
DEFECT_HEIGHT_MM = 1.5
DEFECT_SIGMA_MM = 8.0
_FIRST_CYCLE_ESTIMATE_S = 2.5


class SimulatedScan:
    """Follows cycle events on the hub and computes where the scan is."""

    def __init__(self, hub, x_range, y_range, nominal_height):
        self._hub = hub
        self._x0, self._x1 = x_range
        self._y0, self._y1 = y_range
        self._nominal = nominal_height
        # (cycle start t, expected duration s, defect (x, y) or None) — swapped
        # as one tuple so the sampler threads never see a half-updated scan.
        self._scan = None
        self._last_duration_s = _FIRST_CYCLE_ESTIMATE_S
        hub.on("cycle_start", self._on_cycle_start)
        hub.on("cycle_end", self._on_cycle_end)

    def _on_cycle_start(self, hub, event):
        cycle = event["data"].get("cycle") or 0
        defect = None
        if cycle % DEFECT_EVERY_N == 0:
            defect = (random.uniform(self._x0 + 15, self._x1 - 15),
                      random.uniform(self._y0 + 10, self._y1 - 10))
        self._scan = (event["t"], self._last_duration_s, defect)

    def _on_cycle_end(self, hub, event):
        if "start_t" in event:
            self._last_duration_s = max(0.5, event["t"] - event["start_t"])

    def robot_xy(self):
        scan = self._scan
        if scan is None:
            return [self._x0, self._y0]
        start_t, duration_s, _ = scan
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

    def sensor(self):
        x, y = self.robot_xy()
        height = self._nominal + 0.15 * math.sin(x / 6.0) + 0.1 * math.cos(y / 5.0)
        scan = self._scan
        defect = scan[2] if scan is not None else None
        if defect is not None:
            d2 = (x - defect[0]) ** 2 + (y - defect[1]) ** 2
            height += DEFECT_HEIGHT_MM * math.exp(-d2 / (2 * DEFECT_SIGMA_MM ** 2))
        return height + random.gauss(0.0, 0.04)
