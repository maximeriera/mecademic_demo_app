"""Synthetic robot X and height-sensor signals, for running the custom view on
the simulated devices of the default ``config.yaml``.

During each PROD cycle the "robot" sweeps X from X_START_MM to X_END_MM over
the cycle (its duration estimated from the previous one), and the "sensor"
reads the height of the part under it. Every DEFECT_EVERY_N-th cycle the part
has a bump, which :func:`custom_view.analyse_cycle` should reject.

Delete this file once the workspace runs on real hardware.
"""

import math
import random

X_START_MM = 150.0
X_END_MM = 250.0
NOMINAL_HEIGHT_MM = 12.0
DEFECT_EVERY_N = 4
DEFECT_CENTER_MM = 205.0
DEFECT_HEIGHT_MM = 1.2
DEFECT_HALF_WIDTH_MM = 7.0
_FIRST_CYCLE_ESTIMATE_S = 2.5


class SimulatedScan:
    """Follows cycle events on the hub and computes where the scan is."""

    def __init__(self, hub):
        self._hub = hub
        # (cycle start t, expected duration s, defective?) — swapped as one
        # tuple so the sampler threads never see a half-updated scan.
        self._scan = None
        self._last_duration_s = _FIRST_CYCLE_ESTIMATE_S
        hub.on("cycle_start", self._on_cycle_start)
        hub.on("cycle_end", self._on_cycle_end)

    def _on_cycle_start(self, hub, event):
        cycle = event["data"].get("cycle") or 0
        self._scan = (event["t"], self._last_duration_s, cycle % DEFECT_EVERY_N == 0)

    def _on_cycle_end(self, hub, event):
        if "start_t" in event:
            self._last_duration_s = max(0.5, event["t"] - event["start_t"])

    def robot_x(self):
        scan = self._scan
        if scan is None:
            return X_START_MM
        start_t, duration_s, _ = scan
        progress = min(1.0, max(0.0, (self._hub.now() - start_t) / duration_s))
        return X_START_MM + progress * (X_END_MM - X_START_MM)

    def sensor(self):
        x = self.robot_x()
        height = NOMINAL_HEIGHT_MM + 0.15 * math.sin(x / 6.0)
        scan = self._scan
        if scan is not None and scan[2]:
            offset = (x - DEFECT_CENTER_MM) / DEFECT_HALF_WIDTH_MM
            height += DEFECT_HEIGHT_MM * math.exp(-offset * offset)
        return height + random.gauss(0.0, 0.04)
