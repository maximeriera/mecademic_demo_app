"""Custom view — data sources and analysis behind this workspace's /view/ page.

Optional. When this package exists next to ``config.yaml``, the controller
calls :func:`setup` at every Initialize, then starts the hub: one sampler thread
per source, plus one worker running the analysis callbacks. Delete the folder
and the standard app runs exactly as before.

This example inspects a part's surface: the robot rasters the scan area while a
height sensor reads the part under it. Two independent sources (robot X/Y,
height) share the hub's common clock; the page pairs them live into a 3D point
cloud, and :func:`analyse_cycle` grids each finished cycle into a surface and
judges it. The page (``index.html`` / ``view.js`` / ``plot3d.js`` here) only
reads what the hub holds.

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

from devices.SimulatedDevice import SimulatedDevice

from .sim import SimulatedScan

#: Scan area in robot coordinates, and the expected part height.
SCAN_X_MM = (150.0, 250.0)
SCAN_Y_MM = (-40.0, 40.0)
NOMINAL_HEIGHT_MM = 12.0
#: Largest deviation from nominal, anywhere on the gridded surface, that passes.
TOLERANCE_MM = 0.6
#: Surface grid nodes along X and Y (Y matches the 9 scan lines of the sim).
GRID_NODES = (21, 9)


def setup(hub, devices):
    """Declare the data sources and analysis for this workspace."""
    robot = devices["my_meca_robot"]

    if isinstance(robot, SimulatedDevice):
        # A SimulatedDevice's api returns None (and logs every call), so it
        # cannot feed a source: synthesize the scan instead.
        scan = SimulatedScan(hub, SCAN_X_MM, SCAN_Y_MM, NOMINAL_HEIGHT_MM)
        hub.add_source("robot_xy", scan.robot_xy, rate_hz=50)
        hub.add_source("sensor", scan.sensor, rate_hz=50)
    else:
        # Cached real-time target pose: thread-safe, no I/O. The robot's
        # monitoring runs at ~60 Hz by default, so sampling faster only
        # duplicates values.
        hub.add_source(
            "robot_xy",
            lambda: robot.api.GetRtTargetCartPos(include_timestamp=True).data[:2],
            rate_hz=50,
        )
        # Plug in the real height sensor here, owned by this source, e.g.:
        #   sensor = devices["height_sensor"]
        #   hub.add_source("sensor", lambda: sensor.api.get_measurements(0)[0].value, rate_hz=50)

    # Fixed plot axes and the pass/fail criterion, for the page.
    hub.publish("scan_config", {
        "x": SCAN_X_MM,
        "y": SCAN_Y_MM,
        "nominal": NOMINAL_HEIGHT_MM,
        "tolerance": TOLERANCE_MM,
    })
    hub.on("cycle_end", analyse_cycle)


def analyse_cycle(hub, event):
    """Grid the heights measured over the cycle that just ended, then judge the surface."""
    start_t = event.get("start_t")
    if start_t is None:
        return
    height = hub.window("sensor", start_t, event["t"])
    xy = hub.window("robot_xy", start_t, event["t"])
    points = [(x, y, h) for h, (x, y) in hub.align(height, xy)]
    surface = _grid(points)
    nodes = [z for row in surface["z"] for z in row if z is not None]
    if not nodes:
        return

    deviation = max(abs(z - NOMINAL_HEIGHT_MM) for z in nodes)
    hub.publish("surface", {"cycle": event["data"].get("cycle"), **surface})
    hub.publish("surface_deviation", round(deviation, 3))
    hub.publish("surface_ok", deviation <= TOLERANCE_MM)


def _grid(points):
    """Average heights onto the nearest grid node.

    Gaps along a row (sparse sampling on a fast scan) are interpolated between
    measured nodes; rows or ends never reached stay ``None``.
    """
    nx, ny = GRID_NODES
    (x0, x1), (y0, y1) = SCAN_X_MM, SCAN_Y_MM
    sums = [[0.0] * nx for _ in range(ny)]
    counts = [[0] * nx for _ in range(ny)]
    for x, y, h in points:
        i = min(nx - 1, max(0, round((x - x0) / (x1 - x0) * (nx - 1))))
        j = min(ny - 1, max(0, round((y - y0) / (y1 - y0) * (ny - 1))))
        sums[j][i] += h
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
        "z": [[None if v is None else round(v, 3) for v in row] for row in z],
    }
