---
name: add-device
description: Add support for a new hardware device (robot, stage, sensor, camera, I/O module, feeder, power meter, gripper, PLC...) to the Mecademic demo app. It covers the Device driver class in devices/, the protocol module in devices/api/, the registry.py entry, the devices/__init__.py export, the config.yaml reference, the README tables, requirements, and a hardware-free pytest with a fake transport. Use this skill whenever the user wants to add, integrate, support, wrap or write a driver for a device or instrument in this repo, mentions a new device type or vendor SDK/protocol manual for the cell, or asks for a workspace-only `type: "custom"` driver, even if they never say "device" or "driver" (for example "can the cell talk to our Keyence sensor?").
---

# Add a device to the Mecademic demo app

A device here is more than a class. The app polls every device from three threads, so a driver that does the obvious thing (query the hardware when asked for its status) can stall the UI or deadlock the fault monitor. The cell also only knows a device through `devices/registry.py`, so a class that isn't registered can't be used. This skill walks through all of it, in the order that keeps each step testable.

Read `devices/Device.py` first: it is the contract. Then read the existing driver closest to the new device and copy its structure:

| New device looks like... | Copy from |
|---|---|
| Text protocol (ASCII/SCPI) over TCP or VISA, one session | `devices/SmarActMCS2.py` + `devices/api/smaract_mcs2.py` (motion), `devices/ThorlabsPowerMeter.py` + `devices/api/thorlabs_pm.py` (measurement) |
| Remote digital I/O | `devices/IoLogikE1212.py` (Modbus via pymodbus), `devices/BrainboxesED.py` |
| Vendor Python SDK on PyPI | `devices/MecaRobot.py` (and `tests/test_meca_robot.py` for faking an SDK) |
| Camera | `devices/GigEVisionDevice.py`, `devices/CameraFeatures.py` |

## Step 1: Gather the facts

Collect these before writing code. Take what the user, the attached manuals, the vendor SDK docs or a public open-source driver already say. Ask the user only for what you can't find, and ask once, in one message.

- **Model and vendor**, and what the cell needs it to do (read a value, move an axis, toggle outputs...). Wrap that, plus whatever initialize/fault handling needs. Don't wrap the whole manual.
- **Transport**: TCP port, serial, USB/VISA, Modbus, HTTP/REST, or a vendor SDK.
- **Protocol source**: the command syntax, terminator, how errors are reported (inline, error queue, status bits), and whether a reply comes for setters.
- **Config keys**: what changes between installations (IP, port, channel list, startup settings). Everything else stays a constant in code.
- **Does it move, or does it block?** That decides what `abort()` must do.
- **Built-in or workspace-only?** Built-in by default (any cell can use it). Use a workspace driver (Step 8) only when the user says it belongs to one workspace or one project.

### Scope: add the device, don't rework the cell

The deliverable is the driver and what Steps 2 to 8 list. Leave these alone unless the user asked for them by name:

- task modules (`prod.py`, `home.py`, `manual_actions.py`...) and other app logic
- other devices' entries in config.yaml (their `autostart`, addresses, options)
- existing tests, and other drivers

When the request implies such a change ("the cell grabs a frame with the flash on"), put it in the report instead: show the few lines of task code that would use the new device, and say which file they go in. The user decides whether to apply them. A driver PR that also rewrites the production sequence is hard to review and easy to reject.
Never invent protocol commands. If the syntax for something comes from memory, a forum post or a similar model's manual, say so in the API module's docstring (smaract_mcs2.py names its sources), mark each unverified command with a short comment, and list them in your final report. A wrong command that looks right costs the user hours at the machine. A flagged guess costs them a minute.

