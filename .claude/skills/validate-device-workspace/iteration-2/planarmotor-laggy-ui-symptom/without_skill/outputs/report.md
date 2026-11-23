I found the cause in the PlanarMotor driver and fixed it. I could only test it against a fake PMC, not your hardware.

## Cause

`PlanarMotorApi.faulted` (`devices/api/PlanarMotorApi.py`) called `get_pmc_status()` every time anything read it. That is a blocking round trip to the PMC through pmclib. `PlanarMotor.ready` reads `faulted` again, so each status check cost two round trips. Two threads were paying for this:

- **Fault monitor** (`ApplicationController._monitor_devices_status`). It checks devices one after another, about 5 times a second, and reads `faulted` then `ready` on each. The PMC reads ran before the other devices were checked, so a fault on a robot, feeder or camera had to wait behind them.
- **LivePublisher thread** (`app.py`, the `devices` source at 1 Hz). It calls `connected`, `ready` and `faulted` on every device. The same thread also feeds the 10 Hz `status` stream, so the whole UI froze while it waited on the PMC. That is the lag you saw.

If the PMC answers slowly or stops answering, both threads stall for as long as pmclib's call takes.

## Measured with a simulated slow PMC (300 ms per status call)

I ran the real controller with simulated devices plus a PlanarMotor on a fake PMC, then faulted the simulated camera:

| | before | after |
|---|---|---|
| Camera fault → controller FAULTED (median / max) | 406 / 787 ms | 88 / 187 ms |
| `devices` SSE producer per tick (stalls the 10 Hz status stream) | 601 ms | ~0 ms |

## Fix (`devices/api/PlanarMotorApi.py`)

- A background health thread reads `get_pmc_status()` every 0.2 s (`HEALTH_POLL_S`) and caches the result. `faulted`, `ready` and `connected` now only read that cache and never wait on the PMC.
- **If the PMC stops answering:** after `HEALTH_STALE_S = 2.0` s with no successful status read, `faulted` turns True. A hung controller still stops the cell, even though nothing waits on it any more.
- The meaning of "faulted" hasn't changed: still `PMC_ERROR`, or an exception from the status call. Planar-motor faults are still picked up within about 0.2 s, the same rate as the monitor.
- `connect()` reads the status once to fill the cache, then starts the thread. `shutdown()` stops the thread before deactivating the movers and disconnecting. `clear_fault()` does one fresh read, so Clear faults gets an up-to-date answer.
- I didn't change `PlanarMotor.py` or any other file.

## Tests

- New file: `tests/test_planar_motor.py`, 7 tests, no hardware. It uses a fake pmclib `system_commands`/`xbot_commands` layer. If the real pmclib/.NET DLL can't load, as on Linux or Docker, it also stands in for the pmclib package.
- What the tests cover:
  - status reads return immediately while the PMC is hung
  - 100 reads don't trigger 100+ round trips
  - `PMC_ERROR` is picked up and Clear faults clears it
  - an exception from the status call faults the device
  - a hung PMC faults after the staleness limit and recovers when it answers again
  - `shutdown()` stops polling
  - no PMC calls are made while disconnected
- Against the old driver, 4 of the 7 failed. With the fix, all 7 pass, both with the real pmclib and with it blocked.
- Full suite: **140 passed** (133 before, plus the 7 new ones).

## Not verified without the PMC

- I haven't measured the real round-trip time of `get_pmc_status()`, or how long it blocks when the cable is pulled.
- The health thread and the task thread can now call pmclib at the same time. That already happened before the fix (the monitor thread and the task thread both called it), and there are now fewer threads doing so, but I haven't checked that pmclib/PMCLIB.dll is safe to call from several threads at once.
- **Worth checking on the cell:**
  - the UI stays smooth with the planar motor connected
  - Clear faults recovers after a planar motor error
  - unplugging the PMC faults the cell within about 2 s

## Other problems I found but didn't change

1. **A pulled cable may never fault the cell.** pmclib has a `PMC_UNKNOWN` (-1) status meaning "unable to determine PMC status", and only `PMC_ERROR` counts as faulted. If pmclib returns `PMC_UNKNOWN` rather than raising when the link drops, the planar motor stays "healthy". `connected` never goes False either, because `is_connected` is only set in `connect()`/`shutdown()`.
2. **Clear faults doesn't reset anything on the PMC.** `clear_fault()` tries `sys.reset_fault` / `clear_fault` / `reset_error`, but none of those exist in the bundled pmclib 117.9.1. It only clears the local flag and re-reads the status, so a PMC that is really in `PMC_ERROR` stays faulted.

Nothing is committed. The changes are in my worktree (`devices/api/PlanarMotorApi.py` and the new `tests/test_planar_motor.py`).

**Question:** should I also treat `PMC_UNKNOWN` as a fault and mark the link down when status reads fail (item 1)? It's the safer choice, but I'd want you to check on the cell that the PMC never reports `PMC_UNKNOWN` during normal running.
