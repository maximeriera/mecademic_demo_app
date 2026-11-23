---
name: validate-device
description: Validate, audit or review a device driver that already exists in the Mecademic demo app (devices/*.py, its devices/api/ module, registry entry, export, docs and tests, or a workspace `type: "custom"` driver) against the add-device contract. It runs a static wiring check, proves each finding with hardware-free pytest tests, can smoke-test real hardware read-only, reports pass/fail with file:line evidence, then offers fixes and changes nothing until the user picks them. Use this skill whenever the user wants to check, audit, review, validate, harden, test or "bring up to standard" an existing driver (Asyril, PlanarMotor, LMI, ioLogik, Brainboxes, Arduino, GigE camera, SmarAct, Thorlabs, Tulip, MecaRobot...), asks whether a driver handles faults/abort/reconnect correctly, asks to audit all devices, or reports a symptom that points at a driver (UI freezes while a device is connected, Clear faults doesn't recover, ABORT doesn't stop a device, a device shows healthy after its cable is pulled), even if they never say "validate". Not for writing a new driver (that's add-device).
---

# Validate an existing device driver

The `add-device` skill defines what a driver in this app must do and why: the fault monitor polls `faulted`/`ready` every 200 ms, Flask polls `info` every second, ABORT arrives while the task thread holds the session lock. Many drivers here predate that contract and break it in ways that only show at the machine: a status property that queries the hardware stalls fault detection for the whole cell, a `clear_fault()` that only drops a flag leaves a dead session behind, an `abort()` that does nothing lets a stage finish its move.

This skill audits one driver (or several) against that contract. It proves what it can with hardware-free tests, smoke-tests the real device only when the user says it is reachable, and reports. **It doesn't touch driver code until the user picks the fixes.** The test file is the only thing it writes on its own. Tests are additive, and they are the evidence for the findings.

## The source of truth

Read these before judging anything. They hold the rules and the reason for each one. This skill doesn't repeat them:

- `.claude/skills/add-device/SKILL.md`: Step 3 (the 12 driver rules and the caller/thread table), Steps 4, 5 and 7 (registry, export, docs)
- `.claude/skills/add-device/references/driver-patterns.md`: the skeleton, `abort()` by device kind, the optional members
- `.claude/skills/add-device/references/testing.md`: `tests/fake_instrument.py`, what to cover, the test template
- `.claude/skills/add-device/references/api-module.md`: the protocol module rules
- `devices/Device.py`: the abstract contract
- A driver that follows the pattern, as the yardstick: `devices/SmarActMCS2.py` (moves), `devices/TulipGripper.py` (moves, serial or TCP), `devices/ThorlabsPowerMeter.py` (measures)

## Step 0: Pin down the target

The user may name a type (`asyril`), an alias, a class (`AsyrilEyePlus`), a file, a symptom ("the UI hangs when the planar motor is on"), or a workspace driver (`examples/<ws>/my_light.py:MyLight`). Resolve it to one driver class. When it is a symptom, find the device the symptom points at (check the workspace's config.yaml) and say which driver you're auditing and why.

**"Audit all the devices"**: run the wiring check (Step 1) on every registered type (`python -c "from devices.registry import _SPECS; print(*_SPECS)"`). Show one table: type, class, FAIL/WARN counts, whether a dedicated test exists, plus your one-line guess at the worst problem. Then ask which drivers to audit in depth. A full audit costs real time per driver. Put the drivers the user's workspaces actually use first (grep `type:` in `app_logic/` and `examples/`). `simulated*` and the legacy `zaber` come last.

## Step 1: Wiring check (script)

```bash
.venv/Scripts/python.exe .claude/skills/validate-device/scripts/check_wiring.py <target>   # Windows venv
python .claude/skills/validate-device/scripts/check_wiring.py <target>                    # elsewhere
```

It parses the driver and its API module with `ast` and never imports them, so a missing vendor SDK doesn't matter. It checks:

- the abstract members
- calls and attribute reads inside the status properties
- the constructor (`name` argument, connecting in `__init__`)
- trivial `abort()` and `clear_fault()` bodies
- registry Params against the constructor's arguments and defaults
- the `__init__.py` export
- the API module (standalone, lock, CLI)
- the config.yaml reference block, the README rows, requirements.txt
- whether a test exercises the class

`[FAIL]` lines are almost always real. `[WARN]` lines are **leads, not findings.** For status properties, the script follows one hop into the API module. When `faulted` reads `self._api.faulted` and that's a `@property` calling `get_pmc_status()`, it prints a FAIL naming both lines. It stops there, though. A call into a wrapper class that takes the session lock (ioLogik's `_SerializedClient`), or a property that calls a method that sends, still needs you to follow it to the end in Step 2. Confirm every status-property FAIL by reading the code before you report it.

## Step 2: Read the code and walk the checklist

Read the whole driver, its API module (the methods the driver calls, at least), and every property or method a status property reaches, **all the way down**. A status property that reads `self._api.x` is only clean if `x` is a plain attribute. If it's a `@property` or a method in the API that sends a command, peeks the socket (`recv(..., MSG_PEEK)`) or takes the session lock, that's I/O.

Give each item a verdict: **PASS**, **FAIL**, **N/A** (with the reason, e.g. "sensor, nothing blocks") or **UNVERIFIED** (couldn't tell from code or tests). Give file:line evidence for each one.

| ID | Check | Rule source |
|---|---|---|
| **A: Status & threading** | | |
| A1 | `faulted`, `ready`, `connected`, `info`, `link` do no hardware I/O and take no lock a task exchange holds, through every hop | add-device Step 3 rule 1 |
| A2 | Those getters can't raise (the monitor thread must survive), e.g. no attribute access on an `_api` that can be `None` | driver-patterns §1 |
| A3 | `info` values are JSON-serializable, and the identity is read once at connect | rule 1, Device.py |
| A4 | `faulted` means a fault, not "not connected": it is False before `initialize()` and after `shutdown()`, and a failed `initialize()` or a comm error sets it | rule 3 |
| **B: Faults** | | |
| B1 | A comm error during a task call (socket/serial/VISA/SDK link error) latches `faulted` until cleared | rule 2 |
| B2 | A refused command (usage error) raises to the task but does **not** fault the device | rule 2 |
| B3 | The fault reason is kept as text and logged, not just a bool | rule 3 |
| B4 | `clear_fault()` reopens the session (not just drops the flag), never homes or moves, and returns early when not faulted | rule 6 |
| **C: Lifecycle** | | |
| C1 | `initialize()` closes, then connects. A failure leaves `faulted`/not `connected` and re-raises. It sets `_open` before clearing the fault | rule 4 |
| C2 | `shutdown()` is safe when never initialized and when called twice, and stops motion first on a moving device | rule 5 |
| C3 | Constructor: config keys as kwargs, `name=None` last, no connection, enum-like args validated | rule 8 |
| C4 | `supports_reconnect` is honest: when True, `disconnect()` then `connect()` works. When False but C1 and C2 hold, say it could be enabled | rule 9 |
| C5 | `abort()` on a device that moves or blocks unblocks the task's wait within about 1 s, without queuing behind the session lock, and never raises. On a measuring device it logs a warning | rule 7, driver-patterns §5 |
| **D: Live data** (only if implemented) | | |
| D1 | `telemetry()` is `{}` when not connected and JSON-serializable when connected. `io_points()` has the documented shape. `write_io()` raises `ValueError` for unknown or read-only points | rule 10 |
| **E: Protocol module** (only if `devices/api/<x>.py` exists) | | |
| E1 | It imports nothing from `devices/` or `core/` | api-module.md |
| E2 | One `RLock` serializes every exchange, held across multi-step ones | api-module.md |
| E3 | On a timeout or socket error it closes the session before re-raising (no late reply read as the next answer) | api-module.md |
| E4 | Link errors stay `OSError`, and device-reported errors get their own exception class | api-module.md |
| E5 | The `__main__` CLI is read-only by default (motion, output writes and state changes behind an explicit flag), and it takes the address as an argument rather than hard-coding it | api-module.md |
| **F: Wiring & docs** | | |
| F1 | Registry Params match the constructor's arguments one to one, with the same defaults. Aliases don't collide | add-device Step 4 |
| F2 | `devices/__init__.py`: in `__all__`, with a lazy `__getattr__` branch | Step 5 |
| F3 | config.yaml reference block, README tables (Supported types, Live data, Releasing, Project Structure), requirements.txt pin | Step 7 |
| **G: Logging** | | |
| G1 | `self.logger` only (no `print`), every message prefixed `[{self.device_id}]` | rule 11 |
| **H: Tests** | | |
| H1 | A dedicated `tests/test_<snake>.py` covers what testing.md lists for this kind of device | testing.md |

**Severity**, so the report leads with what matters:

- **Critical**: can stall or kill the cell, or hides a dead device. I/O or a shared lock in a status property (A1), a getter that can raise (A2), comm errors that never fault (B1), an `abort()` that doesn't stop a moving device (C5), a constructor that connects (C3).
- **Major**: wrong behavior an operator hits. A `clear_fault()` that leaves a stale session or moves hardware (B4), a failed init reported as healthy (C1), a `shutdown()` that raises (C2), a usage error faulting the cell (B2), a Param/argument mismatch that raises TypeError at creation (F1), no tests at all (H1).
- **Minor**: everything else. Docs, logging, a missing `__all__`, a default that differs only for direct construction.

## Step 3: Prove it with hardware-free tests

Code reading says "probably". A test says "yes". Write tests for the behaviors in the checklist, especially every Critical or Major suspicion, so the report rests on evidence and the later fix has a test that turns green.

- **No dedicated test file yet**: create `tests/test_<snake_name>.py` and follow `add-device/references/testing.md` (the coverage list, the template, `tests/fake_instrument.py`).
- **A test file exists**: run it, map what it covers against the testing.md list, and add the missing behaviors to the same file under a `# --- Added by validate-device ---` comment. Don't rewrite existing tests.

How to fake the device depends on the transport. Read `references/fakes.md` for the patterns this repo needs: a raw socket fits `FakeTcpInstrument`, while pymodbus, pmclib, harvesters and pyfirmata are monkeypatched in the driver or API module. It also covers counting I/O to prove A1, and simulating a link drop for B1.

**When a test fails because the driver breaks the contract**, keep the test as it is and mark it:

```python
@pytest.mark.xfail(strict=True, reason="validate-device A1: PlanarMotor.faulted queries the PMC through api.is_connected")
def test_status_properties_never_touch_the_device(...):
```

`strict=True` keeps the suite green today. Once the fix lands, the test passes, strict xfail turns that pass into a failure, and whoever applied the fix removes the marker. The test then guards against regressions. Never weaken an assertion to make a test pass. If a test fails because the **fake** is wrong (it disagrees with the manual or the real protocol), fix the fake.

Run the new file, then the whole suite:

```bash
.venv/Scripts/python.exe -m pytest -q tests/test_<snake_name>.py
.venv/Scripts/python.exe -m pytest -q tests
```

Both must pass (xfails count as passing). If the full suite was already failing before you touched anything, say so, and show that your file doesn't add failures.

## Step 4: Live hardware smoke test (only when the user says the device is reachable)

Skip this step unless the user says the device is on the network/USB from this machine and gives the address, or points to a config entry. It is read-only by default:

1. **Read `initialize()` first.** If it moves anything or changes the device's state (homing, referencing, levitating movers, activating a robot, starting production on a feeder), tell the user exactly what will happen, and wait for a yes before you run it. The same goes for any motion, output write or settings change: one approval per action.
2. If the protocol module has a CLI (`python -m devices.api.<module> <addr>`), **read its `__main__` block before running it.** Several older modules run an `example_usage()` that toggles outputs, pulses DOs, resets counters or starts production on a hard-coded IP (`IoLogikE1212Api.py`, `AsyrilAPI.py`, the Asyril driver's own `__main__`). Run it only if it really is read-only. Otherwise write your own read-only probe, and report the CLI under E5.
3. Then run a short script (in the scratchpad, not the repo) that builds the device through the registry from the config entry and does the following:
   - `initialize()`
   - reads `info`, `connected`, `ready` and `faulted`
   - times 50 reads of each status property; more than about 1 ms per read points to I/O, which is good evidence for A1
   - calls `telemetry()`/`io_points()` if present
   - `shutdown()`, `initialize()` again (reconnect), `shutdown()` twice
4. Optionally offer a link-loss test: the user unplugs the cable, you make one task call, and you check that `faulted` latches and that `clear_fault()` recovers after they plug it back in. It needs the user's hands, so ask.

If the device can't be reached from here, report "not reachable from this machine". That isn't a driver failure. Never put a real device into a state the user didn't approve.

## Step 5: Report (in chat)

Open with a plain answer to what the user asked, in 2–4 sentences, before any table. If they described a symptom, say what causes it and how sure you are (proven by a test, measured, or read in the code). If they asked "can we ship it", say yes or no and why. Then use this structure, kept tight. Minor findings can be one line each, and the table is for the findings the user has to decide about:

```markdown
## <ClassName> (`<type>`): validation

Files: devices/X.py, devices/api/x.py, registry entry, tests/test_x.py (new)
Verdict: <n> Critical, <n> Major, <n> Minor. <One sentence: what it means for running a cell with this device.>

### Findings
| # | Sev | Check | Finding | Evidence | Proven by |
|---|---|---|---|---|---|
| 1 | Critical | A1 | `faulted` → `api.is_connected` → `get_pmc_status()`: a controller query every 200 ms | devices/PlanarMotor.py:30, devices/api/PlanarMotorApi.py:55 | test_status_properties_never_touch_the_device (xfail) |

### Passed
A one-line list of the checks that passed, by ID.

### Not verified
What couldn't be tested and why (no hardware, SDK not installed, a behavior only the real device shows), plus the command to check it at the machine.

### Tests
tests/test_x.py: <n> passed, <n> xfailed (one per open finding). Full suite: <result>.
```

"Proven by" names the test, the live-run observation, or "code reading". The user should be able to tell a confirmed bug from a suspicion.

## Step 6: Offer fixes, then apply only what the user picks

End the report by offering fixes grouped by severity, each with one line on what would change (e.g. "B4: make clear_fault() close and reopen the socket instead of resetting the flag"). Ask which to apply. Don't edit the driver, the API module, the registry or the docs before the user answers.

If the request itself already asked for fixes ("validate it and fix what's broken"), that is the user's pick. Still write the report and the tests first, so every fix has a failing test behind it, then apply the fixes in scope. "Fix what's broken" means making the driver keep the contract. It doesn't extend to new features or new configuration. These stay proposals: list them and don't do them:

- anything that changes behavior task code relies on: return values, exception types it doesn't already catch, the number of channels read
- new config keys, changed defaults (timeouts, retries), new registry Params. Each one changes every installation's behavior, and the user sets those values per cell.
- anything that needs the hardware to get right: register maps, which command stops motion, whether a reconnect keeps production state

Edit `registry.py`, `config.yaml` or the README only when a fix changes something they already document.

When applying:

- Follow the `add-device` patterns (`driver-patterns.md` skeleton, `api-module.md` rules). Move the driver toward the pattern, but don't rewrite what already works.
- **Keep the public surface task code uses**: the methods on `.api`, the public helpers on the class, the constructor arguments. Workspaces in `app_logic/` and `examples/` call them. Grep for usages before renaming or removing anything, and say what you found.
- Remove the `xfail` marker from each test whose finding you fixed, and rerun the file and the full suite. A test that still fails means the fix isn't done.
- Report what changed per finding, and what is still open. Don't commit unless asked.
