## IoLogikE1212 (`iologik`): validation and fixes

Files: devices/IoLogikE1212.py, devices/api/IoLogikE1212Api.py, registry entry (devices/registry.py:461), tests/test_iologik_e1212.py (new)

Verdict before the fixes: 2 Critical, 4 Major, 5 Minor. With a pulled cable the unit kept showing healthy. A busy unit could freeze fault detection for the whole cell for up to 12 s. A refused write faulted the cell, and Clear faults couldn't bring the unit back. All of these are fixed and each one has a test. The `.api` surface your task code uses has not changed.

### Findings (line numbers are from before the fix)
| # | Sev | Check | Finding | Evidence | Proven by | Status |
|---|---|---|---|---|---|---|
| 1 | Critical | A1 | `faulted`/`ready`/`connected` -> `api.is_connected` -> `_SerializedClient` wraps `is_socket_open()` in the session RLock. A request that gets no reply holds that lock for timeout 3 s x (retries 3 + 1) = 12 s, and the fault monitor and `/api/info` wait the whole time | devices/IoLogikE1212.py:66,70,74; devices/api/IoLogikE1212Api.py:20-28,147-154 | test_status_properties_dont_wait_for_a_task_exchange (xfail before the fix) | Fixed |
| 2 | Critical | B1 | Link errors never latch a fault. `faulted` only follows "socket open". With the cable pulled, pymodbus raises `ModbusIOException` and leaves the socket open, so the device stays healthy. If the peer closes the socket, the device faults until the next request, then pymodbus reconnects silently and the fault disappears | devices/IoLogikE1212.py:74; nothing in the API catches `ConnectionException`/`ModbusIOException` | test_no_reply_latches_fault_with_reason, test_link_drop_fault_stays_latched_until_cleared (xfail) | Fixed |
| 3 | Major | B2 | A refused request (Modbus exception response, e.g. illegal address) set `api._faulted = True`, so a usage error faulted the whole cell | devices/api/IoLogikE1212Api.py:447-452 | test_refused_command_is_not_a_fault (xfail) | Fixed |
| 4 | Major | B4 | `clear_fault()` only reset `api._faulted`. After a closed socket the device stayed faulted ("Clear faults doesn't recover"), and after a timeout it kept the stale socket | devices/IoLogikE1212.py:94-96 | test_clear_fault_after_socket_closed_recovers, test_clear_fault_reopens_the_session_without_writing (xfail) | Fixed |
| 5 | Major | C1 | `initialize()` turned every error on the identity read into a warning. A unit that accepts TCP on 502 but never answers was reported as initialized. A unit that refuses one identity register initialized "successfully" but came out faulted | devices/IoLogikE1212.py:80-89 | test_initialize_fails_when_the_unit_never_answers, test_unsupported_identity_register_does_not_fault (xfail) | Fixed |
| 6 | Major | H1 | No test exercised the driver | check_wiring.py | n/a | Fixed: 18 tests |
| 7 | Minor | B3 | The fault was a bare bool with no reason | devices/api/IoLogikE1212Api.py:117,451 | test_no_reply_latches_fault_with_reason | Fixed (`_fault` text, logged) |
| 8 | Minor | E3 | The session was not closed on a link error, so a late reply could be read as the answer to the next request | devices/api/IoLogikE1212Api.py:20-28 | test_api_closes_the_session_on_a_link_error (xfail) | Fixed |
| 9 | Minor | E4 | A refused request and a link error both raised a plain `ModbusException`, so the two couldn't be told apart | devices/api/IoLogikE1212Api.py:452 | code reading | Fixed (`IoLogikCommandError`) |
| 10 | Minor | A2 | `is_connected` read `self._client` twice, so a `disconnect()` from another thread in between could raise `AttributeError` in the monitor | devices/api/IoLogikE1212Api.py:150-151 | code reading | Fixed |
| 11 | Minor | G1 | A log message without the `[device_id]` prefix | devices/IoLogikE1212.py:96 | check_wiring.py | Fixed in the driver. The API's own messages are still unprefixed |

