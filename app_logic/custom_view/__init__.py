"""Custom view — data sources and analysis behind this workspace's /view/ page.

Optional. When this package exists next to ``config.yaml``, the controller
calls :func:`setup` at every Initialize, then starts the hub: one sampler thread
per source, plus one worker running the analysis callbacks. Delete the folder
and the standard app runs exactly as before.

This example maps optical power: the robot rasters the scan area while a
Thorlabs power meter reads the light at each spot. Two independent sources
(robot X/Y, power) share the hub's common clock; the page pairs them live into a
3D point cloud, and :func:`analyse_cycle` grids each finished cycle into a power
map and judges its uniformity. The page (``index.html`` / ``view.js`` /
``plot3d.js`` here) only reads what the hub holds.

Rules for sources
-----------------
* A source runs on its own thread, concurrently with ``prod_cycle``. Read only
  what is safe to read from another thread — e.g. mecademicpy's cached
  real-time data (``GetRtTargetCartPos``, never ``synchronous_update=True``) —
  or a device this source owns alone; task code then uses
  ``context.latest("<source>")`` instead of querying that device itself.
* To analyse one movement rather than the whole cycle, bracket it in
  ``prod_cycle`` with ``context.mark("scan_start")`` / ``context.mark("scan_end")``
  and register ``hub.on("scan_end", ...)``: the event carries ``start_t``.
* The page must not load anything from a CDN — show-floor networks are not
  reliable. Vendor any chart library into this folder.
"""

import math

from devices.SimulatedDevice import SimulatedDevice

from .sim import SimulatedScan

#: Device id of the power meter in config.yaml.
POWER_METER = "thorlabs_power_meter"
#: A PM100x averages ~3 ms per sample: with ``averaging: 10`` a reading takes
#: ~30 ms, so 20 Hz leaves headroom. A slower meter only thins the point cloud.
POWER_RATE_HZ = 20
#: Scan area in robot coordinates.
SCAN_X_MM = (150.0, 250.0)
SCAN_Y_MM = (-40.0, 40.0)
#: Largest deviation from the map's own mean, as a fraction of it, that passes.
#: Relative, so the check needs no expected power and works with any source.
UNIFORMITY_TOLERANCE = 0.10
#: Power map grid nodes along X and Y (Y matches the 9 scan lines of the sim).
GRID_NODES = (21, 9)


def setup(hub, devices):
    """Declare the data sources and analysis for this workspace."""
    robot = devices["my_meca_robot"]
    meter = devices.get(POWER_METER)
    if meter is None:
        raise LookupError(f"No device '{POWER_METER}' in config.yaml: this view reads its power.")

    # The analysis averages and compares readings, which only works on a linear
    # scale; dBm would also be negative below 1 mW.
    unit = meter.api.power_unit
    if unit != "W":
        raise ValueError(
            f"Power meter '{POWER_METER}' reads in {unit}; the custom view needs watts. "
            f"Set power_unit: \"W\" on it in config.yaml and Initialize again."
        )

    if isinstance(robot, SimulatedDevice):
        # A SimulatedDevice's api returns None (and logs every call), so it
        # cannot feed a source: synthesize the scan path instead.
        scan = SimulatedScan(hub, SCAN_X_MM, SCAN_Y_MM)
        hub.add_source("robot_xy", scan.robot_xy, rate_hz=50)
    else:
        # Cached real-time target pose: thread-safe, no I/O. The robot's
        # monitoring runs at ~60 Hz by default, so sampling faster only
        # duplicates values.
        hub.add_source(
            "robot_xy",
            lambda: robot.api.GetRtTargetCartPos(include_timestamp=True).data[:2],
            rate_hz=50,
        )

    def read_power():
        watts = meter.api.read_power()
        # Overrange reads +inf and an invalid reading NaN; record nothing
        # rather than a value the page and the analysis cannot use.
        return watts if math.isfinite(watts) else None

    hub.add_source("power", read_power, rate_hz=POWER_RATE_HZ)

    # Fixed plot axes and the pass/fail criterion, for the page.
    hub.publish("scan_config", {
        "x": SCAN_X_MM,
        "y": SCAN_Y_MM,
        "tolerance": UNIFORMITY_TOLERANCE,
    })
    hub.on("cycle_end", analyse_cycle)


def analyse_cycle(hub, event):
    """Grid the power measured over the cycle that just ended, then judge its uniformity."""
    start_t = event.get("start_t")
    if start_t is None:
        return
    power = hub.window("power", start_t, event["t"])
    xy = hub.window("robot_xy", start_t, event["t"])
    points = [(x, y, p) for p, (x, y) in hub.align(power, xy)]
    power_map = _grid(points)
    nodes = [z for row in power_map["z"] for z in row if z is not None]
    if not nodes:
        return

    # Mean over grid nodes, not samples: every spot of the area weighs the same
    # however long the robot lingered on it.
    mean = sum(nodes) / len(nodes)
    # No light (or only noise around zero after a dark zero): nothing to be
    # uniform relative to, so the map fails without a deviation figure.
    deviation = max(abs(z - mean) for z in nodes) / mean if mean > 0 else None
    hub.publish("power_map", {"cycle": event["data"].get("cycle"), "mean": _sig(mean), **power_map})
    hub.publish("power_map_deviation", None if deviation is None else round(deviation, 4))
    hub.publish("power_map_ok", deviation is not None and deviation <= UNIFORMITY_TOLERANCE)


def _sig(value, digits=4):
    """Round to significant digits: power spans pW to W, so fixed decimals would zero it."""
    return float(f"{value:.{digits}g}")


def _grid(points):
    """Average readings onto the nearest grid node.

    Gaps along a row (sparse sampling on a fast scan) are interpolated between
    measured nodes; rows or ends never reached stay ``None``.
    """
    nx, ny = GRID_NODES
    (x0, x1), (y0, y1) = SCAN_X_MM, SCAN_Y_MM
    sums = [[0.0] * nx for _ in range(ny)]
    counts = [[0] * nx for _ in range(ny)]
    for x, y, v in points:
        i = min(nx - 1, max(0, round((x - x0) / (x1 - x0) * (nx - 1))))
        j = min(ny - 1, max(0, round((y - y0) / (y1 - y0) * (ny - 1))))
        sums[j][i] += v
        counts[j][i] += 1
    z = [[sums[j][i] / counts[j][i] if counts[j][i] else None for i in range(nx)] for j in range(ny)]
    for row in z:
        known = [i for i, v in enumerate(row) if v is not None]
        for a, b in zip(known, known[1:]):
            for i in range(a + 1, b):
                row[i] = row[a] + (row[b] - row[a]) * (i - a) / (b - a)
    return {
        "x": [round(x0 + i * (x1 - x0) / (nx - 1), 2) for i in range(nx)],
        "y": [round(y0 + j * (y1 - y0) / (ny - 1), 2) for j in range(ny)],
        "z": [[None if v is None else _sig(v) for v in row] for row in z],
    }
