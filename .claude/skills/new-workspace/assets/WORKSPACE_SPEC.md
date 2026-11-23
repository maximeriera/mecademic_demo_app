# <<TITLE>> — workspace spec

> Status: **DRAFT — not approved.** Nothing but this file is written until it is approved in chat.
> Edit this file directly or answer in chat; every change is folded back here.

| | |
|---|---|
| Folder | `<<absolute path of the new workspace>>` |
| Kind | <<Client workspace / Example in mecademic_demo_app/examples/>> |
| Title (workspace picker) | <<TITLE>> |
| App | `<<absolute path of mecademic_demo_app>>`, commit `<<short sha>>` |

## 1. What the cell does

<<Two to four sentences: the part, where it comes from, what is done to it, where it goes. One PROD cycle = one ... >>

## 2. Devices

| Name in code | Type | Connection | Role | In the simulated smoke test |
|---|---|---|---|---|
| `robot` | `mecademic` | ip 192.168.0.100 (**to confirm**) | picks and places | `simulated` |

Not supported by the app yet: <<none / device + "needs the add-device skill first">>.

## 3. Robot programs (on the controllers)

Motion runs in programs saved on each robot. Python starts a program and waits for the checkpoint set on its last line.

| Robot | Program | What it does | Ends with | Robot variables it reads |
|---|---|---|---|---|
| `robot` | `go_home` | moves to the start pose | `SetCheckpoint(1)` | – |

Python-side motion instead of a program: <<none / list, with why>>.

## 4. Sequences

Step names are the exact `context.step()` names shown in the UI and declared under `sequences:`.

### PROD cycle (`prod_cycle`, repeated until STOP)

| # | Step | Devices and actions | Waits for | On failure |
|---|---|---|---|---|
| 1 | Request part from feeder | `feeder.api.get_part()`, up to `feeder_retries` times | feeder reply (≤ 30 s, not abortable) | go to step 4 |

Parallelism: <<none / which robots move at the same time, and where they meet again>>.
Conditional steps (declared last): <<...>>.

### HOME (`home`) — also runs before the first PROD cycle and after STOP

| # | Step | Devices and actions | Waits for |
|---|---|---|---|

### SHIPMENT (`shipment`)

| # | Step | Devices and actions | Waits for |
|---|---|---|---|

### CALIBRATION (`calib`)

| # | Step | Devices and actions | Waits for |
|---|---|---|---|

## 5. Manual actions (Manual tab)

| Key | Label | Steps | Confirmation message |
|---|---|---|---|

## 6. Params and variables (Variables tab)

| Name | Kind | Type | Default | Editable | Reset on | Persist | Used by |
|---|---|---|---|---|---|---|---|

## 7. Custom view

<<None / what it shows: feeds, live values, plots>>

## 8. Faults and ABORT

- A robot program that does not reach its checkpoint within `program_timeout_s` raises, which faults the cell.
- ABORT stops every robot (ClearMotion); a pending checkpoint wait raises and the cycle ends without the exit home.
- <<device-specific: e.g. a feeder request in progress is not interrupted by ABORT and ends at its own timeout>>

## 9. Open points (to confirm at the machine)

- [ ] <<IP addresses, program names, checkpoint numbers, recipe, I/O channel meanings...>>

## 10. Approval

<<"Approved in chat on YYYY-MM-DD." — written by the skill when the user approves, together with the list of changes since the first draft.>>
