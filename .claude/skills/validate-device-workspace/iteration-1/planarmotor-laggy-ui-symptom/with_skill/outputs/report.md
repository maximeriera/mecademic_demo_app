The lag comes from the PlanarMotor driver. Every time anything asks the planar motor whether it is faulted or ready, the driver sends a live status query to the PMC through pmclib instead of returning a stored value.

- **The fault monitor** asks `faulted` then `ready` every 200 ms, so that's 2 PMC round trips per cycle, about 10 a second.
- **Each open browser page** adds 2 more per second through `/api/info` and the SSE device event.
- **The monitor checks devices one at a time on a single thread.** The other devices' fault checks wait behind those PMC replies, and so does the device health the UI shows.

A test with a fake PMC answering in 0.3 s shows one monitor cycle stuck for 0.6 s on this device alone. I haven't measured how slow the real PMC is. I changed no driver code, and the only new file is the test file.

## PlanarMotor (`planarmotor`): validation

Files: devices/PlanarMotor.py, devices/api/PlanarMotorApi.py, registry entry (devices/registry.py:456), tests/test_planar_motor.py (new)
Verdict: 2 Critical, 5 Major, 6 Minor. With this driver on the cell:
- Fault detection for every device runs at the PMC's pace.
- ABORT doesn't stop the movers.
- After an ABORT, the next task fails on its first wait.

### Findings
| # | Sev | Check | Finding | Evidence | Proven by |
|---|---|---|---|---|---|
| 1 | Critical | A1 | The UI lag. `faulted` reads `api.faulted`, which calls `get_pmc_status()` and then pmclib's `GetPMCStatus` (a DLL round trip to the PMC), on every read. `ready` reads `faulted` again, so it is 2 queries per monitor cycle (`ApplicationController.py:1047,1064`, every 200 ms) and 2 per `/api/info` or SSE refresh (`app.py:505-507`). A slow reply blocks the monitor thread before it reaches the other devices. | devices/PlanarMotor.py:26-34, devices/api/PlanarMotorApi.py:62-70, 155-157 | test_status_properties_never_touch_the_device (xfail: 40 PMC calls for 20 status reads), test_status_reads_dont_wait_on_a_slow_pmc (xfail: 0.6 s for one faulted+ready read) |
| 2 | Critical | C5 | `abort()` only sets a Python flag. It never calls `stop_motion` (it exists in `pmclib.xbot_commands`), so the movers finish their trajectories after ABORT or after another device faults. The task's `wait_move_done` does unblock within about 0.1 s. | devices/PlanarMotor.py:74-78, devices/api/PlanarMotorApi.py:369-371 | test_abort_stops_the_movers (xfail), test_abort_unblocks_a_move_wait (pass) |
| 3 | Major | C5 | The abort flag stays set: only `initialize()` and `clear_fault()` clear it. After an operator ABORT the cell returns to READY, and the next task's first `wait_move_done` raises `InterruptedError` on an idle mover. That task fails, the cell faults, and the operator has to click Clear faults. | devices/api/PlanarMotorApi.py:72-77, 258; cleared only at PlanarMotor.py:45, PlanarMotorApi.py:350 | test_a_new_wait_after_an_abort_works (xfail) |
| 4 | Major | C1 | A failed `initialize()` can be reported as healthy. The driver sets `self._faulted = True`, but `faulted` overwrites it from the PMC state. A PMC that is INACTIVE (not ERROR) after a failed activation reports connected, ready and not faulted. | devices/PlanarMotor.py:33, 53-56; PlanarMotorApi.py:100 | test_failed_activation_is_reported_as_faulted (xfail) |
| 5 | Major | B4 | `clear_fault()` never reopens the PMC session. It looks for `sys.reset_fault`, `clear_fault` and `reset_error`, and none of them exist in pmclib's `system_commands`, so it only re-reads the status. It also runs when the device isn't faulted. It never moves anything (good). | devices/PlanarMotor.py:68-72, devices/api/PlanarMotorApi.py:348-367 | test_clear_fault_reopens_the_session (xfail), test_clear_fault_is_a_no_op_when_healthy (xfail), test_clear_fault_never_moves_hardware (pass) |
| 6 | Major | C2 | `shutdown()` never stops motion. After an ABORT, `deactivate_bots()` raises on the stuck abort flag, `shutdown()` logs it and disconnects with the movers still levitating. `disconnect_from_pmc()` is unguarded and can raise out of `shutdown()`. | devices/api/PlanarMotorApi.py:146, 373-383 | test_shutdown_after_abort_still_lands_movers (xfail) |
| 7 | Major | H1 | There was no test for the driver. | n/a | tests/test_planar_motor.py now added |
| 8 | Minor | B3 | The fault is a bool with no reason text, so the operator sees "faulted" with no reason. | devices/PlanarMotor.py:13, PlanarMotorApi.py:51 | code reading |
| 9 | Minor | A1/rule 3 | `faulted` is `True` whenever the device is just not connected (before INITIALIZE, after SHUTDOWN), so being disconnected and being faulted look the same. | devices/PlanarMotor.py:33 | code reading |
| 10 | Minor | E1 | The API module imports app code (`core.LogSetup`). | devices/api/PlanarMotorApi.py:8 | wiring check |
| 11 | Minor | E2/E4 | No lock around pmclib calls: today the monitor thread and the task thread call the DLL concurrently. pmclib raises one `PmcError` for both link failures and refused commands. | devices/api/PlanarMotorApi.py:36-52 | wiring check, code reading |
| 12 | Minor | G1 | Log messages without the `[{device_id}]` prefix. | devices/PlanarMotor.py:55, 63; PlanarMotorApi.py:362, 379 | wiring check |
| 13 | Minor | C4 | `supports_reconnect` is False, which is honest today. Once 4 and 6 are fixed it could be enabled. | devices/PlanarMotor.py (inherits False) | code reading |

