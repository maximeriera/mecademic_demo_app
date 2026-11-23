# Writing the sequences

How to turn the sequences in the approved WORKSPACE_SPEC.md into task code that runs on the hardware, survives ABORT, and also runs in the simulated smoke test. The helpers named here are in `assets/workspace/cell_tools.py`; copy that file into every workspace with a robot.

## Contents
1. From a described sequence to steps
2. Robot-side programs (the default)
3. Python-side moves (only when the spec says so)
4. Several robots
5. Feeders, sensors and I/O
6. Vision
7. Params and variables
8. Errors, timeouts and ABORT
9. HOME, SHIPMENT, CALIBRATION and manual actions
10. Code that must also run simulated

---

## 1. From a described sequence to steps

- One top-level `context.step()` per phase an operator would name: "Request part from feeder", "Pick part", "Inspect part", "Place part". Usually 3 to 8 per cycle. A step that takes under ~50 ms is noise; fold it into its neighbour.
- Nest a step only when its time is worth seeing on its own ("Pick part" > "Close gripper").
- Write the cycle so the code reads like the spec table: one `with` block per row, in the same order, with the same names. Put the device work of each step in a small named function when PROD and a manual action share it.
- A branch (reject, refill, retry) is its own step, declared last in `sequences:`.
- Pacing sleeps between cycles are left outside any step so they show as idle time.

## 2. Robot-side programs (the default)

The motion lives in programs saved on the robot (MecaPortal). Python decides *when*, the robot program decides *how*.

**Convention** (write it into README.md "Robot programs" and WORKSPACE_SPEC.md §3):
- Every program the workspace starts ends with `SetCheckpoint(<n>)` as its last line.
- Checkpoint numbers are unique per robot: 1-9 for home/shipment/calibration programs, 11 and up for production programs, in cycle order. Allowed range 1-8000.
- Data the program needs (a pick pose from the feeder, a slot index) goes through **robot variables**, created once on the robot (`CreateVariable`) and set from Python before the start: `robot.api.SetVariable("pick_x", x)`.
- Program names and checkpoint numbers are module-level constants at the top of the file, marked `# TODO: confirm on the robot` until the user confirmed them.

```python
from cell_tools import run_program, start_program

PICK_PROGRAM, PICK_DONE = "pick_feeder", 11      # TODO: confirm on the robot

with context.step("Pick part"):
    robot.api.SetVariable("pick_x", part["x"])
    run_program(robot, PICK_PROGRAM, PICK_DONE, timeout=timeout)
```

Why the checkpoint: the app runs mecademicpy in asynchronous mode, where `StartProgram()` only sends the command (`_robot_base.py:2595-2608`). A `WaitIdle()` right after it can return before the program queued anything. `run_program` arms `ExpectExternalCheckpoint(n)` *before* `StartProgram` (armed after, a short program can pass the checkpoint first and the wait never ends), waits for it, then `WaitIdle()`. ABORT's `ClearMotion()` makes the pending wait raise, so ABORT works with no extra code.

A checkpoint in the middle of a program (to release another robot early, or to trigger a camera) is armed the same way with `expect_checkpoint(robot, n)` before the start, and waited for when needed.

## 3. Python-side moves (only when the spec says so)

For a robot the user wants driven from Python, or a quick one-off move (open the gripper):

```python
SAFE_JOINTS = (0, -20, 20, 0, 30, 0)             # TODO: teach at the machine
PICK_APPROACH = (190.0, 0.0, 120.0, 180.0, 0.0, 0.0)   # mm / deg, WRF. TODO

robot.api.SetJointVel(context.get_param("joint_vel_pct", 25))
robot.api.MoveJoints(*SAFE_JOINTS)
robot.api.MovePose(*PICK_APPROACH)
robot.api.MoveLin(PICK_APPROACH[0], PICK_APPROACH[1], 60.0, *PICK_APPROACH[3:])
robot.api.GripperClose()
robot.api.WaitIdle(timeout=timeout)
```

- Every pose is a named constant at the top, with units and frame in a comment and `TODO` until taught. Never invent coordinates silently: an invented pose that looks real is how a robot hits a fixture.
- Set the motion parameters the move relies on (velocity, blending, TRF/WRF) in the code; whether a robot keeps earlier values across activation is not guaranteed.
- `WaitIdle()` after the moves; Move* calls return immediately.

## 4. Several robots

- **At the same time, no Python in between**: start each program, then wait for each. One thread, no locks:
  ```python
  with context.step("Load and dispense"):
      load = start_program(tray_robot, "load_tray", 11)
      dose = start_program(dispenser, "dispense", 21)
      load.wait(timeout); dose.wait(timeout)
  ```
