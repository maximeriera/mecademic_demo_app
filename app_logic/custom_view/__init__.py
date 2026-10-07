"""Custom view: live power against the threshold, and the alignment graph, on /view/.

Optional. When this package exists next to ``config.yaml``, the controller
calls :func:`setup` at every Initialize, then starts the hub. Delete the folder
and the standard app runs exactly as before.

What the page reads:

* ``power``: a source sampling the power meter at POWER_RATE_HZ, for the live
  readout. It runs on its own thread. The Thorlabs driver serializes every
  exchange, so the alignment's own readings, on the task thread, share the
  meter safely.
* ``align_sample`` / ``align_result`` and the ``align_start`` / ``align_end``
  events: published by fiber_alignment/alignment.py during the "Align fiber"
  step only. That is why the graph moves only then.
* The threshold is not on the hub. The page reads ``power_threshold_mw`` from
  /api/context, so an edit in the Variables tab shows at once, even before
  Initialize.

The page must not load anything from a CDN: show-floor networks are not
reliable. Vendor any chart library into this folder.
"""

import math

from devices.SimulatedDevice import SimulatedDevice

try:
    from ..fiber_alignment.cell import POWER_METER
except ImportError:  # pragma: no cover - depends on how the workspace is loaded
    from fiber_alignment.cell import POWER_METER

#: Live readout rate. A PM100x with ``averaging: 10`` takes ~30 ms per reading,
#: so 10 Hz leaves the meter mostly free for the alignment's own readings.
POWER_RATE_HZ = 10


def setup(hub, devices):
    """Declare the data sources for this workspace."""
    meter = devices.get(POWER_METER)
    if meter is None:
        raise LookupError(f"No device '{POWER_METER}' in config.yaml: this view reads its power.")
    if isinstance(meter, SimulatedDevice):
        raise LookupError(f"Device '{POWER_METER}' is simulated: there is no power to show.")

    # The page compares readings with a threshold in mW and draws them on a
    # linear axis; dBm would also be negative below 1 mW.
    unit = meter.api.power_unit
    if unit != "W":
        raise ValueError(
            f"Power meter '{POWER_METER}' reads in {unit}; the custom view needs watts. "
            f"Set power_unit: \"W\" on it in config.yaml and Initialize again."
        )

    def read_power():
        watts = meter.api.read_power()
        # Overrange reads +inf and an invalid reading NaN: record nothing
        # rather than a value the page cannot use.
        return watts if math.isfinite(watts) else None

    hub.add_source("power", read_power, rate_hz=POWER_RATE_HZ)
