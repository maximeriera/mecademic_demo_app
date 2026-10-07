# Fiber alignment demo

A Mecademic robot takes one of N fibers (connected to a light source) from its
rack. It brings the fiber in front of a fixed fiber connected to a Thorlabs
power meter and aligns it until the power reaches a threshold. Then it backs
out and puts the fiber back where it was. Each PROD cycle handles one fiber and
works through the rack in order.

| # | Step (as shown in the UI) | Devices |
|---|---|---|
| 1 | Pick fiber | robot + gripper |
| 2 | Move to alignment start | robot |
| 3 | Align fiber | robot + power meter |
| 4 | Retract from alignment | robot |
| 5 | Place fiber (into the slot it was picked from) | robot + gripper |

A fiber that does not reach the threshold is still put back and counted in
`failed_count`, and the loop moves on to the next fiber.

## Files

| File | What it holds |
|---|---|
| `config.yaml` | Devices (`meca_robot`, `fiber_gripper`, `power_meter`), Manual tab buttons, step sequences |
| `context.yaml` | Params (threshold, alignment settings, speeds, `manual_fiber`) and variables (cell state, results) |
| `prod.py` | PROD cycle: `run_cycle(next_fiber)`, counters, next fiber |
| `home.py`, `shipment.py` | Back out of the alignment station if needed, then a joint position |
| `calib.py` | Nothing to calibrate yet |
| `manual_actions.py` | One Manual tab button per step, plus gripper open/close and a full cycle |
| `fiber_alignment/poses.py` | **Data**: fiber slots, alignment station, home/shipment, tool frame |
| `fiber_alignment/cell.py` | Device names, `ProcessError`, robot setup (frame, speeds) |
| `fiber_alignment/gripper.py` | Placeholder gripper, until its driver exists |
| `fiber_alignment/process.py` | The five steps, shared by PROD and the Manual tab |
| `fiber_alignment/alignment.py` | Contract with the alignment algorithm: moves, readings, timeout, verdict |
| `fiber_alignment/algorithm.py` | The algorithm: **placeholder** spiral search, to be replaced |
| `custom_view/` | `/view/`: live power vs threshold (green/red), and the power-vs-X/Y graph of the alignment |

## To do before running on the cell

- [ ] Teach every pose in `fiber_alignment/poses.py` (and `TOOL_TRF`), then set `POSES_TAUGHT = True`. Until then, every robot move is refused.
- [ ] `config.yaml`: the robot's IP address, and the power meter's `wavelength` (the source's).
- [ ] `context.yaml`: `power_threshold_mw`, from the peak power measured on the setup.
- [ ] Gripper: when its driver exists, set its `type` in `config.yaml` and replace the two calls in `fiber_alignment/gripper.py`.
- [ ] Alignment: plug the colleague's algorithm into `fiber_alignment/algorithm.py`. Its docstring gives the contract.

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

Open `/view/` during Run Alignment to watch the graph fill.
