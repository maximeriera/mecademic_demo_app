# Asyril Eye+ driver: pre-delivery audit

I wouldn't ship it as is. Most of the wiring is fine: it's registered, exported, and listed in config.yaml and the README. The driver itself breaks most of the fault and lifecycle rules in the add-device contract. Five of these show up on the demo cell as "the feeder says it's ready but it isn't" or "ABORT doesn't stop the cycle".

There were no Asyril tests at all. I wrote 27, run against a fake Eye+ on 127.0.0.1, so no feeder is needed:

- **14 pass.** They lock in what already works.
- **13 are `xfail(strict=True)`.** Each one reproduces a finding below, and its reason names the finding number. Once a finding is fixed, its test passes, and strict mode then fails the suite so you remember to remove the marker.

I ran the file 5 times and it was stable. The full suite gives **147 passed, 13 xfailed**; before, it was 133 passed. I did not change the driver.

## Findings: proven by a test

| # | Rule | Problem | Evidence | Test |
|---|---|---|---|---|
| 1 | 2: comm errors latch a fault | Only a socket *timeout* latches a fault. If the link is closed or reset (feeder rebooted, cable pulled), `recv` returns `""`. `get_part()` then returns `{'resp': {''}}`, with no exception and no fault, and the cell keeps running. | `devices/api/AsyrilAPI.py:602-623`, `:596-600` | `test_link_drop_latches_a_fault` |
| 2 | 6: clear_fault reopens the session | `clear_fault()` only resets the flag and keeps the old socket. After a timeout, the late reply is read as the answer to the next command. Proven: `get_part_timeout()` returned `'200 x=9.0 y=9.0 rz=9.0'`. | `devices/Asyril.py:62-63` | `test_clear_fault_opens_a_fresh_session` |
| 3 | 4: a failed init is a fault | `initialize()` sets `self._faulted`, but the `faulted` property overwrites it from `api._faulted`, so a failed init never shows as faulted. If startup fails after the connect, the socket stays open and the feeder reports **connected and ready**. | `devices/Asyril.py:34-37`, `:44-52` | `test_initialize_failure_raises_and_faults[dead_port, state_query_refused]` |
| 4 | 4, plus Step 4 (registry defaults match the constructor) | `start_production()` and `set_part_timeout()` never check the reply. An unknown recipe (`407`) comes up "ready", and every `get_part` then fails. On top of that, the registry gives `recipe` a default of `0` while the constructor requires it, so a config without `recipe:` silently runs recipe 0. | `devices/api/AsyrilAPI.py:177-194`, `:236-251`; `devices/registry.py:446` vs `devices/Asyril.py:8` | `test_initialize_with_an_unknown_recipe_fails`, `test_registry_requires_a_recipe` |
| 5 | 4: close, then connect | `initialize()` doesn't close the previous session, so every INITIALIZE leaks a TCP connection. The Eye+ limits concurrent connections (error 411). | `devices/Asyril.py:44-47`, `AsyrilAPI.py:156-175` | `test_initialize_twice_closes_the_previous_session` |
| 6 | 4 | A successful re-initialize doesn't clear an earlier comm fault (`api._faulted` is never reset), so INITIALIZE alone can't bring the feeder back. | `AsyrilAPI.py:156-175` | `test_initialize_clears_a_previous_comm_fault` |
| 7 | 7: abort unblocks now | `abort()` only logs "no motion to clear". `get_part` blocks until the feeder answers (30 s part timeout, 35 s socket timeout), so after ABORT or a fault elsewhere, the task thread stays stuck. | `devices/Asyril.py:65-66` | `test_abort_unblocks_a_pending_get_part` |
| 8 | 5: shutdown is always safe | If `stop production` fails (silent or dead link), `disconnect()` is skipped. The socket leaks, the feeder still reports `connected`, and in production the shutdown waits 35 s first. | `devices/Asyril.py:54-60` | `test_shutdown_closes_even_when_the_feeder_does_not_answer` |
| 9 | API: a session that frames replies correctly | Each reply is read with a single `recv(4096)` and no line buffering. A reply split over two TCP segments is parsed half-way, and its tail becomes the next reply. In the test, a split reply already **breaks `initialize()`**: the error was `Failed to take calibration image: eady`. | `AsyrilAPI.py:602-623` | `test_reply_split_over_two_segments_is_reassembled` |
| 10 | API: device errors are surfaced | A non-200 `get_part` reply returns `{'resp': {'403'}}`, a *set* holding the raw string. It is not JSON-serializable and never equals a status code. `get_part_async` returns `{'resp': 403, 'raw': ...}` instead. | `AsyrilAPI.py:262` | `test_get_part_error_reply_has_an_int_code` |
| 11 | 1: status never blocks | `AsyrilEyePlusApi.connected` peeks at a blocking socket: it waits the whole timeout (35 s) and then raises `TimeoutError`. The driver's own `connected` doesn't use it, but task code could. | `AsyrilAPI.py:132-154` | `test_api_connected_answers_at_once` |

