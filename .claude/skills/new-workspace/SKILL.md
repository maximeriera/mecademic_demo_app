---
name: new-workspace
description: Create a new workspace (a new cell, demo or application) for the Mecademic demo app — its own folder with config.yaml (robots, feeders, cameras, I/O...), context.yaml, the HOME / PROD / SHIPMENT / CALIBRATION sequences, manual actions, README, git repo — through an explicit interview, a WORKSPACE_SPEC.md the user approves before any code is written, and a simulated smoke test of every task. Use this skill whenever the user wants to set up, start, scaffold or build a new demo, cell, line, project, client application or example for the Mecademic demo app, describes a cell ("two Meca500 and an Asyril feeder that...") and wants it running, or asks to turn a described pick-and-place / assembly / inspection / dispensing sequence into a workspace, even if they never say "workspace". Also for a new reusable example under mecademic_demo_app/examples/. Not for editing an existing workspace or app_logic/, and not for writing a device driver (that is the add-device skill).
---

# Create a new workspace for the Mecademic demo app

A workspace is a folder the app loads with `--workspace`: the cell's devices, its sequences and its tunable values. The framework (`mecademic_demo_app`) provides the UI, the drivers and the task runner. Getting a workspace wrong is easy and quiet: a missing or broken task module makes the app run the repo's own demo code instead, and an invented IP address or robot pose looks exactly like a real one. So this skill is deliberately explicit: interview, a written spec the user approves, code, a simulated run of every task, then a report that separates what was verified from what still needs the machine.

## Ground rules