**Cross-check the user's notes against the vendor's public manual** when you can reach it (web search, the vendor's site, a maintained open-source driver). Notes written from memory are usually right about the commands and wrong about the details that bite at the machine:

- **units**: a setpoint in "sensor units" rather than kelvin, counts rather than mm
- **timing**: a minimum gap between messages, a settle time after a mode change, a reply that comes only after the motion ends
- **framing**: the terminator, an echo, an address prefix, a checksum
- **side effects**: a command that saves to flash, resets the controller, or starts motion

Follow the manual where they disagree, mention the disagreement in the module docstring, and list it in the report. If you can't reach any source, say that too.

## Step 2: The protocol module (`devices/api/<snake_name>.py`)

Write one when the device speaks a text or binary protocol and no maintained Python SDK exists. Don't write one when a maintained SDK is on PyPI (mecademicpy, pymodbus, harvesters): wrap the SDK in the driver directly, and pin it in requirements.txt.

This repo prefers plain sockets, pyserial or PyVISA over vendor DLLs, so the same code runs on Windows and in the Linux Docker image. If the only option is a Windows-only DLL, tell the user that the Docker image can't use the device, before you build on it.

The module must stand on its own, without importing anything from `devices/` or `core/`, so it can be used from a notebook or a script. Read `references/api-module.md` for the template and the rules. In short:

- One `threading.RLock` serializes every exchange. Hold it across multi-step exchanges (a setter and its error check).
- On a timeout or socket error, close the session before raising. Otherwise the late reply is read as the answer to the next query.
- Use an exception hierarchy: `XError`, `XCommandError` (the device refused a command), and `XAborted` if anything blocks.
- If anything blocks (`wait()` for a move), it must notice `abort()` and raise `XAborted`. Use the `_aborts` counter in smaract_mcs2.py.
- Add raw `write()` / `query()` escape hatches, a `with` context manager, and a small `_cli()` under `if __name__ == "__main__":` so the user can test the hardware without the app.

## Step 3: The driver (`devices/<ClassName>.py`)

Read `references/driver-patterns.md` for the full skeleton and the reasons behind it. Every driver must follow these rules, because of who calls what:

| Caller (thread) | Calls | How often |
|---|---|---|
| Fault monitor | `faulted`, `ready` | every 200 ms |
| Flask `/api/info` | `info`, `connected`, `link` | every second, per open page |
| Flask Live panel | `telemetry()`, `io_points()`, `get_image()` | ~2 Hz while the panel is open |
| Task thread | `api.*`, your public methods | whenever the task runs |
| Controller (STOP/ABORT/fault) | `abort()` | while the task thread may hold the session lock |

That gives these rules:

1. **`faulted`, `ready`, `connected` and `info` never touch the hardware.** They return plain attributes. Read the identity (model, serial, firmware) once at connect, and keep it in `self._identity`.
2. **Comm errors latch a fault.** Subclass the API class as `_Cell<Api>`, override `write`/`query` to call an `on_comm_error` callback on socket/VISA errors, and let the callback set `self._fault = "Communication error: ..."`. Don't count usage errors (a bad parameter the device refused) as faults: the task gets the exception and the controller handles it.
3. **`self._fault: str | None`** holds the reason. `faulted` is `self._fault is not None`, and `connected` is `self._open and self._fault is None`.
4. **`initialize()`**: close, then connect. While connecting, any failure sets `_fault`, closes, logs and re-raises. Set `_open = True` before clearing `_fault`, so the monitor never sees "healthy but disconnected".
5. **`shutdown()`**: safe if never initialized and safe twice. Stop motion first if the device moves.
6. **`clear_fault()`**: reopen the session. Never home, reference or move hardware: an operator clicking "Clear faults" doesn't expect motion. Return early when not faulted.
7. **`abort()`**: must unblock a blocking wait in the task thread *now*, and must not queue behind the session lock. For a device that never blocks, it only logs a warning.
8. **Constructor**: config keys as keyword arguments, plus `name: str | None = None` last. Auto-generate `name` from the address when omitted, then call `super().__init__(device_id=name)`. Validate enum-like arguments with a clear `ValueError`. Never connect in the constructor: the controller creates every device at startup, and a dead device must not stop the app from starting.
9. **`supports_reconnect = True`** when `initialize()`/`shutdown()` can run in any order and more than once (almost always true if rules 4 and 5 hold). It enables Disconnect/Connect in the UI.
10. **Optional live data**: `telemetry()` returns `{}` when not connected, and its values must be JSON-serializable. `io_points()` returns `[{name, kind: di|do|ai|ao, value, writable}]`, and `write_io()` raises `ValueError` for unknown or read-only points. Live-panel calls go through the API lock, or read a cache.
11. **Logging**: `self.logger`, every message prefixed `[{self.device_id}]`.
12. **`api` property**: return the session/SDK object so task code can call anything (`devices["stage"].api.move_to_mm(0, 1.0)`). Type-annotate it with the API class, and give a docstring example of task usage.
13. **`telemetry()` stays cheap**: two or three short queries at most. It runs twice a second and shares the link with the task, so a slow device (one that needs 50 ms between messages, or a slow serial link) gets one query, or a value cached by the last task call.
14. **No blind retries of commands that act**: retrying a query after a timeout is fine. Resending a move, a relative step, a dispense or a toggle can do it twice. If a retry helps, retry only the queries, or re-read the state before resending.
15. **Outputs keep their state** on `abort()`, `shutdown()` and `clear_fault()`: heaters, lights, valves, digital outputs and power stages are not switched off unless the user asks. Something switched off without warning can drop a part, or let a process cool mid-cycle. Stopping motion is the exception (rules 5 and 7).

Write the class docstring in numpy style like its neighbors: what it is, the transport, how status and faults work, `Parameters`, and an `Example` block of task code.

## Step 4: Register (`devices/registry.py`)

Add a `register_device(DeviceSpec(...))` block among the built-in types, before the legacy `zaber` and the `simulated` entries:

```python
register_device(DeviceSpec(
    "keyence_il", "devices.KeyenceIL:KeyenceIL",
    {
        "ip_address": _p("str", doc="DL-EN1 IP address"),          # required: no default
        "port": _p("int", 64000, "TCP port"),
        "heads": _p("int_list", None, "sensor heads used; default: all"),
        "mode": _p("str", "fast", "sampling mode", choices=("fast", "precise")),
    },
    aliases=("keyence", "il"),
    doc="Keyence IL-series laser displacement sensor (DL-EN1, Ethernet ASCII).",
))
```

- `type` is snake_case, vendor + model (`thorlabs_pm`, `smaract_mcs2`). Aliases are optional short names; they must not collide with existing types or aliases (check `known_types()`).
- The target is a `"module:Class"` string so the SDK is imported only when a config uses the type. Never import the driver at the top of registry.py.
- Param types: `str`, `int`, `float`, `bool`, `list`, `dict`, `int_list`, `path`, `device` (the name of another device: the constructor receives the device object, see `cv_process`), `any`. A default of `None` makes a key optional and nullable. No default means required.
- Every key the constructor accepts (except `name`) gets a Param. Every Param maps to a constructor argument of the same name. The defaults match the constructor's defaults.
- Use `exclusive=(("a", "b"),)` for keys that can't be set together.

## Step 5: Export (`devices/__init__.py`)

Add the class name to `__all__` and a lazy branch to `__getattr__`, matching the existing ones. Lazy, because importing `devices` must not import every vendor SDK.

## Step 6: Test without hardware (`tests/test_<snake_name>.py`)

Read `references/testing.md`. It has a scripted fake TCP instrument (`tests/fake_instrument.py`: create it if it doesn't exist yet, reuse it otherwise), the monkeypatch pattern for SDK-based drivers, and the list of behaviors to cover: registry validation and creation, initialize and identity, no I/O from status properties, comm-error fault latching, clear_fault reconnecting without motion, a failed initialize, shutdown when never connected, abort unblocking a wait, a disconnect/connect cycle, and the live-data shape.

Run it, and run the whole suite, because registry changes affect every test:

```bash
.venv/Scripts/python.exe -m pytest -q tests          # Windows venv in this repo
python -m pytest -q tests                            # elsewhere
```

Iterate until both pass. If a test fails because the fake disagrees with the manual, fix the fake.

## Step 7: Docs, config reference and dependencies

These are easy to forget and are what the next person reads. Update each one that applies, matching the surrounding wording and format:

- **`app_logic/config.yaml`**: a reference block in the commented "SUPPORTED DEVICE TYPES" section, before the Simulated Device block: `# --- Vendor Model (ClassName) ---`, the type and its aliases, and every key with a short comment saying whether it's optional and what its default is. Don't add a live entry under `devices:`.
- **`README.md`**:
  - the "Additional devices" row of the Features table
  - the "Supported device types" table (before `zaber`)
  - the Live data table under Settings tab > Devices, if the driver has telemetry, I/O or images
  - the list of types offering release in "Releasing a device", if `supports_reconnect`
  - the `devices/` listing in Project Structure
- **`requirements.txt`**: a new direct dependency, pinned with `==` to the version you tested, under a commented group like its neighbors. Note system libraries in a comment.
- **`Dockerfile`**: `apt-get` packages, if a system library is needed (libusb is already there).
- **`docker-compose.yml`**: add the device to the comment that lists devices reachable over the bridge network, or to the USB passthrough note.

## Step 8: Workspace-only driver (`type: "custom"`)

When the driver belongs to one workspace, it lives in that workspace's folder, which is on `sys.path`, and registry.py stays untouched:

```yaml
devices:
  gripper:
    type: "custom"
    driver: "my_gripper:MyGripper"        # my_gripper.py next to config.yaml
    # driver: "app_logic.my_gripper:MyGripper"   # if the workspace uses the app_logic/ layout
    port: "COM7"
```

The rules of Step 3 all still apply. One difference matters: custom keys reach the constructor **unvalidated and uncoerced** (a YAML `port: "502"` arrives as a string), so the constructor converts and checks its own arguments, and raises a clear `ValueError`. Skip Steps 4, 5 and 7, but still write the test (in the workspace or in `tests/`, whichever the user prefers), and document the keys in a comment above the entry in that workspace's config.yaml.

A live entry in a workspace's config.yaml is initialized with the rest of the cell. If you don't know the device's real address, add the entry with `enabled: false` and a placeholder address, and tell the user to set both. An enabled entry pointing at nothing makes INITIALIZE fail for the whole demo.

## Step 9: Verify and report

Before you say you're done:

1. `pytest` passes, both the new file and the full suite.
2. A config entry validates and builds. Set `MECADEMIC_DEMO_ROOT` to a temp folder first: without it, the app's logger creates `logs/` two levels above the repo, which inside a git worktree is a stray folder next to the other worktrees:
   ```bash
   .venv/Scripts/python.exe -c "import os,tempfile; os.environ['MECADEMIC_DEMO_ROOT']=tempfile.mkdtemp(); from devices.registry import validate_devices_config, create_device; e,i=validate_devices_config({'x': {'type': 'keyence_il', 'ip_address': '10.0.0.5'}}); print(i); d=create_device(e[0]); print(d.device_id, d.info, d.faulted)"
   ```
   (pytest needs nothing: `tests/conftest.py` already does this.)
3. `git diff --stat` shows every file from Steps 2 to 7 that applies, and nothing outside the scope above.

Then report, with these four headings every time, even when one is short:

```markdown
## Files
<added and changed files, one line each>

## Config
<the YAML entry to paste under `devices:`, with every key the user is likely to set>

## Verified
<tests run and their result; real hardware only if you actually talked to it>

## Not verified: check at the machine
<numbered list: every command, unit, timing or default that came from notes, memory or a
similar model rather than the device's own manual, and every place the notes and the manual
disagreed. Then the CLI to check them: `python -m devices.api.<module> <address>`>
```

If the request implied changes outside the scope (task code, other devices), add a fifth heading, **Suggested changes (not applied)**, with the code and the file it goes in.
