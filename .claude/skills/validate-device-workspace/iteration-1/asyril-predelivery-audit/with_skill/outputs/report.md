## AsyrilEyePlus (`asyril`): validation

Files: devices/Asyril.py, devices/api/AsyrilAPI.py, devices/registry.py:442-450, tests/test_asyril_eye_plus.py (new), tests/fake_instrument.py (new, the shared fake from add-device's testing.md)

Verdict: 2 Critical, 4 Major, 7 Minor. I wouldn't ship the demo as it is. STOP/ABORT doesn't interrupt a `get_part` that is waiting for parts, and a pulled cable or a rebooted Eye+ still shows as healthy while `get_part` returns junk dicts. The 12 tests marked xfail each fail today because of one finding below.

### Findings
| # | Sev | Check | Finding | Evidence | Proven by |
|---|---|---|---|---|---|
| 1 | Critical | C5 | `abort()` only logs. A `get_part` waiting on an empty platform keeps the task blocked through STOP/ABORT for up to the Eye+ part timeout (30 s), or the 35 s socket timeout. | devices/Asyril.py:65-66, devices/api/AsyrilAPI.py:21,253-257 | test_abort_unblocks_a_waiting_get_part (xfail) |
| 2 | Critical | B1 | A dropped link never faults. On a closed socket `recv()` returns `b""`, and `__receive_raw__` returns `""` with no error, so `get_part()` returns `{'resp': {''}}`. `connected` stays True because only the unused API `connected` property ever clears `_connected`. A `ConnectionResetError` on send isn't latched either. | devices/api/AsyrilAPI.py:596-623, devices/Asyril.py:39-42 | test_link_drop_latches_fault (xfail) |
| 3 | Major | B4 | `clear_fault()` only sets `api._faulted = False`. It doesn't reopen the session, and it doesn't return early when the device isn't faulted. | devices/Asyril.py:62-63 | test_clear_fault_reopens_the_session_without_vibrating (xfail) |
| 4 | Major | E3 | After a socket timeout the socket stays open, so the late reply gets read as the answer to the next command. After a timeout fault and a Clear faults, the old `get_part` pose (`200 x=… y=…`) can come back as the reply to the next command. The test fixture's own teardown showed this: `stop production` received the stale `501` meant for the earlier `get_part`. | devices/api/AsyrilAPI.py:615-622 | test_late_reply_after_timeout_is_not_read_as_the_next_answer (xfail) |
| 5 | Major | C1 | Three problems with a failed `initialize()`. (a) It sets `self._faulted`, but the `faulted` property overwrites that from `api._faulted`, so a failed init never shows as faulted. (b) If the connect succeeds and a later step fails (for example the state query gets refused with 409), `initialize()` raises but the device still reports connected and ready. (c) It never closes the previous socket, so each re-init leaks a session, and the Eye+ has a connection limit (error code 411). | devices/Asyril.py:35-37,44-52, devices/api/AsyrilAPI.py:156-175 | test_initialize_against_dead_port_faults, test_failed_initialize_after_connect_is_not_reported_ready, test_reinitialize_closes_the_previous_session (all xfail) |
| 6 | Major | B2 | Refused commands don't raise. `initialize()` ignores the reply to `start production <recipe>`, so a wrong recipe (407) still gives a ready feeder that isn't in production. `get_part()` on a 4xx/5xx reply returns `{'resp': {'403\n'}}`. That's a Python set literal: it isn't the status code, and it isn't JSON-serializable. The calibration helpers raise `RuntimeError` instead, so the API is inconsistent. | devices/Asyril.py:47-48, devices/api/AsyrilAPI.py:259-263 | test_initialize_with_refused_recipe_raises, test_refused_command_raises_but_does_not_fault (xfail) |
| 7 | Minor | C2 | If `stop production` fails, `shutdown()` skips `disconnect()`, so the device keeps reporting connected and the socket leaks. It never raises (pass). | devices/Asyril.py:54-60 | test_shutdown_closes_the_session_even_if_stop_fails (xfail) |
| 8 | Minor | F1 | The registry gives `recipe` a default of `0`, but the constructor requires it. A config without `recipe` validates and runs `start production 0`, and per #6 a refusal there goes unnoticed. | devices/registry.py:446, devices/Asyril.py:8 | test_registry_defaults_match_the_constructor (xfail) |
| 9 | Minor | G1 | `print()` in `shutdown()`. `initialize()` logs an error without the `[device_id]` prefix. None of the API module's log lines carry the prefix. | devices/Asyril.py:51,60 | test_shutdown_does_not_print (xfail), code reading |
| 10 | Minor | B3 | The fault is a bool. No reason text is kept for the UI or the logs. | devices/api/AsyrilAPI.py:123 | code reading |
| 11 | Minor | E2 | No lock in the API. `get_part_async()` followed by any other call (for example `can_take_image(True)`, the natural next step) lets the background `recv` take the other call's reply. A UI Disconnect during a `get_part` interleaves the same way. Both are documented as caveats in the code, not prevented. | devices/api/AsyrilAPI.py:265-312 | code reading |
| 12 | Minor | E4 | There's no command-error exception class. `__handle_response__` is never called, and it would crash anyway: `EyePlusErrorCode(int("400 ..."))` raises `ValueError`. | devices/api/AsyrilAPI.py:625-635 | code reading |
| 13 | Minor | A3 | `info` holds only ip/port/recipe. No model, serial or firmware is read at connect. | devices/Asyril.py:18-24 | code reading |

Other notes (Minor, no ID):
- `recv(4096)` has no line framing. `reset_state` assumes a single `\n` terminator (`response[4:-1]`) and its error message is a copy-paste ("Failed to take calibration image") (AsyrilAPI.py:551,559).
- `initialize()` hard-codes a 30 s part timeout against a 35 s socket timeout. If a task calls `set_part_timeout(60)`, the socket times out first and an empty feeder faults the cell.
- The API's `connected` property peeks the socket, and it raises `AttributeError` before the first connect (AsyrilAPI.py:132-154). The driver doesn't use it, so A1 still passes.

### Passed
A1 (status reads touch no socket: no send/recv, MSG_PEEK included, and they don't wait behind a blocked `get_part`), A2, A3 (serializable), C3, C4 (happy path), E1, F2, F3. D1 is N/A (no telemetry or I/O). H1 was a FAIL (no test) and is covered now.

### Not verified
- **No feeder reachable from this machine**, so there was no live smoke test. The fake follows what AsyrilAPI.py assumes (`\n` terminator, `200 ready`, `200 x=… y=… rz=…`), not the Eye+ manual.
- Whether `stop production` sent from a second connection interrupts a pending `get_part` with 510 (GET_PART_INTERRUPTED). The C5 fix depends on it.
- Whether production state survives a TCP reconnect, which decides whether `clear_fault()` must restart production. Also how the real link drop looks (RST or FIN).
- The customer's demo workspace isn't in this repo, so I couldn't check how its task code uses `get_part()`'s return value. app_logic/prod.py:67 ignores it.
- Careful at the machine: `python -m devices.api.AsyrilAPI` and `python devices/Asyril.py` aren't read-only. They hard-code 192.168.0.50, change the feeder state, and the second one starts production and calls `get_part`. For a read-only check:
  `python -c "import logging; from devices.api.AsyrilAPI import AsyrilEyePlusApi as A; a=A(logging.getLogger('x'),'<ip>',0); a.connect(); a._AsyrilEyePlusApi__send_raw__('get_parameter state'); print(a._AsyrilEyePlusApi__receive_raw__()); a.disconnect()"`

### Tests
tests/test_asyril_eye_plus.py: 15 passed, 12 xfailed (one or more per open finding), stable over 3 runs. Full suite: 148 passed, 12 xfailed. The baseline before my changes was 133 passed. Nothing in driver code was changed.

### Proposed fixes (none applied)
**Critical**
- C5: `abort()` bumps an abort counter and unblocks the pending `recv` without taking any lock: `socket.shutdown(SHUT_RDWR)` on the session, optionally plus `stop production` over a short second connection (to check at the machine). `get_part` then raises `AsyrilAborted` instead of returning a dict.
- B1: `__receive_raw__` treats `b""` as `ConnectionError`. Any `OSError` on send or recv closes the socket and latches a fault with its reason through an `on_comm_error` callback (the `_CellAsyril…` subclass pattern). The driver's `connected` becomes `_open and _fault is None`.

**Major**
- B4 + E3: the API closes the session on a timeout or socket error. `clear_fault()` returns early when not faulted, and otherwise closes and reconnects (re-entering production if the hardware check says it's needed), with no vibration.
- C1: `initialize()` closes first. Any failure sets the fault reason, closes the session and re-raises. It sets `_open` before clearing the fault, and checks the `start production` and `set_parameter timeout` replies.
- B2: add `AsyrilCommandError(code, command)` and raise it on non-2xx replies. This changes what `get_part()` returns on errors (an exception instead of a dict), so it needs your call on the customer's task code. One option: raise on 4xx, but keep returning `{'resp': 501}` for "no part found" so existing retry loops keep working.

**Minor**: C2 (`disconnect()` in a `finally`), F1 (make `recipe` required in the registry), G1/B3 (logger with prefix, fault reason text), E2 (one `RLock` across exchanges), E4 (fix or remove `__handle_response__`), A3 (read the identity at connect, if the Eye+ has a command for it).

Which fixes should I apply? With delivery next week, I'd do Critical + Major in this worktree, each one turning its xfail test green. The B2 change to `get_part()`'s return contract needs your answer first: does the customer workspace check `resp`, or does it just read `x`/`y`/`rz`?