## Findings: static only (no test)

12. **Rule 2 / API lock: the socket has no lock.** The task thread and the `get_part_async` worker share the socket, protected only by a docstring ("do not call other methods while pending"). `AsyrilAPI.py:128-130`, `:280-283`.
13. **Rule 3: no fault reason.** The fault is a bare bool, not `_fault: str`, so neither the UI nor the log says *why* the feeder faulted.
14. **API errors.**
    - There is no exception hierarchy, and device error codes are returned as raw strings, never raised.
    - `__handle_response__` (`:625-635`) is dead code, and it would crash on `EyePlusErrorCode(int("401 ..."))`.
    - `reset_state` raises a copy-pasted message, "Failed to take calibration image" (`:559`).
    - It parses the state as `response[4:-1]`, which assumes a 1-character terminator (`:551`).
15. **Rule 11: logging.** `shutdown()` uses `print()` (`Asyril.py:60`): you can see "Failed to shutdown: ..." on stdout in pytest.txt. No message carries the `[device_id]` prefix.
16. **Timeout.** `TIMEOUT = 35` is hard-coded (`AsyrilAPI.py:21`) and not configurable. If task code calls `set_part_timeout()` with more than 35 s, the socket times out first, which means a fault and then desync (finding 2).
17. **Docs.**
    - `README.md:150` and `:726` call it a "REST API/client". It is ASCII over TCP on port 7171.
    - The config.yaml reference doesn't say `recipe` is required.
    - `info` holds no identity (model, serial, firmware).
    - `AsyrilEyePlus` has no class docstring, and `api` has no type annotation.
    - The API has no `_cli()` or context manager.

What's fine:

- It is registered, exported lazily, and listed in config.yaml, the README tables and the "Releasing a device" list. It needs no new dependency.
- The constructor doesn't connect.
- `faulted`, `ready`, `connected` and `info` send nothing.
- A timeout latches a fault.
- A refused command doesn't fault the cell.
- `clear_fault()` and `abort()` send nothing that vibrates the feeder or takes an image.
- `shutdown()` is safe when never connected, and safe twice.
- Disconnect followed by connect works.

## Files

- `tests/test_asyril.py` (new): 27 tests.
- `tests/fake_instrument.py` (new): the shared fake TCP instrument from the add-device testing guide.

## Verified

- `python -m pytest -q tests`: **147 passed, 13 xfailed**.
- With `--runxfail`, each of the 13 xfail tests fails for the reason described above, not for some unrelated error.
- I didn't talk to real hardware.

## Not verified: check at the machine

I didn't have the Eye+ protocol manual, so I couldn't check these:

1. Whether `stop production` (or another command) interrupts a pending `get_part`. Error `510 GET_PART_INTERRUPTED` suggests something does. Fix 7 depends on the answer.
2. The unit of `set_parameter timeout 30.0`. The driver assumes seconds.
3. The reply terminator. The driver assumes `\n`; `reset_state` breaks on `\r\n`.
4. The exact `get_parameter state` values and their matching `stop <state>` commands.

## Suggested changes (not applied)

- `app_logic/prod.py:67` ignores the result of `feeder.api.get_part()`. With the real feeder, a `501` (no part found) or `403` reply comes back as a dict, and the robot picks at nothing. The customer workspace (not in this repo) should check `result["resp"] == 200` before picking.
- In that workspace's calibration task, call `set_calibration_pose()` **before** `take_calibration_image()` for each pose. The image call increments the pose index; the order is locked in by `test_calibration_points_and_images_share_the_pose_index`.

**Question:** which findings should I fix before delivery? I'd fix 1–9 and 13 in the driver, plus the registry default from 4, and leave 10–12 and 14–17 for after the delivery unless you want them now. Each fix flips its xfail test to pass. Fix 7 needs the answer to "Not verified" item 1, or I can make `abort()` close the socket to unblock `recv` (no feeder command needed).