### What changed
- **devices/api/IoLogikE1212Api.py**: `_SerializedClient` catches link errors (`ConnectionException`, `ModbusIOException`, `OSError`). It closes the socket, reports the error through a callback, and re-raises the original exception. `is_socket_open()` no longer takes the lock. A refused request raises the new `IoLogikCommandError(ModbusException)` and no longer sets `_faulted`. There is a new optional `on_comm_error=` constructor argument and a `last_error` attribute. I also added `__all__`.
- **devices/IoLogikE1212.py**: now follows the driver pattern. `_fault: str | None` and `_open` hold the status, so `faulted`/`ready`/`connected` are plain attributes. `initialize()` closes then connects, and a link error during the identity read fails it, while a register the unit refuses only logs a warning. `clear_fault()` returns early when there is no fault, otherwise it closes and reopens the session and never writes outputs. `io_points()` checks `connected`.

**Your task code:** I couldn't find any workspace calling the driver on this machine (I searched app_logic/, examples/ and mecademic_demo_template/), so I kept the API surface identical. Every method name, signature and return value is the same: `read_all_di()` still returns 8 bools and `write_do(ch, value)` still writes coil `ch`. `test_task_api_write_do_and_read_all_di_unchanged` guards both. Three behaviors did change:
- A refused request raises `IoLogikCommandError`. It subclasses `ModbusException`, so `except ModbusException` still catches it. Link errors raise the same pymodbus exceptions as before.
- `device.faulted` is now False before `initialize()` and after `shutdown()` (it used to be True). This matches the other drivers. `connected`/`ready` are False as before.
- After a link error the API closes the socket. pymodbus reopens it on its own at the next request, but the device stays FAULTED until someone clears the fault.

### Passed
A3, C2, C3, C4 (disconnect/connect tested), C5 (I/O module: `abort()` only logs and outputs keep their state, tested), D1, E1, E2 (one lock per request), F1, F2, F3.

### Not verified
- No hardware is reachable from here. The pymodbus 3.12.1 behavior is faked from its source: `is_socket_open()` is `socket is not None`, `execute()` reconnects by itself, a timeout raises `ModbusIOException` with the socket left open, and a peer close raises `ConnectionException` and drops the socket. To check at the machine: initialize the cell, pull the ioLogik cable, and run a manual action that calls `io.api.read_all_di()`. After about 12 s the device should show FAULTED. Plug the cable back in, click Clear faults, and it should come back ready with no output changed.
- The identity registers (5000 model, 5027 IP, 5029 firmware) and their decoding haven't been checked against a unit.
- Don't run `python -m devices.api.IoLogikE1212Api` as a smoke test: it is not read-only (see item 4 below).

### Tests
tests/test_iologik_e1212.py: 18 tests. Before the fix: 9 passed and 9 xfailed (strict), one xfail per open finding. After the fix: 18 passed, and the xfail markers are removed. Full suite: 151 passed (133 before, plus 18 new). check_wiring.py now reports 0 FAIL and 1 WARN; the WARN is the "shutdown has no guard" heuristic, and the tests show shutdown is safe. Nothing is committed.

### Not fixed: these would change behavior your tasks rely on or need the unit
1. **DI count.** `DI_COUNT = 8`, but the class and `read_all_di` docstrings say 16 DI, and the E1212 has 16 DI and 8 DIO. So `read_all_di()` returns 8 values, `read_di(8..15)` raises `ValueError`, and the Live panel shows di0-7 only. Switching to 16 would change the length `read_all_di()` returns, and the register map needs checking on a unit first. One option is a `count` argument whose default stays 8.
2. **Request timeout.** pymodbus defaults to `timeout=3`, `retries=3`, so a dead unit blocks a task call for about 12 s, and `abort()` can't shorten that. Fix: add optional `timeout`/`retries` config keys (constructor plus registry Params; the defaults stay the same).
3. **Unchecked writes.** `start/stop/reset_di_counter`, `clear_di_counter_overflow`, `start/stop_do_pulse`, `configure_do_pulse` and `clear_watchdog_alarm` never check the response, so a request the unit refuses is silently ignored. Adding the check would make them raise where they pass today.
4. **The module's `__main__` writes to the unit.** `example_usage()` toggles DO0, pulses DO1 and resets counter 0 on a hardcoded IP. Fix: make it read-only by default and put the writes behind a `--write` flag.
5. **Minor:** `configure_do_pulse` writes 3 registers without holding the lock across all three, and the API's log lines lack the `[device_id]` prefix.

Do you want any of 1 to 5 applied? For 1, I'd need the DI register map from the E1212 manual, or a read on the unit.