I found and fixed six bugs in the ioLogik E1212 driver, and the task API your workspaces use still works the same: `io.api.write_do` and `io.api.read_all_di` keep their names, signatures and return values (8 bools). The full suite passes (155 tests: the 133 that were there before plus 22 new ones). There is no hardware reachable from here, so I haven't tested against a real E1212. Instead I ran the driver with pymodbus' real client against a local Modbus TCP server.

## What was broken

The line numbers below refer to the original files.

1. **A dead unit still showed as healthy.** When a cable is pulled, pymodbus raises an exception (or a raw socket error) instead of returning an error reply. Only error replies set the fault flag (`devices/api/IoLogikE1212Api.py:447-452`), so the device stayed "OK / Ready" and the socket stayed open.
2. **The whole UI could freeze for about 12 s.** The `faulted`, `connected` and `ready` checks took the same lock as the Modbus requests (`_SerializedClient.__getattr__` wraps `is_socket_open`, `:20-28` and `:147-154`). The health monitor checks every 200 ms and the status route uses them too, so both waited behind any request stuck on a dead unit. pymodbus' defaults make that wait up to 3 s × 4 attempts.
3. **Several commands ignored error replies from the unit:**
   - `start_di_counter`, `stop_di_counter`, `reset_di_counter`, `clear_di_counter_overflow`
   - `configure_do_pulse`, `start_do_pulse`, `stop_do_pulse`
   - `clear_watchdog_alarm`

   They reported success even when the unit refused the command (`:214-250`, `:341-364`, `:430-435`).
4. **"Clear fault" didn't check anything.** It just reset the flag (`devices/IoLogikE1212.py:94-96`), so a unit that was still unreachable came back as "OK".
5. **INITIALIZE succeeded on a unit that didn't answer.** It only checked that the TCP connection opened, and the identity read only logged a warning.
6. **Smaller issues:**
   - Calling the API before INITIALIZE or after a disconnect failed with `AttributeError: 'NoneType'` instead of a clear connection error.
   - A failed connect kept the dead client.
   - Channel checks accepted `1.5` and gave a `TypeError` for `"1"` instead of a clear `ValueError`.

## What I changed

- **`devices/api/IoLogikE1212Api.py`:**
  - Every request now goes through one `_call()` helper. Any connection error or error reply marks the device as faulted.
  - On a connection error it also closes the socket, so the next exchange starts clean, and re-raises the original exception.
  - Calling before connecting raises pymodbus' `ConnectionException`.
  - The connected/faulted checks no longer wait on the lock.
  - New `recover()`: it reconnects if needed, reads the outputs once to check the unit answers, and only then clears the fault.
  - New `timeout` and `retries` settings, passed to the client.
- **`devices/IoLogikE1212.py`:**
  - `initialize()` reads the outputs once before reporting ready, so a unit that doesn't answer fails INITIALIZE. An identity register the firmware refuses no longer marks the device as faulted.
  - `clear_fault()` uses `recover()`.
  - The constructor accepts `timeout` and `retries`.
- **`devices/registry.py` and `app_logic/config.yaml`:** new optional `timeout` (default 1.0 s) and `retries` (default 1) config keys, documented in the config file. A dead unit now blocks a task for about 2 s instead of about 12 s.
- **`tests/test_iologik.py` (new):** 22 tests that need no hardware and use a fake Modbus client:
  - the task API is unchanged
  - each bug above fails on the old code and passes now
  - the device reconnects after Disconnect, and the registry builds it
- **`tests/test_registry.py`:** updated the expected iologik settings to include `timeout` and `retries`.

**Effect on task code:**
- Error types are compatible: error replies still raise `ModbusException`, and the new connection errors are subclasses of it. So existing `except ModbusException` blocks still catch them, and a raw socket `OSError` is still re-raised unchanged.
- The one change you'll notice is intended: a call that fails now marks the device faulted. The cell then stops until **Clear faults**, as it does for the other drivers.

## Checked against a real pymodbus server

- **Live unit:** `write_do`, `read_all_di`, pulses, counters and the live panel all work.
- **Server stopped:** the device faults immediately, and Clear fault refuses and stays faulted.
- **Silent unit** (accepts the connection but never replies, like a pulled cable): INITIALIZE fails in 1.0 s, a running `write_do` fails in 1.0 s and faults the device, and the health check returns in 0.000 s while the request hangs.

These runs used timeout 0.5 s and retries 1; the defaults (1.0 s, 1 retry) give about 2 s.

## Questions for you

1. **Only 8 of the 16 inputs are read.** The E1212 has 16 digital inputs, but `DI_COUNT = 8` (`IoLogikE1212Api.py:56`), so `read_all_di()` returns 8 values and `read_di(8..15)` raises. This has been the case since the driver was added, and the README says "DI 0–7". I left it alone because returning 16 values would break task code that unpacks the list or checks its length. I only corrected the docstrings that said 16. Do you need inputs 8–15? If so, I can add them without breaking anything: either a separate `read_all_di16()`, or raise the limit for `read_di()` only.
2. **ABORT doesn't touch the unit.** It's still a no-op, so a pulse train started with `start_do_pulse` keeps running after ABORT or a cell fault. Should ABORT stop pulse outputs? I didn't make outputs change on abort, because dropping a DO could, for example, release a vacuum.
3. **The register map is unchecked.** I couldn't check the addresses or the firmware-version and MAC decoding against hardware. A quick read-only check on a real unit is worth doing: model name, firmware version and `read_all_di`.
4. **Defaults are shorter.** The new defaults (1.0 s timeout, 1 retry) are shorter than pymodbus' (3 s, 3 retries). If a unit is on a slow or congested network, set `timeout` higher in its config.

Nothing is committed; the changes are in the worktree.
