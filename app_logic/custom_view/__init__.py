"""Custom view — data sources and analysis behind this workspace's /view/ page.

Optional. When this package exists next to ``config.yaml``, the controller
calls :func:`setup` at every Initialize, then starts the hub: one sampler thread
per source, plus one worker running the analysis callbacks. Delete the folder
and the standard app runs exactly as before.

This example measures a part's height profile *as a function of robot X*:
two independent sources (robot X, height sensor) on the hub's common clock,
joined at the end of every cycle by :func:`analyse_cycle`. The page
(``index.html`` / ``view.js`` here) only reads what the hub holds.

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

from .sim import NOMINAL_HEIGHT_MM, SimulatedScan

#: Largest height deviation from nominal, anywhere on the profile, that passes.
TOLERANCE_MM = 0.6


def setup(hub, devices):
    """Declare the data sources and analysis for this workspace."""
    robot = devices["my_meca_robot"]

    if isinstance(robot, SimulatedDevice):
        # A SimulatedDevice's api returns None (and logs every call), so it
        # cannot feed a source: synthesize the scan instead.
        scan = SimulatedScan(hub)
        hub.add_source("robot_x", scan.robot_x, rate_hz=50)
        hub.add_source("sensor", scan.sensor, rate_hz=50)
    else:
        # Cached real-time target pose: thread-safe, no I/O. The robot's
        # monitoring runs at ~60 Hz by default, so sampling faster only
        # duplicates values.
        hub.add_source(
            "robot_x",
            lambda: robot.api.GetRtTargetCartPos(include_timestamp=True).data[0],
            rate_hz=50,
        )
        # Plug in the real height sensor here, owned by this source, e.g.:
        #   sensor = devices["height_sensor"]
        #   hub.add_source("sensor", lambda: sensor.api.get_measurements(0)[0].value, rate_hz=50)

    hub.on("cycle_end", analyse_cycle)


def analyse_cycle(hub, event):
    """Join height against robot X over the cycle that just ended, then judge it."""
    start_t = event.get("start_t")
    if start_t is None:
        return
    x = hub.window("robot_x", start_t, event["t"])
    height = hub.window("sensor", start_t, event["t"])
    profile = hub.align(x, height)
    if not profile:
        return

    deviation = max(abs(h - NOMINAL_HEIGHT_MM) for _, h in profile)
    hub.publish("profile", {
        "cycle": event["data"].get("cycle"),
        "nominal": NOMINAL_HEIGHT_MM,
        "tolerance": TOLERANCE_MM,
        "points": [[round(px, 2), round(h, 3)] for px, h in profile],
    })
    hub.publish("profile_deviation", round(deviation, 3))
    hub.publish("profile_ok", deviation <= TOLERANCE_MM)
