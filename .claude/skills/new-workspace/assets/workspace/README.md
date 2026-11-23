# <<TITLE>>

<<One paragraph: what the cell does, for whom, and what one PROD cycle produces.>>

This folder is a workspace of the [Mecademic demo app](https://github.com/maximeriera/mecademic_demo_app): the app provides the web UI, the device drivers and the task runner; this folder provides the cell's devices (`config.yaml`), its sequences (`*.py`) and its tunable values (`context.yaml`). The design decisions are in [WORKSPACE_SPEC.md](WORKSPACE_SPEC.md).

## Run it

```bash
python <path to>/mecademic_demo_app/app.py --workspace <path to this folder>
```

or, from the `mecademic_demo` superproject on Windows: `start_demo.bat <path to this folder>`.

Open http://localhost:5000, press **INITIALIZE** (connects, activates and homes every robot), then **HOME**, then **PROD** on the Production tab. **STOP** ends after the current cycle and homes; **ABORT** stops at once.

## Devices

| Name in code | Type | Address | Role |
|---|---|---|---|
| `robot` | Mecademic (`mecademic`) | 192.168.0.100 | Picks from the feeder, places on the tray |
| `feeder` | Asyril Eye+ (`asyril`) | 192.168.0.50, recipe 63083 | Presents parts and returns their pose |
| `io` | Moxa ioLogik E1212 (`iologik`) | 192.168.127.254 | DO0: feeder-empty lamp |

## Robot programs

The motion lives in programs saved on the robot controllers. Each program ends with `SetCheckpoint(<n>)`; the Python side starts it and waits for that checkpoint. **They must exist on the controller before PROD runs.**

| Robot | Program | Ends with | Reads robot variables | Called from |
|---|---|---|---|---|
| `robot` | `go_home` | `SetCheckpoint(1)` | – | `home.py` |
| `robot` | `go_shipment` | `SetCheckpoint(2)` | – | `shipment.py` |
| `robot` | `go_calib_pose` | `SetCheckpoint(3)` | – | `calib.py` |
| `robot` | `pick_feeder` | `SetCheckpoint(11)` | `pick_x`, `pick_y`, `pick_rz` | `prod.py`, manual action *Pick one part* |
| `robot` | `place_tray` | `SetCheckpoint(12)` | – | `prod.py` |

## Sequences

| Task | Steps |
|---|---|
| PROD cycle | Request part from feeder → Pick part → Place part (→ Reject part when the feeder has no pickable part) |
| HOME | Open gripper → Move to home |
| SHIPMENT | Open gripper → Move to shipment pose |
| CALIBRATION | Move to calibration pose |
| Manual: *Open gripper* | Open gripper |
| Manual: *Pick one part* | Request part from feeder → Pick part |

## Params and variables (Variables tab)

| Name | Kind | Default | Meaning |
|---|---|---|---|
| `program_timeout_s` | param | 60 | Longest a robot program may take, seconds |
| `feeder_retries` | param | 3 | Feeder requests before a cycle is rejected |
| `part_count` | variable | 0 | Parts placed since PROD start |
| `reject_count` | variable | 0 | Rejected cycles since PROD start |

## Files

| File | Contents |
|---|---|
| `config.yaml` | Devices, manual actions, declared step sequences |
| `context.yaml` | Params and variables |
| `prod.py` | `prod_cycle()`: one production cycle |
| `home.py`, `shipment.py`, `calib.py` | HOME, SHIPMENT and CALIBRATION tasks |
| `manual_actions.py` | Manual tab buttons |
| `cell_tools.py` | Start a robot program and wait for its checkpoint; run branches in parallel |
| `WORKSPACE_SPEC.md` | The approved design: devices, sequences, open points |

## Before the first run on hardware (TODO)

- [ ] Confirm every IP address in `config.yaml`.
- [ ] Create the robot programs and robot variables listed above on the controller.
- [ ] Check the Eye+ recipe number.

## Tested

- Simulated smoke test (`check_workspace.py` of the new-workspace skill): every task ran on simulated devices, <<date>>.
- Hardware: not yet.