### Passed
A2 (getters catch errors and never raise), A3, B1 (a dead link does latch `faulted`, but only through the status polling in finding 1), B2 (a refused command doesn't fault), C3, F1, F2, F3. D and E3 are N/A: there's no telemetry or I/O, and pmclib owns the socket.

### Not verified
- **No hardware:** all tests run against a fake pmclib. The `sys` and `bot` aliases are monkeypatched, and the real `PMCLIB.dll` is loaded but never called. I couldn't check:
  - How long a real `GetPMCStatus` takes, which is how much lag finding 1 actually causes on your cell.
  - Whether `PMCLIB.dll` is thread-safe, and whether it releases the Python GIL during a call. If it doesn't, every Flask thread stalls too.
  - What pmclib does when the link drops: does it raise, or does `get_pmc_status()` return `PMC_UNKNOWN`? The current `faulted` only treats `PMC_ERROR` as a fault, so UNKNOWN or ERRORHANDLING would show as healthy.
  - Whether `stop_motion(0)` (all movers) is the right ABORT command for your setup.
- **To measure the polling cost at the machine.** This only connects (no mastership, no levitation) and times 50 `faulted` reads:
  ```
  .venv/Scripts/python.exe -c "import time; from devices.PlanarMotor import PlanarMotor; d=PlanarMotor('192.168.10.200', name='pm_probe'); assert d.api.connect(); t=time.perf_counter(); [d.faulted for _ in range(50)]; print((time.perf_counter()-t)/50*1000, 'ms per faulted read'); d.api.sys.disconnect_from_pmc()"
  ```
  Anything above about 1 ms per read confirms finding 1 on the real controller. Don't run `initialize()` without checking: it gains mastership and levitates the movers.

### Tests
- **tests/test_planar_motor.py:** 10 passed, 8 xfailed. There is one strict xfail per open finding, and each one fails for the documented reason.
- **Full suite:** 143 passed, 8 xfailed. Before the new file it was 133 passed, so nothing new fails.

## Fixes I can apply

**Critical**
- **A1 (the lag):** `faulted`, `ready` and `connected` would return plain attributes (`_fault: str | None`, `_open`) and never call the PMC. You need to choose how a PMC that goes into `PMC_ERROR` on its own still gets detected:
  - **(a)** A small driver thread polls `get_pmc_status()` about twice a second and caches it. Detection is unchanged and the monitor never waits.
  - **(b)** No polling. Faults latch only when a task call fails (wrap pmclib's `sys`/`bot` so a failed call sets `_fault`). This is simpler, but a PMC error while idle shows up only on the next command.
- **C5:** `abort()` would call `bot.stop_motion(0)` inside a try/except, so it never raises and works with the link down, then interrupt the waits.

**Major**
- **C5 (stuck flag):** replace the abort flag with an abort counter that each wait reads when it starts, so an ABORT stops only the waits in progress and not the next task.
- **C1:** a failed `initialize()` keeps `_fault` set with the reason, and `faulted` reports it.
- **B4:** `clear_fault()` would return early when healthy. Otherwise it disconnects and reconnects the PMC session, with no activation or motion. A PMC stuck in `PMC_ERROR` would still need INITIALIZE, because recovering it means levitating the movers.
- **C2:** `shutdown()` would call `stop_motion` first, land the movers whether or not ABORT was pressed, and guard `disconnect_from_pmc()`.

**Minor**
- Keep the fault reason as text (B3).
- Add the `[device_id]` prefix to log messages (G1).
- Use a plain `logging` logger in the API module (E1).
- Put an `RLock` around pmclib calls (E2).
- Stop reporting "not connected" as "faulted".
- Set `supports_reconnect = True` once C1 and C2 are fixed.

All of these keep the public surface: the `PlanarMotorApi` methods, its constructor and `PlanarMotor.isFaulted()`. No workspace in `app_logic/` or `examples/` uses `planarmotor` today, so nothing in task code depends on the current behavior.

**Which of these fixes should I apply? For A1, do you want option (a), a background status poller, or (b), faults only from failed calls?**