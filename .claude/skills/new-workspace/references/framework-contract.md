# What the app expects from a workspace

The contract between `mecademic_demo_app` (the framework) and a workspace folder, as the code implements it. Paths are relative to `mecademic_demo_app/`. Read this before writing any workspace file: most bugs in existing workspaces come from one of the "silent fallback" rules below.

## Contents
1. Layout and files
2. How the code is loaded (and the silent fallbacks)
3. Task functions and what the runner does around them
4. config.yaml
5. context.yaml
6. Steps and sequences
7. Manual actions
8. Custom view (optional)
9. Running, the workspace picker, logs

---

## 1. Layout and files

New workspaces use the **flat layout**: every file directly in the workspace folder, like the client workspaces in `dev_ws/` and the folders in `examples/`. (The app also reads an `app_logic/` subfolder layout, used by the repo itself; don't create new ones that way.)

| File | Needed | Why |
|---|---|---|
| `config.yaml` | always | Devices, manual actions, sequences, title. A folder without it is not a workspace (`core/Workspaces.py:37-44`). |
| `prod.py` with `prod_cycle(devices, context)` | always | PROD loop body. |
| `home.py` with `home(devices, context)` | always | HOME, and the entry/exit home of PROD. |
| `shipment.py` with `shipment(devices, context)` | always | SHIPMENT button. |
| `calib.py` with `calib(devices, context)` | always | CALIBRATION button. |
| `manual_actions.py` with `get_manual_actions()` | always (may return `{}`) | Manual tab buttons. |
| `context.yaml` | recommended | Params and variables; without it `set_variable()` raises. |
| `cell_tools.py` | when robots run programs | Program/checkpoint helpers (skill asset). |
| `custom_view/` | optional | Extra page with live data and feeds. |
| `README.md`, `WORKSPACE_SPEC.md`, `.gitignore` | always (skill convention) | For the people at the machine. |

No `__init__.py`: the folder is put on `sys.path` and its modules are imported by bare name; a package marker is not needed and invites relative imports, which fail in the flat layout.

## 2. How the code is loaded (and the silent fallbacks)

- `app.py --workspace <dir>` inserts `<dir>` at `sys.path[0]` **before importing the framework** (`app.py:24-25`).
- Task functions are resolved per run from the module candidates `("prod", "app_logic.prod")`, same for `home`, `shipment`, `calib` (`core/Task.py:55-61`). Manual actions from `("manual_actions", "app_logic.manual_actions")` (`core/ManualActions.py:7`). Custom view from `("custom_view", "app_logic.custom_view")` (`core/ApplicationController.py:49`).
- **Silent fallback.** `app_logic` here is the repo's own demo package (simulated pick/inspect/place with `devices["my_meca_robot"]`). If a workspace module is missing **or fails to import** (a typo, an import of a module that no longer exists), the app loads the demo module instead, and the task then fails with `KeyError: 'my_meca_robot'`, far from the real cause. Hence:
  - ship all four task modules and `manual_actions.py`, even when a task has nothing to do;
  - import only what exists today: `from core import AppContext`, `from devices import Device, MecaRobot` (lazy exports, `devices/__init__.py`). `core.ProductionContext` no longer exists; the old template and the old client workspaces still import it.
  - Without a `custom_view/`, the app tries the demo's custom view at INITIALIZE and logs `Custom view setup failed: 'my_meca_robot'`. Harmless (no view link is shown), but expect the line in the log.
- Imports between workspace modules are flat: `from cell_tools import run_program`, `from prod import pick_part`.
- **Module names must not shadow anything**, because the workspace is first on `sys.path`: not `core`, `devices`, `app`, `app_logic`, `tests`, `conftest`, `static`, `templates`; no standard-library name (`io`, `queue`, `time`, `random`, `calendar`, `logging`, `select`...); no installed package (`numpy`, `yaml`, `flask`, `mecademicpy`...). A *device* may be called `io`; a *file* `io.py` breaks Python.
- Switching workspace or RELOAD CODE purges the workspace's modules by name and re-imports them (`core/Workspaces.py:147-176`), so edits are picked up without restarting. Threads a workspace starts itself are not stopped.

## 3. Task functions and what the runner does around them

Signature: `def prod_cycle(devices: Dict[str, Device], context: AppContext)`. Same shape for `home`, `shipment`, `calib` and every manual action. The runner passes `context` whenever the function takes two positional parameters (`core/Task.py:64-84`), so declare it required and don't write `if context is None` branches.

- `devices` maps the names under `devices:` in config.yaml to driver objects. `devices["robot"].api` is the SDK object (`mecademicpy.Robot` for a robot), `.logger` writes `logs/devices/<name>.log`. What each type offers: `device-task-api.md`.
- **INITIALIZE already prepared the hardware**: robots are connected, error-reset, activated and homed, motion resumed (`devices/MecaRobot.py:184-213`); an Asyril feeder is in production with its recipe; SmarAct stages are referenced. Task code never repeats this.
- **PROD** (`core/Task.py:327-395`): `home()` inside a framework step `Home (entry)`, then `prod_cycle()` in a loop, each call one cycle. STOP lets the current cycle finish, then runs `home()` in `Home (exit)`. Count cycles is automatic.
- **ABORT** (button, or any device turning `faulted`) calls `abort()` on every device. For a robot that is `ClearMotion()` + `ResumeMotion()`, which makes a pending `WaitIdle()` or checkpoint wait raise `InterruptException`. An exception that escapes `prod_cycle` after an abort is a clean exit; no exit home. Other devices differ (an Asyril `get_part()` is not interrupted): see each section of `device-task-api.md`. Never catch and swallow exceptions around robot waits, or ABORT stops working.
- **Faults**: any exception escaping a task function (PROD without abort, HOME, SHIPMENT, CALIBRATION, manual action) sets the cell FAULTED with the message in a toast. Raise `RuntimeError("clear message")` for a process error the operator must see.
- A released device (Settings > Devices > Release control) blocks PROD/HOME/SHIPMENT/CALIBRATION; manual actions still run.

## 4. config.yaml

Top-level keys the app reads: `title`, `devices`, `manual_actions`, `sequences`, `history`, `context_file` (default `context.yaml`). Anything else is ignored.

```yaml
title: "Medical tray loading"          # name in the workspace picker

devices:
  robot:                               # the name task code uses: devices["robot"]
    type: "mecademic"
    ip_address: "192.168.0.100"
  spare_robot:
    type: "mecademic"
    ip_address: "192.168.0.101"
    enabled: false                     # kept in the file, not created
```

- Each `type` and its keys are defined in `devices/registry.py`; unknown types and missing required keys are errors that block INITIALIZE, unknown keys are warnings. `check_workspace.py` runs the same validation.
- A device type the app doesn't support yet: use the **add-device** skill first (built-in driver, or a workspace-only `type: "custom"` driver with `driver: "module:Class"`).
- Device names: short snake_case roles (`robot`, `left_robot`, `feeder`, `io`, `camera`, `vision`), unique, stable: they appear in code, logs and the UI.

## 5. context.yaml

```yaml
settings:
  default_reset_events: none
params:                       # tunable inputs, edited in the Variables tab while idle
  program_timeout_s: {type: float, default: 60, editable_when_idle: true, description: ...}
variables:                    # values the code carries between cycles
  part_count: {type: int, default: 0, editable_when_idle: false, reset_on: prod_start, persist: true, description: ...}
```

- Types: `int | float | bool | str`. `reset_on`: `initialize`, `prod_start`, `prod_stop`, `abort`, `fault`, a list of them, or `none`.
- `persist: true` keeps the value in `context.state.json` next to the file (git-ignore it).
- Code: `context.get_param(name, default)`, `context.get_variable(name, default)`, `context.set_variable(name, value)`, `context.set_param(...)`. Writing an undeclared name raises `ContextError`; reading one silently returns the default in code. Declare every name the code uses. No `force=` argument (ignored).
- The file is never rewritten by the app, so comments are safe.

## 6. Steps and sequences

- `with context.step("Pick part"):` reports what the cell does (sidebar, Production tab), times it, records failures. Steps may nest; nested steps are timed but only top-level ones move the "Step n of m" position.
- Names are **stable literals**: verb + object, sentence case, no part numbers or counters (each distinct name is a timing bucket, capped at 200).
- One stack per task: **never open a step from another thread** (inside `in_parallel` branches); wrap the parallel block in one step in the task thread.
- `sequences:` in config.yaml declares the expected top-level order per task key: `prod_cycle`, `home`, `shipment`, `calib`. Names must match the code exactly. A step not in the list shows "Off sequence". Put conditional steps (reject, retry, refill) **last**, so a cycle that skips them simply ends early.
- The runner's own `Home (entry)` / `Home (exit)` steps wrap the home inside PROD; they are not declared.
- **Don't declare `manual:<key>` sequences.** The runner wraps every manual action in its own top-level step `Manual: <key>` (`core/Task.py:409`), so the action's steps are nested and such a sequence never advances (the UI shows "Off sequence"). The action's steps still show and are timed.
- Time not inside any step is shown as idle time on the Production tab. That is useful: don't wrap pacing sleeps.

## 7. Manual actions

```yaml
manual_actions:
  - key: "open_gripper"                 # must be returned by get_manual_actions()
    label: "Open gripper"               # button text
    description: "Release whatever the gripper holds."
    confirm_title: "Open the gripper?"
    confirm_message: "A held part will drop. Make sure nothing is below the gripper."
```

- Every configured key must exist in `get_manual_actions()`, or the controller refuses to start the workspace (`core/ApplicationController.py:281-305`). A key returned but not configured just has no button.
- A manual action is an exclusive task: the cell must be READY. Write it from the same functions PROD uses, so testing a piece by hand tests the production code.

## 8. Custom view (optional)

A `custom_view/` package with `__init__.py` defining `setup(hub, devices)`, plus `index.html` (served at `/view/`, linked from the sidebar).

- `hub.add_source(name, read, rate_hz, history_s=120.0, device=None)`: sample a value on its own thread (e.g. a robot pose through `robot.rt...(synchronous_update=False)`).
- `hub.add_video(name, grab, fps=10, quality=75, max_width=1280, device=None)`: an MJPEG feed at `/api/data/video/<name>.mjpg`.
- Task code publishes values with `context.publish(channel, value)`.
- The page reads `/api/data` (manifest) and `/api/data/read?ch=...&after=<cursor>`.
- Copy from `examples/gige_live_view/` (one feed), `examples/gige_cv_analysis/` (two feeds + a result channel) or `app_logic/custom_view/` (plots). Write it only if the spec asks for one.

## 9. Running, the workspace picker, logs

- Run: `python <app>/app.py --workspace <workspace>`; on Windows from the superproject: `start_demo.bat <workspace>` (relative to the superproject folder, or absolute).
- The picker (Settings > Workspace) lists the folders next to the startup workspace (subfolders of its parent) that hold a `config.yaml`, by `title`. So where the folder lives decides which neighbours the picker shows.
- Logs go under `$MECADEMIC_DEMO_ROOT/logs/` (default: the folder above the app, i.e. the superproject): `logs/app/ApplicationController.log`, `logs/devices/<name>.log`, `logs/history/<workspace folder>/`.
- Backups of robots go to `<workspace>/backups/` (git-ignore it).