- **A handshake** (robot B may start only when A cleared the zone): A's program sets a mid checkpoint; arm it with `expect_checkpoint(a, 15)` before starting A, `.wait()` it, then start B.
- **Parallel branches with Python logic** (B keeps asking the feeder while A places): `in_parallel(branch_a, branch_b)` runs each in a thread, joins both and re-raises the first exception. No `context.step()` inside a branch; wrap the call in one step. Never fire-and-forget a `Thread` the way older workspaces did: its exceptions vanish and the cycle carries on with a robot in an unknown state.

## 5. Feeders, sensors and I/O

- Asyril Eye+: `part = feeder.api.get_part()` blocks until the feeder answers (≤ ~30 s) and is **not interrupted by ABORT**. Check `part.get("resp") == 200`; on anything else the pose keys are missing. Bound the retries with a param.
- Waiting for a digital input: poll with a deadline, never `while not read: pass`:
  ```python
  deadline = time.monotonic() + context.get_param("sensor_timeout_s", 5.0)
  while not io.api.read_di(3):                    # part present
      if time.monotonic() > deadline:
          raise RuntimeError("No part on the conveyor sensor (DI3) after 5 s.")
      time.sleep(0.05)
  ```
- Write each I/O channel's meaning next to its number, as a constant: `LAMP_EMPTY_DO = 0  # feeder-empty lamp`.
- Per-device details (channel ranges, bool-only arguments, blocking behaviour): `device-task-api.md`.

## 6. Vision

Task code never reads a camera itself when a `cv_process` device exists: `result = devices["vision"].analyze_once(timeout=5.0)`, then `result["ok"]` and `result["data"][...]` (keys are whatever the analysis function returns). The analysis function lives in the workspace (`blob_analysis.py` style, `examples/gige_cv_analysis/`). Camera features at runtime: `devices["camera"].set_feature("ExposureTime", 8000.0)`.

## 7. Params and variables

- **Params** (inputs an operator or integrator may tune without editing code): timeouts, retries, speeds in %, pause durations, enable flags ("skip inspection"). Give each a unit in the name (`_s`, `_mm`, `_pct`).
- **Variables** (state carried between cycles, shown live): counters (`part_count`, `reject_count`, `reset_on: prod_start`, `persist: true`), alternation state (`tray_id`), last measurement (`last_height_mm`).
- Read params at the top of the function each cycle (so a change applies on the next cycle); write variables after the step that makes them true.
- Every name used in code is declared in context.yaml: `check_workspace.py` fails on an undeclared write.

## 8. Errors, timeouts and ABORT

- Every wait has a timeout from a param: `WaitIdle(timeout=...)`, `run_program(..., timeout=...)`, polling deadlines. A hung cell with no message is the worst failure at a demo.
- A process failure the operator must act on raises `RuntimeError` with what happened and what to check ("Feeder found no part 3 times: refill the hopper"). The cell faults and shows it.
- Don't wrap robot waits in `try/except Exception` that logs and continues: it swallows ABORT's `InterruptException`, so ABORT no longer stops the cycle. Catch only a specific, expected condition, and re-raise everything else.
- Don't check for STOP/ABORT in the code: the runner handles both.

## 9. HOME, SHIPMENT, CALIBRATION and manual actions

- `home()` brings every moving device to the cycle's start state: gripper open (or the program's own choice), robots at their start pose, outputs off. It runs before the first cycle and after STOP, so it must work from wherever the cell stopped.
- `shipment()` folds the robots into their transport pose.
- `calib()`: the cell's calibration routine, or, when there is none, a short documented placeholder (move to a check pose, or one step "No calibration for this cell"). The file must exist either way.
- Manual actions: the pieces an integrator wants to run alone (open gripper, pick one part, run one inspection, move to maintenance pose). Each calls the same functions the cycle uses.

## 10. Code that must also run simulated

The smoke test swaps every device for `type: "simulated"`, whose `.api` returns `None` for every call. Real drivers return values for the calls below, so these patterns change nothing on hardware:

- Use `.api.<method>()` rather than driver-level helpers, except on cameras and `cv_process` (which are simulated by `simulated_camera`).
- A value the cycle uses: `part = or_simulated(feeder.api.get_part(), SIMULATED_PART)` with a module constant `SIMULATED_PART = {"resp": 200, "x": 0.0, "y": 0.0, "rz": 0.0}`.
- An input polled in a loop: a simulated read is always `None`, so the deadline above makes it raise in simulation. Give the loop a stand-in: `while not or_simulated(io.api.read_di(3), True):`.
- Checkpoints: `cell_tools.expect_checkpoint` already returns a no-op wait when the robot is simulated.

If a call cannot be made simulation-safe without contorting the code, leave it and report the expected simulated failure instead of hiding it.