- **A new folder every time.** Never write into `mecademic_demo_app/app_logic/` (it is the repo's template and demo) or into an existing workspace. A reusable demo goes to `mecademic_demo_app/examples/<name>/`; a client cell goes to its own folder.
- **Nothing but the spec before approval.** Until the user approves WORKSPACE_SPEC.md in chat, the only file this skill writes is that spec.
- **Never invent machine facts silently.** IP addresses, robot program names, checkpoint numbers, poses, recipe numbers, I/O channel meanings: use what the user gave. Where you must propose a value, mark it `TODO: confirm` in the spec and in the code, and list it in the final report.
- **Ask in rounds, not one question at a time.** Each round is one message. Offer a sensible default for every choice so the user can just say "ok".

## Phase 0 — Ground yourself (no questions yet)

1. Locate the app: the folder holding `core/ApplicationController.py`, usually `.../mecademic_demo/mecademic_demo_app`. Note its commit: `git -C <app> log -1 --format=%h`.
2. Find the app's Python: `<app>/.venv/Scripts/python.exe` (Windows) or `<app>/.venv/bin/python`. Every command below runs with it.
3. Read `references/framework-contract.md` in full. Skim `references/sequence-patterns.md`. You'll read `references/device-task-api.md` per device type in Phase 3.
4. Look at where existing workspaces live (siblings of the `mecademic_demo` folder in the user's dev folder, e.g. `mecademic_demo_medical_device`) to suggest a location and a naming style. They are useful to understand the user's cells, but **don't copy their code**: they predate the current API (`core.ProductionContext`, `production_context.yaml`, `force=True`) and `check_workspace.py` flags them. Same for `mecademic_demo_template`.

## Phase 1 — Interview

### Round 1: the cell in the user's words

Send one message asking for everything at once, as a fill-in list, and say they can answer loosely:

```
To set up the new workspace I need:
1. Name and location — folder name (snake_case, e.g. mecademic_demo_tray_loading), the title shown in the
   app's workspace picker, and where to create it (I suggest <dev folder>/<name>, next to your other
   workspaces). Or is it a reusable example for mecademic_demo_app/examples/?
2. What the cell does — the part, where it comes from, what happens to it, where it goes.
3. Devices — for each: a short name to use in code (robot, feeder, io...), the model, and its address
   (IP, COM port...) if you know it.
4. The production cycle — the steps in order, which device does what in each step, what happens when
   something goes wrong (no part, failed inspection...), and what may run at the same time.
5. The robot programs on the controllers — their names, what each does, and the checkpoint each ends
   with if they already exist. (Default: motion lives in robot programs and Python starts them.)
6. HOME, SHIPMENT and CALIBRATION — what each should do for this cell (or "nothing special").
7. Manual tab buttons you want (open gripper, pick one part, run one inspection...).
8. Values an operator should be able to tune (timeouts, speeds, retries) and counters to show.
Anything you don't know yet becomes a TODO in the spec; that's fine.
```

If the user's first message already answers some of this, don't ask it again: confirm what you understood and ask only for the rest.

### Round 2: close the gaps

From the answers, check each of these and ask for what is missing or ambiguous, in one message. Use multiple-choice questions (the AskUserQuestion tool, if available) where there are real options, with your recommendation first:

| Topic | What must be settled |
|---|---|
| Location | Absolute parent folder; folder must not exist yet (if it does, stop and ask). Client workspace or `examples/`. |
| Devices | A registry `type` for each (`device-task-api.md` table of contents lists them). A device the app doesn't support → say so and offer the **add-device** skill first; don't fake it with `simulated`. |
| Robot motion | Per robot: robot-side programs (default) or Python-side moves. For programs: names, end checkpoint numbers (propose 1-9 for home/shipment/calib, 11+ for production), robot variables that carry data in (e.g. a pick pose). |
| Cycle | Order of steps; for each step the device actions and what it waits for; branches (reject, retry) and where they rejoin; parallel parts and how robots hand over (checkpoint, I/O signal). |
| Failure behaviour | What faults the cell (timeouts, no part after N tries) vs what is a normal branch (reject). |
| Params / variables | Name, type, default, unit; counters with `reset_on: prod_start`, `persist: true`. |
| Custom view | Wanted or not (default: not, unless there is a camera or a live measurement to show). |
| Example convention | For `examples/`: should it run without hardware out of the box (live entry `simulated`, real one commented beside it, like the existing examples)? Default yes. |

Stop interviewing when every row is either answered or explicitly a TODO the user accepted. Two rounds are typical; three is the limit before you write the spec with TODOs and let the user correct the document instead.

## Phase 2 — The spec, and approval

1. Create the workspace folder and write **only** `WORKSPACE_SPEC.md` in it, from `assets/WORKSPACE_SPEC.md`. Fill every section; delete the placeholder text. Step names in §4 are the exact strings that will appear in the UI and in `sequences:`.
2. Show the user, in chat: the spec's path, a short summary (devices, the PROD steps as one line, the manual actions), and the list of open points (§9). Ask: *"Review WORKSPACE_SPEC.md (edit it directly if you like) and tell me what to change, or say approved."*
3. Apply every change to the spec (re-read the file first: the user may have edited it), show what changed, and ask again. Repeat until the user approves in words ("approved", "go", "looks good, build it"). Silence, or an answer to a different question, is not approval.
4. On approval, set the status line at the top to `Approved in chat on <date>` and fill §10.

## Phase 3 — Write the workspace

Write the files in this order, each from its asset in `assets/workspace/` (a small but complete cell: one robot running programs, a feeder, a remote I/O). The assets show the **structure, conventions and comments** to reproduce; the **content** comes from the approved spec. Replace every `<<...>>` placeholder, and remove whatever the spec doesn't have (no feeder in the spec: no feeder in the code).

1. **`config.yaml`** — `title`, `devices` (keys from `device-task-api.md` for each type; a `# TODO: confirm` comment on every value the user didn't confirm), `manual_actions` (with description and confirmation texts that tell the operator what will move), `sequences` for `prod_cycle`, `home`, `shipment`, `calib` (conditional steps last; no `manual:` keys, see the contract §6).
2. **`context.yaml`** — every param and variable the code will use, with type, default, unit in the name, description.
3. **`cell_tools.py`** — copy unchanged when the cell has a robot; drop the helpers it doesn't use only if the user asks.
4. **`prod.py`, `home.py`, `shipment.py`, `calib.py`** — follow `references/sequence-patterns.md`. For every device type you call, read its section in `references/device-task-api.md` first (what initialize already did, exact method signatures, what ABORT interrupts). Program names, checkpoints, poses and I/O channels are constants at the top of the file. All four files exist even when a task does little (contract §2: a missing one silently runs the demo's).
5. **`manual_actions.py`** — `get_manual_actions()` returns exactly the keys in config.yaml; actions reuse the cycle's functions.
6. **`custom_view/`** — only if the spec asks for it (contract §8).
7. **`README.md`** — from the asset: what the cell does, how to run it, devices, robot programs table (every program, its end checkpoint, its robot variables: the integrator creates these on the controllers), sequences, params, files, the TODO checklist, and a "Tested" section you fill in Phase 4.
8. **`.gitignore`** — copy `assets/workspace/gitignore`.

For an `examples/` workspace: follow the existing examples (flat, a header comment in config.yaml with the run command), and add one line for it under `examples/` in the Project Structure section of the app's README.md.

Before moving on, re-read prod.py against the spec table, row by row: same steps, same order, same names, every branch.

## Phase 4 — Verify

### 4a. Static checks and simulated smoke test

```bash
<app python> <skill dir>/scripts/check_workspace.py <workspace> --app <app>
```

It never modifies the workspace. It validates config.yaml against the device registry; checks that the four task modules and manual actions load **from the workspace** (not the demo fallback); that no module shadows the framework or a package; that every `devices[...]` name, context name and declared step exists. Then it copies the workspace with every device swapped for a simulated stand-in, and runs INITIALIZE, HOME, CALIBRATION, SHIPMENT, every manual action and 3 PROD cycles + STOP through the real controller, printing the steps each task opened and the traceback of any task that faulted.

- **ERROR**: fix and re-run until there are none.
- **WARN**: fix, or explain to the user why it stays.
- A failure that only happens because a simulated device returns `None` (a pose, an input): apply `sequence-patterns.md` §10. If it can't be made simulation-safe without contorting the code, leave it and report it as "expected in simulation".
- The INFO line about the demo's custom view (`KeyError: 'my_meca_robot'`) is expected for a workspace without `custom_view/`.

### 4b. Browser check (Claude desktop app with the in-app browser)

1. Keep a simulated copy outside the workspace: re-run the check with `--keep-sim <temp dir>/mecademic-sim/<name>_sim` (delete the old copy first; the option refuses a non-empty folder).
2. Add a configuration to `<app>/.claude/launch.json`, beside the existing ones: `"name": "<name>-sim"`, `runtimeExecutable` = the app's Python, `runtimeArgs` = `["app.py", "--host", "127.0.0.1", "--port", "<free port 5060-5099>", "--workspace", "<sim copy>"]`, `"port"` = that port. Don't commit launch.json unless the user asks.
3. Start it with the preview tool, then in the page: press **INITIALIZE** and wait for READY; open the Manual tab and confirm one button per manual action; on the Production tab press **START**, let it run ~3 cycles, check the step list shows "Step n of m" with the spec's step names; press **STOP** and wait for READY. Read the console for errors.
4. Take one screenshot of the Production tab during the run for the report. Stop the preview server.

If there is no in-app browser, say so and skip 4b; 4a already ran every task through the real controller.

Then fill README.md "Tested" with the date, what ran in simulation, and "Hardware: not yet" (unless you really talked to the hardware).

## Phase 5 — git

- Client workspace: if the folder is inside another git work tree (`git -C <parent> rev-parse --is-inside-work-tree`), ask before creating a nested repository. Otherwise `git init`, `git add -A`, and one commit: `Initial workspace: <title>` with the attribution trailer your environment requires. Don't add a remote or push.
- `examples/` workspace: no `git init` (it is part of the app repo); leave the change uncommitted unless the user asks for a commit.

## Phase 6 — Report and feedback

Report in this shape, then ask for feedback:

```
Workspace <title> is at <path> (<n> files, committed as <sha>).
Run it:  <app python> <app>/app.py --workspace <path>

Verified: static checks clean; in simulation INITIALIZE, HOME, CALIBRATION, SHIPMENT, <n> manual actions and
<n> PROD cycles + STOP ran with the declared steps <(browser check done / not available)>.
Not verified: nothing ran on hardware.

Before the first run at the machine:
- <every TODO: IP to confirm, robot programs + checkpoints + robot variables to create, poses to teach...>
- Expected in simulation only: <list, or none>
```

Then ask: *"What would you like changed?"* Apply feedback to the code **and** to WORKSPACE_SPEC.md and README.md (they must keep describing the code), re-run 4a, and make one new commit per round of changes (don't amend the first one).
