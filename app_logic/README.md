# Fiber alignment demo

A Mecademic robot takes one of N fibers (connected to a light source) from its
rack. It brings the fiber in front of a fixed fiber connected to a Thorlabs
power meter and aligns it until the power reaches a threshold. Then it backs
out and puts the fiber back where it was. Each PROD cycle handles one fiber and
works through the rack in order.

| # | Step (as shown in the UI) | How |
|---|---|---|
| 1 | Pick fiber | robot program `fiber<N>_in`, gripper closes, `fiber<N>_out` |
| 2 | Move to alignment start | robot program `align_start` |
| 3 | Align fiber | colleague's algorithm: robot moves from Python + power meter |
| 4 | Retract from alignment | robot program `align_retract` |
| 5 | Place fiber (into the slot it was picked from) | robot program `fiber<N>_in`, gripper opens, `fiber<N>_out` |

A fiber that does not reach the threshold is still put back and counted in
`failed_count`, and the loop moves on to the next fiber.

## Robot programs

Every absolute move is a program stored on the robot (Mecademic web portal),
started with `StartProgram`. Their names are in `fiber_alignment/poses.py`.

| Program | From → to |
|---|---|
| `fiber1_in` … `fiber4_in` | any safe pose → the slot's grip pose. Never drives the gripper |
| `fiber1_out` … `fiber4_out` | the slot's grip pose → a safe pose |
| `align_start` | a safe pose → the alignment start: fibers facing, small gap |
| `align_retract` | the aligned pose (anywhere inside the alignment window) → straight back out along the fiber axis → a safe pose |

Teach them with `TOOL_TRF` (`poses.py`) active: Python sets it before every
step, and the alignment rotates about it, so its TCP must be the fiber tip.
They run at the speeds set in the Variables tab unless they set their own.

## Files

| File | What it holds |
|---|---|
| `config.yaml` | Devices (`meca_robot`, `fiber_gripper`, `power_meter`), Manual tab buttons, step sequences |
| `context.yaml` | Params (threshold, alignment settings, speeds, `manual_fiber`) and variables (cell state, results) |
| `prod.py` | PROD cycle: `run_cycle(next_fiber)`, counters, next fiber |
| `home.py`, `shipment.py` | Back out of the alignment station if needed, then a joint position |
| `calib.py` | Nothing to calibrate yet |
| `manual_actions.py` | One Manual tab button per step, plus gripper open/close and a full cycle |
| `fiber_alignment/poses.py` | **Data**: robot program names, tool frame, home/shipment joints |
| `fiber_alignment/cell.py` | Device names, `ProcessError`, robot setup (frame, speeds), `run_program()` |
| `fiber_alignment/gripper.py` | SmarAct gripper: open/close are moves of its MCS2 jaw channel |
| `fiber_alignment/process.py` | The five steps, shared by PROD and the Manual tab |
| `fiber_alignment/alignment.py` | Contract with the algorithm: 3D moves, scans, readings, window, timeout, verdict |
| `fiber_alignment/algorithm.py` | The colleague's algorithm (spirals, axial search, gradient circles), ported |
| `fiber_alignment/frames.py` | His frame math (poses ↔ 4×4 matrices, plane fit) |
| `custom_view/` | `/view/`: live power vs threshold (green/red), the verdict, and a plot per alignment phase (see below) |

## To do before running on the cell

- [ ] Write the robot programs above in the web portal, taught with `TOOL_TRF` active.
- [ ] Teach `TOOL_TRF` and the home/shipment joints in `fiber_alignment/poses.py`, then set `POSES_TAUGHT = True`. Until then, every robot move is refused.
- [ ] `config.yaml`: the robot's IP address. The power meter's `wavelength` (1557 nm, from the colleague's script) should match the source.
- [ ] `context.yaml`: `power_threshold_mw`, from the peak power measured on the setup, and the four phase thresholds (`spiral_cart_threshold_mw` … `gradient_threshold_mw`) from the robot variables of the colleague's `fiber_variables` program.
- [ ] Check `align_range_um` / `align_range_deg`: any pose the algorithm asks for beyond them is refused, which faults the alignment.
- [ ] Gripper (SmarAct MCS2 at 192.168.0.20): check its jaw channel and `velocity_mm_s` in `config.yaml`, then teach `gripper_open_mm` / `gripper_closed_mm` (Variables tab). The closed position is where the jaws hold the fiber, not beyond it.

## Testing step by step

With the controller READY, run the Manual tab buttons in process order, at low
speed first (`joint_vel_pct`, `lin_vel_mm_s`):

Pick Fiber → Move to Alignment Start → Run Alignment → Retract → Place Fiber

- `manual_fiber` (Variables tab) selects the fiber for Pick Fiber and Full Cycle.
- Each step checks the cell state before moving, using `held_fiber` and
  `at_alignment`. Asked for in the wrong state, it faults the cell with a
  message saying what to do. The message is in the ApplicationController log;
  fix the state, then Clear Faults.
- After a manual intervention (e.g. a fiber removed by hand), correct
  `held_fiber` / `at_alignment` in the Variables tab.
- HOME backs out of the alignment station first. It never opens the gripper.
  Recovering from an abort at a rack slot is not automated: jog the robot clear.

Open `/view/` during Run Alignment to watch it.

## Custom view (`/view/`)

Laid out for a 1920×1080 screen, read from a distance. It stacks on narrower
screens.

- **Top row**:
  - the live meter reading, green at or above `power_threshold_mw`, red below;
  - the power over time in dB vs the threshold (best so far in green), with
    a band per phase;
  - the alignment: the verdict, the time spent against `align_timeout_s`,
    and one line per phase with its gain in dB and its duration.
- **Plots, side by side**: the Cartesian spiral (X/Z in µm), the angular
  spiral (α/γ in °) and the gradient circles (lateral and angular). Each is
  a 3D landscape of the power read over the axes that phase moves, and stays
  on screen after its phase. The running phase's card is outlined in yellow.
  - The axial search is often skipped. When it runs, its scan profiles take
    the gradient's card until the gradient starts.
  - Axes fit the phase's readings.
  - Colours span the dB range of the plot. A colour bar gives the values and
    marks the threshold, so small differences near the peak still show.

Everything comes from the hub. A page opened or reloaded after a run redraws it
from `align_result`. If the algorithm changes, keep its `io.phase()` /
`io.publish()` calls and its phase names (see `fiber_alignment/algorithm.py`):
`custom_view/view.js` finds each phase's plot by its name.
