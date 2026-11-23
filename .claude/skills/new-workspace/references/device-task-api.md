# Device task API reference — what task code may call on `devices["<name>"]`

Paths are relative to the repo root `mecademic_demo_app/`. `mpy/` = `.venv/Lib/site-packages/mecademicpy/` (installed mecademicpy **3.0.3**, `.venv/Lib/site-packages/mecademicpy-3.0.3.dist-info`). Other installed SDKs used below: pyfirmata2 2.5.1, pmclib 117.9.1, pymodbus 3.12.1, pyvisa 1.16.2, harvesters 1.4.3, zaber_motion 9.1.0.

## Contents
0. [Rules common to every device](#0-rules-common-to-every-device)
1. [mecademic](#1-mecademic--mecarobot)
2. [asyril](#2-asyril--asyrileyeplus)
3. [arduino](#3-arduino--arduinoboard)
4. [planarmotor](#4-planarmotor--planarmotor)
5. [iologik](#5-iologik--iologike1212)
6. [brainboxes_ed_digital](#6-brainboxes_ed_digital--brainboxeseddigital)
7. [lmi](#7-lmi--lmisensor)
8. [gige_camera](#8-gige_camera--gigevisiondevice)
9. [simulated_camera](#9-simulated_camera--simulatedcamera)
10. [cv_process](#10-cv_process--cvprocessdevice)
11. [thorlabs_pm](#11-thorlabs_pm--thorlabspowermeter)
12. [smaract_mcs2](#12-smaract_mcs2--smaractmcs2)
13. [zaber](#13-zaber--zaberaxis-legacy)
14. [simulated](#14-simulated--simulateddevice)
15. [custom](#15-custom)

---

## 0. Rules common to every device

- `devices["name"]` is the **driver** object (a `Device` subclass), built with `cls(name=<config name>, **validated_keys)` (`devices/registry.py:414-428`). `.api` is the SDK/protocol object (`devices/Device.py:166-173`). `.logger` writes `logs/devices/<name>.log` (`devices/Device.py:97-112`).
- Config keys: a `None` default means optional and nullable (`devices/registry.py:61-62`); missing optional keys are filled with their default (`devices/registry.py:313-320`); an unknown key is a warning and is **ignored** (`devices/registry.py:286-291`). `enabled: false` skips the entry (`devices/registry.py:255-258`). Type aliases are case-insensitive (`devices/registry.py:104-115`).
- **ABORT** (button, or any device reporting `faulted`): `Task.abort()` sets the stop flag and calls `device.abort()` on every device not released/detached (`core/Task.py:434-460`; fault path `core/ApplicationController.py:1050-1059`).
  - In **PROD**, an exception raised inside `prod_cycle` after abort is a clean exit, no home (`core/Task.py:365-372`).
  - In HOME/SHIPMENT/CALIB/manual actions, any escaping exception faults the controller (`core/Task.py:229-238`, `core/Task.py:266-317`).
  - So a blocking call only "unblocks on ABORT" if that device's `abort()` actually interrupts it. That is device-specific; see each section.
- A device whose `faulted` turns True (several report `faulted` when merely disconnected) faults the cell and aborts the task (`core/ApplicationController.py:1046-1059`).
- **Simulated stand-ins**: any `type: "simulated"` device's `.api` is a `_FakeApi`. Every attribute access on it returns a function that logs and returns `None` (`devices/SimulatedDevice.py:9-31`). Consequences, for every section below:
  - Every `.api.X(...)` returns `None`.
  - A non-call attribute or property read (`api.recipe`, `api.wavelength`) returns a function object, not a value.
  - An assignment like `api.wavelength = 532` is silently stored on the fake.
  - **Driver-level** methods/properties that `SimulatedDevice` lacks (`rt`, `deactivate`, `set_feature`, `capture_frame_8bit`, `analyze_once`, `move_axis`, `set_digital_pin`, `channels`...) raise `AttributeError`. `SimulatedDevice` only defines the `Device` interface plus `io_points()`/`write_io("do1".."do4", v)` (`devices/SimulatedDevice.py:87-165`).
- The shipped `app_logic` calls `camera.api.TriggerAcquisition()` / `camera.api.WaitForFrame()` (`app_logic/manual_actions.py:39-40`, `app_logic/calib.py:23,34-35`). These only work because the camera is `simulated`. **No real driver has these methods** (grep of `devices/` finds none). Do not copy them.

---

## 1. mecademic — `MecaRobot`

**Config** (`devices/registry.py:437-441`): `ip_address` (str, **required**). No other keys.

**What `initialize()` already does** (`devices/MecaRobot.py:184-213`):
- `Connect(ip, disconnect_on_exception=False)` in mecademicpy's default **asynchronous** mode (`enable_synchronous_mode=False`, `mpy/robot.py:121`).
- Then `ClearMotion()`, `ResumeMotion()`, `ResetError()`, `ActivateRobot()`, `WaitActivated()`; it raises if the robot is in error.
- Then `ActivateAndHome()` + `WaitIdle()`.

So the robot is **connected, activated, homed, motion resumed, error reset**. Task code must not repeat activation/homing. `initialize()` sets no motion parameters (velocity, blending, TRF/WRF, payload, gripper force); whether the robot keeps earlier values is unclear from the repo, so set the ones a task relies on.

**`.api`**: `mecademicpy.robot.Robot` (`devices/MecaRobot.py:5,39,99-101`), the control connection. Enums (`MxIoBankId`, `MxDigitalIoState`...) are re-exported by `mecademicpy.robot` (`mpy/robot.py:17-18`), e.g. `import mecademicpy.robot as mdr; mdr.MxIoBankId.MX_IO_BANK_ID_IO_MODULE` (`mpy/mx_robot_def.py:267-277`).

**Async mode: every motion/gripper/IO-queue command returns immediately.** It blocks only in synchronous mode (`mpy/_robot_base.py:5638-5658`). Use `WaitIdle()` or a checkpoint to wait.

Commonly used `.api` methods (exact signatures from `mpy/robot.py`; none return a value unless stated):

| Group | Signature (line) |
|---|---|
| Motion | `MoveJoints(*args)` 441 (6 floats or a list) · `MoveJointsRel(*args)` 479 · `MoveJointsVel(*args)` 518 · `MovePose(x, y=None, z=None, alpha=None, beta=None, gamma=None)` 562 (x may be a list) · `MoveLin(x, y=None, z=None, alpha=None, beta=None, gamma=None)` 676 · `MoveLinRelTrf(...)` 730 / `MoveLinRelWrf(...)` 793 (same params) · `MoveLinVelTrf(v_x, v_y=None, v_z=None, omega_x=None, omega_y=None, omega_z=None)` 862 / `MoveLinVelWrf(...)` 935 · `MoveJump(x, y=None, z=None, alpha=None, beta=None, gamma=None)` 617 · `Delay(t)` 2749 (queued pause between motions) |
| Motion params | `SetJointVel(p)` 1196 · `SetJointAcc(p)` 1179 · `SetCartLinVel(v)` 1157 · `SetCartAngVel(w)` 1135 · `SetCartAcc(p)` 1117 · `SetBlending(p)` 1094 · `SetConf(shoulder=None, elbow=None, wrist=None)` 1011 · `SetConfTurn(n)` 1058 · `SetAutoConf(e)` 1040 · `SetAutoConfTurn(e)` 1076 · `SetTrf(x, y=None, z=None, alpha=None, beta=None, gamma=None)` 1439 · `SetWrf(...)` 1507 · `SetVelTimeout(t)` 993 · `SetPayload(mass, x=None, y=None, z=None)` 5200 · `SetMoveMode(mode)` 1231 · `SetMoveDuration(t)` 1289 |
| Queue control | `PauseMotion()` 398 · `ResumeMotion()` 409 · `ClearMotion()` 420 (includes an implicit pause) · `ResetError()` 2708 |
| Gripper | `GripperOpen()` 1632 · `GripperClose()` 1661 · `MoveGripper(target: bool \| float)` 1690 (True = open, or opening in mm) · `SetGripperForce(p)` 1729 · `SetGripperVel(p)` 1746 · `SetGripperRange(closePos, openPos=None)` 1775 · `WaitGripperMoveCompletion(timeout=None)` 1615 · `WaitHoldingPart(timeout=None)` 2002 · `WaitReleasedPart(timeout=None)` 2023 |
| Vacuum / IO (MVK01) | `VacuumGrip()` 1860 · `VacuumRelease()` 1901 · `SetValveState(*args)` 1815 · `SetOutputState(bank_id, *output_states)` 2160 · `SetOutputState_Immediate(bank_id, *output_states)` 2214 · `WaitInputState(bank_id, *expected_states, timeout=None)` 2471 · `WaitOutputState(bank_id, *expected_states, timeout=None)` 2417 |
| Checkpoints | `SetCheckpoint(n) -> InterruptableEvent` 1560 (call `.wait(timeout=None)` on it, `mpy/robot_classes.py:512`) · `ExpectExternalCheckpoint(n) -> InterruptableEvent` 1588 · `WaitForAnyCheckpoint(timeout=None)` 2227 · `GetCheckpoint(*, timeout=None) -> int` 5647 |
| Programs | `StartProgram(name: int \| str, timeout=None)` 2881 (async mode: only **sends** the command and returns, `mpy/_robot_base.py:2595-2608`, so sync on a checkpoint the program sets, see `sequence-patterns.md`) · `StopProgram()` 2911 · `LoadProgram(name, timeout=None) -> dict` 3015 · `WaitProgramDone(*, timeout=None)` 3244 · `ListPrograms(timeout=None) -> dict` 2968 |
| Robot variables | `SetVariable(name: str, value, timeout=10)` 7542 (variable must already exist on the robot, `CreateVariable` 7413) · `GetVariable(name)` 7586 · `ListVariables() -> List[str]` 7635. Used to hand a pose to a robot program (`SetVariable("PickPose.x", x)`, as in mecademic_demo_medical_device) |
| Waits | `WaitIdle(timeout=None, wait_rt_data=False)` 2674 · `WaitActivated/WaitHomed/WaitErrorReset/WaitMotionResumed/WaitMotionPaused/WaitMotionCleared(timeout=None)` 2270/2296/2530/2619/2632/2645 · `WaitEndOfCycle(timeout=None, *, force_cycle=False)` 2658 · `WaitForError(timeout=None)` 2517 |
| Getters | `GetPose(synchronous_update=False, timeout=None) -> List[float]` 4117 · `GetJoints(synchronous_update=False, timeout=None) -> List[float]` 3861 · `GetRtTargetCartPos(include_timestamp=False, synchronous_update=True, timeout=None) -> TimestampedData \| List[float]` 4085 · `GetRtTargetJointPos(...)` 3829 (same params) · `GetRtCartPos(*, include_timestamp=False, synchronous_update=True, timeout=None)` 4126 · `GetStatusRobot(synchronous_update=False, timeout=None) -> RobotStatus` 6658 (`.activation_state`, `.homing_state`, `.error_status`, `.error_code`, `.pause_motion_status`..., `mpy/robot_classes.py:1855-1900`) · `GetRtGripperState(include_timestamp=False, synchronous_update=False, timeout=None) -> GripperState` 3616 (`.holding_part`, `.target_pos_reached`, `.closed`, `.opened`, `mpy/robot_classes.py:2644-2667`) · `GetRtVacuumState(...) -> VacuumState` 3756 (`.vacuum_on`, `.purge_on`, `.holding_part`) · `GetRtInputState(bank_id=MX_IO_BANK_ID_IO_MODULE, synchronous_update=False, timeout=None, include_timestamp=False) -> List[int]` 3717 · `GetRtOutputState(...)` 3677 · `GetRobotInfo() -> RobotInfo` 6571 · `IsConnected()/IsControlling()/IsAllowedToMove() -> bool` 171/185/213 |

**Driver-level members**:
- `robot.rt`: whichever `Robot` link is up (control or monitoring). Use it for **cached getters only**, with `synchronous_update=False`; never send commands through it (`devices/MecaRobot.py:103-115`; `README.md:289-296`).
- `robot.deactivate()`: `DeactivateRobot()` + `WaitDeactivated()` (`devices/MecaRobot.py:239-242`).
- `telemetry()` / `io_points()`: cached, read-only (`devices/MecaRobot.py:139-178`).

**Blocking calls and ABORT**:
- `abort()` sends `ClearMotion()` + `ResumeMotion()`, only if this app holds control (`devices/MecaRobot.py:260-268`).
- ClearMotion invalidates pending checkpoints, either client-side or via the robot's checkpoint-discarded message (`mpy/_robot_base.py:1929-1938, 5618-5636, 7204-7210`). So `WaitIdle()` raises `InterruptException('WaitIdle interrupted by the robot')` (`mpy/_robot_base.py:2463-2476`), and so does any `SetCheckpoint(n).wait()`.
- `WaitGripperMoveCompletion` waits on an internal checkpoint first, so it is interrupted the same way (`mpy/_robot_base.py:2006-2010`).
- `WaitIdle` raises at once if the robot is already in error (`mpy/_robot_base.py:5558-5560`).
- **Not interrupted by ClearMotion** (they end at their timeout, default `default_timeout` = 10 s): `WaitHoldingPart`/`WaitReleasedPart` (`mpy/robot.py:2016-2018`; `mpy/robot_classes.py:26`) and `WaitInputState`/`WaitOutputState` (polling loop, `mpy/_robot_base.py:2314-2339`). Only events flagged `abort_on_clear_motion` are woken (`mpy/_robot_base.py:5785-5792`).
- **Gotcha (mecademicpy 3.0.3):** `WaitInputState`/`WaitOutputState` forward `timeout` *positionally* into `WaitIOState(bank_id, is_input, *args, timeout=None)` (`mpy/robot.py:2444,2498`; `mpy/_robot_base.py:2314-2324`).
  - The `timeout` you pass becomes an extra expected state, and the 10 s default is used.
  - With fewer than 8 states given, a timeout of `0` or `1` would even be compared as an input state (`mpy/_robot_base.py:2278-2311`).
  - Wrap your own deadline around `GetRtInputState()` polling if you need a different timeout.

**Simulated stand-in**:
- Every call above returns `None`, so `MoveX` + `WaitIdle()` "works".
- Breaks: `SetCheckpoint(n).wait()` (`None.wait` → AttributeError), `GetPose()[0]`, `GetStatusRobot().error_status`, `GetRtGripperState().holding_part`, `LoadProgram(...)["..."]`.
- `robot.rt` and `robot.deactivate()` do not exist (AttributeError).
- Do not branch on getter results when the device may be simulated.

```python
robot = devices["my_meca_robot"]
robot.api.SetJointVel(25); robot.api.SetBlending(0)
robot.api.MovePose(200, 0, 150, 0, 90, 0)
robot.api.GripperClose(); robot.api.WaitGripperMoveCompletion(timeout=3)
robot.api.MoveJoints(0, 0, 0, 0, 0, 0)
robot.api.WaitIdle(timeout=30)          # raises InterruptException on ABORT
```

---

## 2. asyril — `AsyrilEyePlus`

**Config** (`devices/registry.py:442-450`): `ip_address` (str, **required**), `recipe` (int, default `0`), `port` (int, default `7171`).

**`initialize()`** (`devices/Asyril.py:44-52`):
- `api.connect()` (TCP, socket timeout 35 s, `devices/api/AsyrilAPI.py:21,156-175`).
- `api.start_production()`, which first calls `reset_state()` and then `start production <recipe>` (`devices/api/AsyrilAPI.py:236-251`).
- `api.set_part_timeout()` (30 s default, `devices/api/AsyrilAPI.py:177-194`).

The feeder is **in production with the configured recipe**; do not call `start_production()` again.

**`.api`**: `AsyrilEyePlusApi` (`devices/api/AsyrilAPI.py:84`). Every command blocks on the socket reply. Methods:
- `get_part() -> dict`, blocking (`devices/api/AsyrilAPI.py:253-263`).
  - 200 reply: `{'x': float, 'y': float, 'rz': float, ..., 'resp': 200}`, i.e. every `key=value` in the reply as float, plus `resp` (`devices/api/AsyrilAPI.py:660-695`).
  - Non-200 reply: `{'resp': {<raw reply string>}}`. That is a **set** holding the string, not a code (`devices/api/AsyrilAPI.py:262`). Test `res.get('resp') == 200`.
- `get_part_async() -> concurrent.futures.Future` (`devices/api/AsyrilAPI.py:265-312`).
  - The Future resolves to the same dict on 200, `{'resp': <int code>, 'raw': str}` on non-200, or `{'resp': 501, 'error': ...}` on socket timeout.
  - It raises `RuntimeError` if one is already pending.
  - Do not call other api methods while it is pending.
- `can_take_image(value: bool) -> str` (`devices/api/AsyrilAPI.py:393-412`) · `prepare_part() -> str` (328) · `force_take_image() -> str` (314) · `force_execute_vibration() -> str` (342) · `get_remaining_duration_of_current_vibration() -> int` (355-390, unit unclear per its docstring) · `set_parameter(parameter: str) -> str` (414) · `set_part_timeout(timeout: float = 30.0) -> str` (177) · `get_part_timeout() -> str` (196) · `start_production()/stop_production() -> str` (236/223) · `reset_state() -> str | None` (532).
- Calibration: `start_calibration(recipe=None) -> str` (432), `take_calibration_image() -> str` (503), `set_calibration_pose(x, y) -> str` (564), `calibrate() -> str` (470, raises RuntimeError on failure), `stop_calibration()` (461).
- `api.recipe` (int attribute, `devices/api/AsyrilAPI.py:119`).

**Blocking and ABORT**:
- `abort()` only logs (`devices/Asyril.py:65-66`), so **ABORT does not interrupt** a `get_part()`.
- `get_part()` returns when the Eye+ replies (its own part timeout, 30 s after init) or raises `TimeoutError` at the 35 s socket timeout.
- A socket timeout also sets `api._faulted = True` (`devices/api/AsyrilAPI.py:618-622`), which makes the device `faulted` (`devices/Asyril.py:34-37`) and faults the cell. `clear_fault()` only resets the flag (`devices/Asyril.py:62-63`).

**Simulated**:
- `get_part()` returns `None`, so `pose['x']` raises TypeError.
- `get_part_async()` returns `None`, so `.result()` raises AttributeError.
- Guard results: `pose = feeder.api.get_part() or {}`.

```python
feeder, robot = devices["part_feeder"], devices["my_meca_robot"]
feeder.api.can_take_image(True)
part = feeder.api.get_part()                       # blocks up to ~30-35 s, not abortable
if part and part.get("resp") == 200:
    robot.api.MovePose(part["x"], part["y"], 20, 180, 0, part["rz"]); robot.api.WaitIdle()
```

---

## 3. arduino — `ArduinoBoard`

**Config** (`devices/registry.py:451-455`): `port` (str, default `"COM3"`).

**`initialize()`**: `pyfirmata2.Arduino(port)` and nothing else (`devices/ArduinoBoard.py:49-55`). No pin is configured.

**`.api`**: `pyfirmata2.Arduino` (a `Board`), or `None` before init (`devices/ArduinoBoard.py:13,45-47`).
- `get_pin("d:13:o")` (also `a:`/`d:`, `i/o/p/s/u`) returns a `Pin` (`.venv/Lib/site-packages/pyfirmata2/pyfirmata2.py:246-297`). It raises `PinAlreadyTakenError` if that pin was already taken (`pyfirmata2.py:276-278`).
- `Pin.write(value)`: bool for OUTPUT, 0-1 float for PWM, degrees for SERVO (`pyfirmata2.py:611-640`).
- `Pin.read()` raises `IOError` for input/analog pins: "Reading via polling is not supported" (`pyfirmata2.py:587-593`). Inputs need `pin.register_callback(fn)` + `api.samplingOn()` (`pyfirmata2.py:595-603, 200-206`).
- `api.digital[n].write(v)` works directly: digital pins default to OUTPUT mode (`pyfirmata2.py:523`).

**Driver method**: `set_digital_pin(pin_number, value)` (`devices/ArduinoBoard.py:76-81`). It calls `get_pin(f"d:{n}:o")` on **every** call, so the **second call on the same pin raises `PinAlreadyTakenError`**. Prefer taking the pin once (`p = board.api.get_pin("d:13:o")`) and reusing `p.write()`, or `board.api.digital[13].write(1)`.

**Blocking and ABORT**:
- `abort()` calls the abstract base body and does nothing (`devices/ArduinoBoard.py:73-74`; `devices/Device.py:189-195`).
- `faulted` is simply `api is None` (`devices/ArduinoBoard.py:39-42`).
- `clear_fault()` reconnects (shutdown + initialize, `devices/ArduinoBoard.py:63-71`).
- No blocking calls in the wrapper.

**Simulated**: `api.get_pin(...)` returns `None`, so `.write()` raises AttributeError. `set_digital_pin` does not exist (AttributeError).

```python
board = devices["arduino"]
led = board.api.get_pin("d:13:o")      # take once per connection
led.write(1); time.sleep(0.5); led.write(0)
```

---

## 4. planarmotor — `PlanarMotor`

**Config** (`devices/registry.py:456-460`): `ip_address` (str, default `"192.168.10.200"`).

**`initialize()`** (`devices/PlanarMotor.py:43-56`):
- `api.clear_abort()` and `api.connect()` (direct IP).
- `api.initialize()`: gain mastership if needed, `activate_xbots()`, then wait ≤10 s for `PMC_FULLCTRL` (`devices/api/PlanarMotorApi.py:103-125`).

The movers are **levitated and controllable**. No stereotypes are defined or assigned (unclear whether needed for your setup).

**`.api`**: `PlanarMotorApi` (`devices/api/PlanarMotorApi.py:36`). Raw pmclib is on `api.sys` / `api.bot` (`devices/api/PlanarMotorApi.py:48-49`). Units per the wrapper's docstring: m, m/s, m/s² (`devices/api/PlanarMotorApi.py:23-27`).
- Moves (**non-blocking**, they return at once):
  - `send_single_linear_command(xbot_id: int, xpos: float, ypos: float, vel=1.0, acc=10.0) -> None` (210-217).
  - `send_multi_linear_commands(moves: list[PlanarMotorMove]) -> None` (219-226), with `PlanarMotorMove(bot_id, xpos, ypos, vel=1.0, acc=10.0, ending_speed=0)` (12-34).
  - `send_auto_move_command(num_bot, xbot_ids, x_pos, y_pos) -> None` (228-234).
  - `send_rotation(id, time, target_angle=52.36) -> None` (203-208).
  - `start_macro(macro_id, xbot_id) -> None` (344-346).
- Waits:
  - `wait_move_done(bot_id, timeout=10.0) -> XBOTSTATE`. It returns `XBOT_IDLE` or `XBOT_OBSTACLE_DETECTED`, raises `TimeoutError`, or raises `InterruptedError` on abort (236-271).
  - `wait_multiple_move_done(bot_list, timeout=10) -> dict{id: XBOTSTATE}`; the timeout covers the whole list (273-296).
- Status: `get_pmc_status() -> PMCSTATUS` (155) · `get_num_xbots() -> int` (159) · `get_xbots_state() -> {id: XBOTSTATE}` (163) · `get_xbots_pos() -> {id: (x, y)}` (174) · `get_xbot_at_pos(xpos, ypos, tolerance=0.005) -> int | None` (184).
- Other: `activate_bots(timeout=10.0)` / `deactivate_bots(timeout=10.0)` (127/142, blocking polls) · `define_stereotype(mover_type, id, payload=0, ...)` (298) · `assign_stereotype(bot_id, ster_id)` (337). Enums come from `from pmclib import pmc_types` (`devices/api/PlanarMotorApi.py:3`).

**Blocking and ABORT**:
- `abort()` → `api.abort()` **only sets a flag** (`devices/PlanarMotor.py:74-78`; `devices/api/PlanarMotorApi.py:369-371`). Movers are **not stopped**.
- Any `wait_move_done`/`activate_bots`/`initialize` poll then raises `InterruptedError` within ~0.1 s (`devices/api/PlanarMotorApi.py:39,72-74,258`).
- The flag stays set until `clear_abort()` (called by INITIALIZE, `devices/PlanarMotor.py:45`) or `api.clear_fault()` (CLEAR FAULTS, `devices/api/PlanarMotorApi.py:348-350`). After a PROD abort that left the cell READY, the next wait raises `InterruptedError` immediately until one of those runs.
- `shutdown()` deactivates the movers (`devices/api/PlanarMotorApi.py:373-383`).

**Simulated**: the waits return `None` (no state to compare); `get_xbots_pos()` returns `None`, so `.items()` raises AttributeError.

```python
pm = devices["planar"]
pm.api.send_single_linear_command(1, 0.120, 0.060, vel=0.5, acc=5.0)
state = pm.api.wait_move_done(1, timeout=10)        # InterruptedError on ABORT
if state == pmc_types.XBOTSTATE.XBOT_OBSTACLE_DETECTED:
    raise RuntimeError("mover 1 blocked")
```

---

## 5. iologik — `IoLogikE1212`

**Config** (`devices/registry.py:461-469`): `ip_address` (str, **required**), `port` (int, default `502`), `slave_id` (int, default `1`).

**`initialize()`**: `api.connect()` (Modbus TCP), then reads model, firmware and IP for the card (`devices/IoLogikE1212.py:80-89`). No outputs are changed.

**`.api`**: `IoLogikE1212Api` (`devices/api/IoLogikE1212Api.py:31`). Every call is serialised by a lock (`devices/api/IoLogikE1212Api.py:8-28`).
- **Channels are validated against `DI_COUNT = 8` / `DO_COUNT = 8`**, so the valid range is 0-7 for both, even though some docstrings say 0-15 (`devices/api/IoLogikE1212Api.py:56-57,441-445`).
- DI: `read_di(channel) -> bool` (160) · `read_all_di() -> list[bool]` (8 values) (180) · `get_di_counter(channel) -> int` (194) · `start_di_counter/stop_di_counter/reset_di_counter(channel) -> None` (214/221/228) · `is_di_counter_overflow(channel) -> bool` (236) · `clear_di_counter_overflow(channel)` (245).
- DO: `read_do(channel) -> bool` (256) · `read_all_do() -> list[bool]` (271) · `write_do(channel, value: bool) -> None` (285) · `write_all_do(values: list) -> None` (exactly 8 values) (302) · `configure_do_pulse(channel, on_width_ms, off_width_ms, count=0)` (321) · `start_do_pulse/stop_do_pulse(channel)` (352/359).
- System: `get_model_name()/get_device_name()/get_firmware_version()/get_lan_ip()/get_lan_mac() -> str` (370-411) · `get_uptime() -> int` (413) · `get_watchdog_alarm() -> bool` (422) · `clear_watchdog_alarm()` (430).

**Driver**: `io_points()` exposes `di0..di7`/`do0..do7`; `write_io("doN", v)` (`devices/IoLogikE1212.py:105-118`).

**Blocking and ABORT**:
- `abort()` only logs (`devices/IoLogikE1212.py:98-99`). Each call is a single Modbus exchange; its timeout is pymodbus's default (not set in the repo).
- A Modbus error response raises `ModbusException` and sets `_faulted`, which faults the cell (`devices/api/IoLogikE1212Api.py:447-452`; `devices/IoLogikE1212.py:72-74`). A closed socket also reports faulted.
- There is no "wait for input" helper; poll with your own deadline and `time.sleep`.

**Simulated**: reads return `None`, so `if io.api.read_di(0):` is always False and a polling loop never ends without a deadline.

```python
io = devices["io"]
io.api.write_do(0, True)                           # e.g. clamp
deadline = time.monotonic() + 5
while not io.api.read_di(3):                       # sensor; never True when simulated
    if time.monotonic() > deadline: raise TimeoutError("DI3 not set")
    time.sleep(0.05)
```

---

## 6. brainboxes_ed_digital — `BrainboxesEDDigital`

**Config** (`devices/registry.py:470-481`): `ip_address` (str, **required**), `address` (int, default `1`), `port` (int, default `9500`), `timeout` (float, default `5.0` s), `channels` (int, default `8`; only the Devices tab uses it). Aliases: `brainboxes_dio`, `ed_digital`.

**`initialize()`**: builds `EDDigitalDeviceSync` and reads the device name and firmware as a connectivity probe (`devices/BrainboxesED.py:77-99`). No outputs are changed.

**`.api`**: `EDDigitalDeviceSync`, or `None` before init (`devices/BrainboxesED.py:50-52`). **Each call opens a TCP connection, runs one command and closes** (`devices/api/ed_device.py:1199-1228`). The driver re-exposes the same methods, raising RuntimeError if not initialized (`devices/BrainboxesED.py:128-189`), so `dev.set_single_output(...)` and `dev.api.set_single_output(...)` are equivalent.
- `read_inputs_as_int() -> int` (bitmask, bit 0 = DIn0) (`devices/api/ed_device.py:729-738`) · `read_outputs_as_int() -> int` (718-727) · `read_io_status() -> str` (hex, outputs byte then inputs byte) (464-473).
- `set_single_output(channel: int, on: bool) -> str` (544-561). **`on` must be a real `bool` and `channel` 0-7**: `_require` raises `TypeError` for `1`/`0` (`devices/api/ed_device.py:54-58`). Use `set_single_output_upper(channel, on)` for DOut 8-15 (563-580).
- `set_digital_output(value: int) -> str` (bitmask 0x0000-0xFFFF) (494-514).
- `read_input_counter(channel) -> int` (0-15) (586-603) · `clear_input_counter(channel) -> str` · `clear_all_latched_inputs() -> str` · watchdog `host_ok()`, `read_watchdog_status()`, `set_watchdog_timeout(enable, timeout_tenths_sec)` · `get/set_debounce_time(channel[, time_ms])` (`devices/BrainboxesED.py:152-177`). The `str` returns are the reply data field (`devices/api/ed_device.py:227-229`).

**Blocking and ABORT**:
- `abort()` only logs (`devices/BrainboxesED.py:110-111`). Each call blocks at most `timeout` s and then raises `EDTimeout` (`devices/api/ed_device.py:126-135`); a `?` reply raises `EDError` (`devices/api/ed_device.py:217-220`).
- These errors do **not** latch `faulted`, which is set only by a failed init (`devices/BrainboxesED.py:95-98`). They reach the task as exceptions.

**Simulated**: `read_inputs_as_int()` returns `None`, so `>>`/`&` raise TypeError. The driver-level `set_single_output` does not exist on `SimulatedDevice`, so use `dev.api.*` if the device may be simulated.

```python
ed = devices["ed_io"]
ed.api.set_single_output(2, True)                  # bool, not 1
inputs = ed.api.read_inputs_as_int()
part_present = bool(inputs >> 4 & 1)               # DIn4
```

---

## 7. lmi — `LMISensor`

**Config** (`devices/registry.py:482-494`): `ip_address` (str, **required**), `control_port` (int, 3190), `data_port` (int, 3192), `health_port` (int, 3194), `delimiter` (str, `","`), `terminator` (str, `"\r\n"`), `timeout` (float, 5.0 s).

**`initialize()`**: `api.connect()` opens 3 sockets; only the command channel is mandatory (`devices/api/LMISensorApi.py:131-161`). Then `api.start()`, which puts the sensor in the **Running** state (`devices/LMISensor.py:108-110`). No job is loaded.

**`.api`**: `LMISensorApi` (`devices/api/LMISensorApi.py:34`).
- Command channel: `start()/stop()/trigger() -> None` (191/196/201) · `load_job(job_name=None) -> str` (None queries the current job) (206-226) · `get_stamp(*fields) -> dict` (keys `time`/`encoder`/`frame`, int values) (228-264) · `clear_alignment()`, `stationary_alignment()` (266/277; command strings are "inferred" per the docstrings) · `set_runtime_variable(name, value)` (287) · `get_runtime_variable(name) -> str` (304).
- Data channel:
  - `get_measurements(*measurement_ids: int) -> list[MeasurementResult]` (377-404). At least one ID is required (ValueError otherwise). Each `MeasurementResult` has `id, sensor_id, value (float or None if invalid; wire value / 1000), decision, value_valid, passed, anchor_valid` (`devices/api/LMISensorApi.py:8-26, 541-596`).
  - `get_result(*ids) -> str` (345) · `get_formatted_result() -> str` (323).
- Async: `set_async_callback(cb)` + `start_async_listener()` / `stop_async_listener()`; `set_health_callback(cb)` + `start_health_listener()` / `stop_health_listener()` (406-480).

**Blocking and ABORT**:
- `abort()` sends `stop()` (`devices/LMISensor.py:123-128`). It does not interrupt an in-flight reply wait, which ends at the socket `timeout` (`devices/api/LMISensorApi.py:487-492`).
- After an abort the sensor **stays stopped until the next INITIALIZE**, because nothing calls `start()` again.
- Any socket error or `ERROR,...` reply raises `LMISensorError` and sets `_faulted` (`devices/api/LMISensorApi.py:502-521`), which faults the cell (`devices/LMISensor.py:100-102`). A bad measurement ID therefore faults the cell too.
- Whether `get_measurements` returns data from *this* trigger or the previous scan is unclear from the code; there is no "wait for new scan" helper.

**Simulated**: `get_measurements()` returns `None`, so iterating it raises TypeError. `get_stamp()` returns `None`.

```python
lmi = devices["lmi"]
lmi.api.trigger()
m = lmi.api.get_measurements(0, 1)
height = m[0].value if m and m[0].value_valid else None
ok = bool(m) and all(r.passed for r in m)
```

---

## 8. gige_camera — `GigEVisionDevice`

**Config** (`devices/registry.py:495-511`):
- `cti_path` (str, **required**).
- `camera_index` (int, 0) · `autostart_stream` (bool, False).
- `features` (dict, None) **or** `features_file` (path, None); the two are exclusive.
- `trigger` (`off`|`software`|`hardware`, default `off`) · `trigger_source` (str, `"Line0"`) · `trigger_activation` (`RisingEdge`|`FallingEdge`|`AnyEdge`|`LevelHigh`|`LevelLow`, default `RisingEdge`).

**`initialize()`** (`devices/GigEVisionDevice.py:135-164`):
1. Connect, then stop any running stream.
2. Release a stale acquisition left by a crashed session.
3. Apply the trigger features (`devices/GigEVisionDevice.py:166-183`).
4. Write `features` / `features_file` in order.
5. Start the stream **only if `autostart_stream`**.

**Driver methods (use these, not `.api`)**:
- `capture_frame_8bit(timeout_sec=2.0) -> np.ndarray` (uint8 `H×W` mono or `H×W×3` RGB) (`devices/GigEVisionDevice.py:337-369`).
  - It starts the stream if needed, for example after an abort.
  - With `trigger: software` it drops stale frames and fires `TriggerSoftware`.
  - With `trigger: hardware` it waits for a pulse and raises `TimeoutError` without faulting (`devices/GigEVisionDevice.py:328-332`).
  - Thread-safe through a capture lock.
- `capture_frame(timeout_sec=2.0) -> np.ndarray`, raw dtype/shape (`devices/GigEVisionDevice.py:322-335`). It does **not** start the stream (RuntimeError if not streaming, `devices/api/gigE_vision.py:483-484`) and does **not** fire the software trigger.
- `discard_queued() -> int` (`devices/GigEVisionDevice.py:303-320`): call it before a hardware-triggered capture to drop old pulses (`README.md:447`).
- `set_feature(name, value)`: if the feature is locked while streaming, it stops, writes and restarts the stream. It raises `KeyError` (unknown) or `ValueError` (refused/read-only) and never faults. The value lasts until the next INITIALIZE (`devices/GigEVisionDevice.py:382-413`).
- `get_feature(name)` · `describe_feature(name) -> dict` · `feature_tree(visibility="Beginner") -> list` (`devices/GigEVisionDevice.py:415-431`).
- `start_stream()` / `stop_stream()` · `streaming` / `trigger` properties (`devices/GigEVisionDevice.py:106-129, 285-301`).

**`.api`**: `GigECamera` (`devices/api/gigE_vision.py:153`): `execute(command_name)` (343), `get_feature`/`set_feature` without stream handling (285-304), `last_pixel_format`, `incomplete_frames`, `harvester`, `ia` (165-174).

**Blocking and ABORT**:
- `abort()` stops the stream outside the capture lock "to interrupt a fetch in progress" (`devices/GigEVisionDevice.py:70-72, 275-283`). Whether harvesters' `fetch()` returns early when stopped is not shown in repo code (unclear); otherwise a capture returns at `timeout_sec`.
- A non-timeout capture error, or a timeout in non-hardware mode, sets `faulted`, which faults the cell (`devices/GigEVisionDevice.py:326-335`).
- `ready` is just `connected` (`devices/GigEVisionDevice.py:115-117`).

**Simulated** (`type: simulated`): `set_feature`/`capture_frame_8bit` don't exist (AttributeError). Use `simulated_camera` instead to keep the same driver API.

```python
cam = devices["camera"]
cam.set_feature("ExposureTime", 8000.0)
cam.discard_queued()                               # hardware trigger: drop old pulses
frame = cam.capture_frame_8bit(timeout_sec=2.0)    # uint8 H×W or H×W×3
```

---

## 9. simulated_camera — `SimulatedCamera`

**Config** (`devices/registry.py:512-530`): `width` (int, 1600), `height` (int, 1200), `fps` (float, 15.0), `fail_to_connect` (bool, False), `features`|`features_file` (exclusive), `trigger`/`trigger_source`/`trigger_activation` (same as gige). Alias: `test_pattern_camera`.

**`initialize()`**: marks the camera connected (or raises if `fail_to_connect`), leaves the stream stopped, and applies features (`devices/SimulatedCamera.py:168-180`).

**Driver methods** (same contract as gige):
- `capture_frame_8bit(timeout_sec=2.0) -> np.ndarray`: RGB `H×W×3`, or `H×W` with `PixelFormat: Mono8`. It is paced to `fps`, auto-starts the stream, and with `trigger: hardware` raises `TimeoutError` if the next simulated pulse is beyond `timeout_sec` (`devices/SimulatedCamera.py:201-226`).
- `capture_frame` is the **same function** (`devices/SimulatedCamera.py:228`).
- `set_feature` / `get_feature` / `describe_feature` / `feature_tree` (`devices/SimulatedCamera.py:245-309`). Only `ExposureTime` (100-1e6 µs), `Gain` (0-24 dB) and `PixelFormat` (`RGB8`/`Mono8`) are writable; `Width`, `Height`, `DeviceModelName` and `DeviceTemperature` are read-only (`devices/SimulatedCamera.py:35-56`).
- `start_stream()` / `stop_stream()`.
- There is **no `discard_queued()`** and no `TriggerSoftware`; `trigger: software` behaves like free-run here (`devices/SimulatedCamera.py:201-219`). Guard with `getattr(cam, "discard_queued", None)` as `cv_process` does (`devices/CVProcessDevice.py:449-451`).

**`.api`**: `TestPattern` (`devices/SimulatedCamera.py:61-96,160-162`): `frame(t=None) -> np.ndarray` (RGB) and `ball_center(t) -> (x, y)`, where `t` is seconds since start. Useful for checking an analysis against ground truth.

**Blocking and ABORT**: `abort()` only sets `streaming = False` (`devices/SimulatedCamera.py:189-190`). A capture sleeps at most `timeout_sec` / one frame period and is not interrupted.

**Simulated**: this *is* the hardware-free camera. Return values are real arrays, so code can rely on them.

```python
cam = devices["camera"]
cam.set_feature("PixelFormat", "Mono8")            # restarts the stream
img = cam.capture_frame_8bit()
assert img.shape[:2] == (cam.get_feature("Height"), cam.get_feature("Width"))
```

---

## 10. cv_process — `CVProcessDevice`

**Config** (`devices/registry.py:531-544`):
- `camera` (device name, **required**; must have `capture_frame_8bit`, i.e. `gige_camera` or `simulated_camera`, `devices/CVProcessDevice.py:174-176`).
- `analysis` (str `"module:function"`, **required**; resolved at construction, also inside `app_logic.`, `devices/CVProcessDevice.py:72-102,184`).
- `analysis_params` (dict, None) · `autostart` (bool, False) · `rate_hz` (float, 5.0) · `capture_timeout_s` (float, 2.0) · `fault_on_error` (bool, False).
- Aliases: `vision`, `cv`. The camera is initialized first and shut down last (`devices/registry.py:26-28`).

**`initialize()`**: stops any running analysis, marks the device ready, and calls `start()` if `autostart` (`devices/CVProcessDevice.py:242-248`).

**`.api`**: the **camera device object** (`devices/CVProcessDevice.py:237-240`), so `vision.api.set_feature(...)` works.

**Driver methods** (task-facing, all thread-safe):
- `analyze_once(timeout=5.0) -> dict` `{seq, time, ok, data, analysis_ms}` (`devices/CVProcessDevice.py:437-457`).
  - While running, it returns the next result.
  - While stopped, it drops queued frames, grabs one frame in the calling thread (waiting `min(timeout, capture_timeout_s)`, or `timeout` for a hardware trigger) and analyses it.
  - It raises `RuntimeError` if the analysis raised or the device is not initialized.
- `wait_for_result(after_seq=None, timeout=5.0) -> dict`: raises `TimeoutError`, or `RuntimeError` on abort (`devices/CVProcessDevice.py:414-435`).
- `last_result() -> dict | None` · `last_raw_image()` / `last_processed_image() -> np.ndarray | None` (read-only arrays) (`devices/CVProcessDevice.py:399-412`).
- `start()` / `stop()` / `running` (`devices/CVProcessDevice.py:266-298`) · `video_source(kind="processed")` for custom views (459-479).
- `data` is whatever the analysis returned; `ok` is `bool` or `None` (`devices/CVProcessDevice.py:64-69,120-141`).

**Blocking and ABORT**:
- `abort()` only wakes `wait_for_result()`, which raises `RuntimeError`. The analysis loop keeps running (`devices/CVProcessDevice.py:258-262`).
- So `analyze_once()` is abortable **only while the analysis is running**. Stopped, it is a direct `capture_frame_8bit` call that `abort()` does not touch; it ends at its timeout or when the camera's own abort takes effect (see gige). The README's "ABORT wakes ... `analyze_once()`" (`README.md:557`) holds only for the running case.
- An exception in the analysis does not fault the device unless `fault_on_error` (`devices/CVProcessDevice.py:380-395`).

**Simulated** (`type: simulated`): `analyze_once`, `last_result` etc. don't exist (AttributeError). For hardware-free runs, keep `cv_process` and point it at a `simulated_camera`.

```python
vision = devices["vision"]
res = vision.analyze_once(timeout=5.0)             # {seq, time, ok, data, analysis_ms}
if not res["ok"]:
    raise RuntimeError(f"inspection failed: {res['data']}")
x_px, y_px = res["data"]["x_px"], res["data"]["y_px"]   # keys = your analysis's data dict
```

---

## 11. thorlabs_pm — `ThorlabsPowerMeter`

**Config** (`devices/registry.py:545-557`): all optional. `resource` (str, None = auto-detect the single meter), `backend` (str, None), `timeout_ms` (int, 5000), `wavelength` (float nm, None), `averaging` (int, None), `power_unit` (`"W"`/`"DBM"`, None). Alias: `thorlabs_power_meter`.

**`initialize()`** (`devices/ThorlabsPowerMeter.py:163-165,190-224`):
- Opens the VISA session and **checks that a sensor head is attached** (raises if not).
- Then sets `wavelength`, `averaging` and `power_unit` if configured. The meter keeps its last unit otherwise (`devices/ThorlabsPowerMeter.py:75-77`).
- No zeroing.

**`.api`**: `PowerMeter` subclass `_CellPowerMeter` (`devices/ThorlabsPowerMeter.py:11-40`; `devices/api/thorlabs_pm.py:220`). An RLock serialises every exchange (`devices/api/thorlabs_pm.py:255-257`).
- `read_power() -> float` (in the current unit) (`devices/api/thorlabs_pm.py:470-472`).
- `measure(quantity="power") -> float`; quantity is one of power/current/voltage/energy/power_density/energy_density/frequency/temperature; overrange gives ±inf, invalid gives nan (461-468).
- `acquire(n, interval=0.0, quantity="power") -> (times: list[float], values: list[float])` (474-490).
- `zero(timeout=30.0, poll=0.2) -> float` (blocking dark adjustment; cover the sensor) (492-510).
- **Properties** (get/set): `wavelength` (nm), `averaging`, `auto_range`, `power_range` (W), `power_unit` (`"W"`/`"DBM"`, ValueError otherwise), `low_bandwidth`, `accelerator` (391-459). Read-only: `wavelength_limits -> (min, max)` (400-402), `idn -> str` (382-384), `sensor -> SensorInfo` (`.name, .serial, .kind, .connected, ...`) (169-215, 386-388).
- `reset()` (`*RST`) (374-379).
- Setters drain the SCPI error queue and raise `SCPIError` (`devices/api/thorlabs_pm.py:345-350,369-372`).

**Blocking and ABORT**:
- `abort()` only logs; a reading ends within `timeout_ms` (`devices/ThorlabsPowerMeter.py:180-184`).
- `zero()` holds the session lock until done or timed out (`devices/api/thorlabs_pm.py:497-510`) and is not abortable. `acquire()` loops `n` times and is not abortable.
- Any VISA or OS error latches `faulted` (cell faults) until CLEAR FAULTS reopens the session (`devices/ThorlabsPowerMeter.py:170-178,234-239`). SCPI errors do not fault.

**Simulated**:
- `read_power()` returns `None`, so arithmetic and comparisons raise TypeError.
- Reading `api.wavelength` returns a *function object*.
- `api.wavelength = 532` is silently accepted.

```python
pm = devices["power_meter"]
pm.api.wavelength = 1064                           # property setter, raises SCPIError if refused
times, values = pm.api.acquire(20, interval=0.05)
mean_w = sum(values) / len(values)
context.set_variable("last_power_w", mean_w)
```

---

## 12. smaract_mcs2 — `SmarActMCS2`

**Config** (`devices/registry.py:558-574`):
- `ip_address` (str, **required**).
- `port` (int, 55551) · `timeout` (float, 5.0 s per reply).
- `channels` (int or list of int, None = all).
- `reference` (`if_needed`|`always`|`never`, default `if_needed`) · `reference_reverse` (bool, False) · `reference_timeout_s` (float, 120.0).
- `velocity_mm_s` / `acceleration_mm_s2` (float, None).
- Aliases: `mcs2`, `smaract`.

**`initialize()`** (`devices/SmarActMCS2.py:191-193,226-304`):
- Opens TCP and drains stale errors.
- Checks that every used channel **has a sensor** (raises if not).
- Sets the velocity/acceleration if configured.
- **References** each channel: `if_needed` (only unreferenced channels) or `always`, waiting ≤ `reference_timeout_s` per channel, with `auto_zero` (`devices/api/smaract_mcs2.py:476-485`). `never` leaves it to task code and only logs a warning.
- CLEAR FAULTS reconnects **without** referencing, and raises if a channel lost its reference (`devices/SmarActMCS2.py:203-211,296-304`).

**`.api`**: `MCS2` subclass `_CellMCS2` (`devices/SmarActMCS2.py:11-40`; `devices/api/smaract_mcs2.py:172`). One device covers all channels; `ch` is the channel index.
- Moves (**non-blocking**; call `wait`):
  - `move_to_mm(ch, mm, velocity_mm_s=None, acceleration_mm_s2=None)` (389-397) · `move_by_mm(ch, mm, ...)` (399-407).
  - Picometre versions: `move_to(ch, pos_pm, velocity=None, acceleration=None)` / `move_by(...)` (378-387).
  - `reference(ch, reverse=False, auto_zero=True)` (476) · `calibrate(ch)` (472) · `stop(ch)` (420).
- `wait(ch, timeout=60.0, poll=0.05) -> None` (423-449). It raises:
  - `MCS2Aborted` on abort.
  - `MCS2MotionError` if the move failed, or if still busy at the timeout (the channel is stopped first).
- Status: `position_mm(ch) -> float` · `target_mm(ch) -> float` · `position(ch) -> int` (pm) · `is_moving/is_busy/is_referenced/is_calibrated/has_sensor(ch) -> bool` · `state(ch) -> int` · `state_flags(ch) -> list[str]` · `positioner_name(ch) -> str` (326-364).
- Motion params: `set_velocity_mm_s(ch, mm_per_s)` · `set_acceleration_mm_s2(ch, mm_per_s2)` (372-376).
- Errors: `MCS2CommandError` (controller error queue) · `MCS2Error` · socket failures as raw `OSError` (136-158).

**Driver**: `channels -> list[int]` (resolved at connect) (`devices/SmarActMCS2.py:153-156`).

**Blocking and ABORT**:
- `abort()` → `api.abort(channels)`. It bumps a counter, so **every in-progress `wait()` raises `MCS2Aborted`**, then stops all channels; this works even if the link is down (`devices/SmarActMCS2.py:213-220`; `devices/api/smaract_mcs2.py:451-469`).
- A `wait()` started after the abort is unaffected (`devices/api/smaract_mcs2.py:455`).
- Socket errors latch `faulted` (cell faults); a failed move does not fault the device, it raises in the task (`devices/SmarActMCS2.py:56-60,314-319`).

**Simulated**: `position_mm()` returns `None` and `wait()` returns at once; `stage.channels` does not exist (AttributeError).

```python
stage = devices["stage"]
stage.api.move_to_mm(0, 12.5, velocity_mm_s=2.0)
stage.api.wait(0, timeout=30)                      # MCS2Aborted on ABORT, MCS2MotionError on failure
pos = stage.api.position_mm(0)
```

---

## 13. zaber — `ZaberAxis` (legacy)

**Config** (`devices/registry.py:575-582`): `port` (str, default `"COM3"`), `axis_number` (int, default `1`).

**`initialize()`** (`devices/OLD_ZaberAxis.py:46-63`):
- Opens the serial port, detects devices, and takes axis `axis_number` of the **first** device.
- Then **`home()` + `wait_until_idle()`**: the axis is homed.
- **CLEAR FAULTS re-runs `initialize()` (and homes again) if the link is down** (`devices/OLD_ZaberAxis.py:75-79`).

**`.api`**: `zaber_motion.ascii.Connection` (`devices/OLD_ZaberAxis.py:42-44,49`). The axis object is private (`_axis`); use the driver method instead.
- `move_axis(position: float, speed: float = 250) -> None` (`devices/OLD_ZaberAxis.py:93-101`). Units are mm and mm/s.
  - It sets `maxspeed`, then `move_absolute(position, mm)`; zaber_motion's `wait_until_idle=True` default makes that **blocking** (`.venv/Lib/site-packages/zaber_motion/ascii/axis.py:411-422`). It then calls `wait_until_idle()`.
  - It raises RuntimeError if not `ready`.

**Blocking and ABORT**:
- `abort()` calls `_axis.stop()` + `wait_until_idle()` (`devices/OLD_ZaberAxis.py:81-91`).
- zaber_motion documents `MovementInterruptedException` as "thrown when ongoing movement is interrupted by another command" (`.venv/Lib/site-packages/zaber_motion/exceptions/movement_interrupted_exception.py:9-12`). So the task's `move_axis` should raise that exception; this is not tested in the repo.
- If `abort()` itself fails, it sets `faulted` and re-raises.
- `faulted` is also True whenever the axis is disconnected (`devices/OLD_ZaberAxis.py:38-40`).

**Simulated**: `move_axis` does not exist on `SimulatedDevice` (AttributeError), and `api.*` calls are no-ops. Code that must run simulated cannot use this driver's API unchanged.

```python
axis = devices["zaber"]
axis.move_axis(25.0, speed=50)                     # blocks until idle; mm, mm/s
axis.move_axis(0.0)
```

---

## 14. simulated — `SimulatedDevice`

**Config** (`devices/registry.py:583-594`): `connect_delay` (float, 0.0 s), `fail_to_connect` (bool, False), `fail_rate` (float 0-1, 0.0: chance per `faulted` check of latching a fault), `info` (dict, None), `supports_monitoring` (bool, False). Aliases: `dummy`, `fake`.

**`initialize()`**: sleeps `connect_delay`, raises `ConnectionError` if `fail_to_connect`, otherwise connected and unfaulted (`devices/SimulatedDevice.py:115-124`).

**`.api`**: `_FakeApi` (`devices/SimulatedDevice.py:9-31`). **Any** attribute is a callable that logs `api.<name>(args, kwargs) -> simulated no-op` to the device log and **returns `None`**. It counts calls (`api.calls`, `api.last_call`, also shown in `telemetry()`, `devices/SimulatedDevice.py:135-142`).
- Therefore **never rely on a return value**: `get_part()`, `read_di()`, `GetPose()`, `read_power()`, `position_mm()`, `get_measurements()`, `SetCheckpoint().wait()`, `get_part_async().result()` all return `None` or fail on `None`.
- Waits (`WaitIdle`, `wait`, `wait_move_done`) return instantly.
- `SimulatedDevice` has no device-specific driver methods (`rt`, `deactivate`, `set_feature`, `capture_frame_8bit`, `analyze_once`, `move_axis`, `set_digital_pin`, `set_single_output` (driver level), `channels`, `streaming`...): AttributeError.

**Driver extras**: `io_points()` (di1 blinks, di2 mirrors do1, do1-do4 writable) and `write_io("do1".."do4", value)` (`devices/SimulatedDevice.py:144-158`).

**ABORT**: `abort()` only logs (`devices/SimulatedDevice.py:164-165`); nothing blocks. `fail_rate > 0` makes `faulted` flip randomly, which faults the cell and aborts the task (`devices/SimulatedDevice.py:99-103`). `clear_fault()` resets it.

**Pattern for code that must run on both real and simulated devices**:

```python
pose = feeder.api.get_part() or {}                 # None when simulated
if pose.get("resp", 200) != 200:                   # tolerate the simulated empty dict
    raise RuntimeError(f"feeder: {pose}")
x, y, rz = pose.get("x", 0.0), pose.get("y", 0.0), pose.get("rz", 0.0)
robot.api.MovePose(x, y, 20, 180, 0, rz); robot.api.WaitIdle()
```

---

## 15. custom

**Config** (`devices/registry.py:18-24, 265-273`):
- `type: "custom"`, `driver: "module:ClassName"` (**required**, importable from the workspace).
- Every other key except `type`/`driver`/`enabled` is passed to the constructor **as-is**: no schema, coercion, defaults or unknown-key warnings.
- The constructor is called with `name=<config name>` too (`devices/registry.py:428`), so it must accept `name`.
- `device`-type links are not available to custom entries, because `_device_refs` returns `[]` for them (`devices/registry.py:330-336`).

**`initialize()` / `.api` / blocking / ABORT**: whatever the class implements. It must subclass `Device` and implement `info, connected, ready, faulted, api, initialize, shutdown, clear_fault, abort` (`devices/Device.py:130-195`). To be ABORT-safe, its `abort()` must make its own blocking waits raise (the SmarAct counter pattern, `devices/api/smaract_mcs2.py:423-469`, is a good model). Read the class before generating task code; anything beyond the base interface is unknown here.

**Simulated**: same as section 14. Only `.api.*` calls survive a swap to `type: simulated`; driver-level methods do not.
