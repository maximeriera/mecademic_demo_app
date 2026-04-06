# Mecademic Demo App

A Flask-based web application for controlling a robotic cell built around [Mecademic](https://www.mecademic.com/) robots.
Provides a single-page web UI and a REST API to initialize, monitor, and run tasks across all connected devices.

---

## Table of Contents

- [Mecademic Demo App](#mecademic-demo-app)
  - [Table of Contents](#table-of-contents)
  - [Features](#features)
  - [Architecture](#architecture)
  - [Project Structure](#project-structure)
  - [Requirements](#requirements)
  - [Quick Start](#quick-start)
    - [Typical workflow](#typical-workflow)
  - [Configuration](#configuration)
    - [Supported device types](#supported-device-types)
    - [Example](#example)
  - [Web UI](#web-ui)
    - [Control tab](#control-tab)
    - [Logs tab](#logs-tab)
  - [REST API](#rest-api)
    - [System](#system)
    - [Tasks](#tasks)
    - [Logs](#logs)
  - [State Machine](#state-machine)
  - [Stop vs. Abort](#stop-vs-abort)
  - [Adding a New Device](#adding-a-new-device)
  - [Logging](#logging)

---

## Features

| Area | Details |
|---|---|
| **Multi-device architecture** | Abstract `Device` base class with per-device rotating log files |
| **Mecademic robot support** | Wraps [mecademicpy](https://github.com/Mecademic/mecademicpy) — connection, homing, motion, fault detection, and clearing |
| **Additional devices** | Asyril Eye+ feeder, planar motor, Arduino I/O (Firmata), Moxa ioLogik E1212 remote I/O, LMI Gocator 3D sensor |
| **Web UI** | Real-time status badges, manual/auto task controls, device cards, integrated log viewer |
| **REST API** | Full endpoint set for status, task management, initialization, shutdown, and fault clearing |
| **Thread-safe state machine** | `ControllerState` (OFF → INITIALIZING → READY → BUSY → FAULTED) with lock-protected transitions |
| **Task system** | Background `Task` thread — PROD loop, HOME, SHIPMENT, and CALIBRATION |
| **Graceful stop vs. immediate abort** | STOP finishes the current cycle; ABORT calls `ClearMotion()` to unblock `WaitIdle()` instantly |
| **Fault monitoring** | Background thread polls every 200 ms; any device fault auto-aborts and sets FAULTED |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Browser (index.html + app.js)                              │
│  ─ polls /api/status, /api/info every second                │
│  ─ sends POST to /api/task/*, /api/initialize, …            │
└──────────────────────────┬──────────────────────────────────┘
                           │  HTTP (port 5000)
┌──────────────────────────▼──────────────────────────────────┐
│  Flask server  (app.py)                                     │
│  ─ routes map to ApplicationController methods              │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│  ApplicationController  (core/)                             │
│  ┌──────────────┐  ┌──────────┐  ┌───────────────────────┐  │
│  │ControllerState  │   Task   │  │  Monitor thread       │  |
│  │(state machine)  │ (thread) │  │  (fault polling 200ms)│  |
│  └──────────────┘  └──────────┘  └───────────────────────┘  │
│  ─ owns all Device instances                                │
│  ─ delegates task logic to application_code/                │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│  Devices  (devices/)                                        │
│  MecaRobot · AsyrilEyePlus · PlanarMotor · ArduinoBoard     │
│  IoLogikE1212 · LMISensor                                   │
│  ─ each extends abstract Device base class                  │
│  ─ each has a dedicated rotating log under logs/devices/    │
└─────────────────────────────────────────────────────────────┘
```

---

## Project Structure

```
app.py                        # Flask server — all REST API endpoints
config.yaml                   # Device declarations (type, IP, …)
requirements.txt              # Python dependencies
autostart.bat                 # Windows quick-launch script

core/
    ApplicationController.py  # Orchestrator: devices, tasks, monitor thread
    ControllerState.py        # ControllerState enum (OFF → INITIALIZING → READY → BUSY → FAULTED)
    Task.py                   # Task thread — TaskType enum + execution logic

devices/
    Device.py                 # Abstract base class — contract for all devices
    MecaRobot.py              # Mecademic robot (mecademicpy)
    Asyril.py                 # Asyril Eye+ intelligent feeder
    PlanarMotor.py            # Planar motor system
    ArduinoBoard.py           # Arduino I/O via Firmata
    IoLogikE1212.py           # Moxa ioLogik E1212 remote I/O (Modbus TCP)
    LMISensor.py              # LMI Gocator 3D sensor (Ethernet ASCII)
    api/                      # Low-level device protocol implementations

application_code/
    home.py                   # HOME task — move all robots to home positions
    shipment.py               # SHIPMENT task — move robots to storage positions
    prod.py                   # PROD task — looped production sequence
    calib.py                  # CALIBRATION task — calibration sequence (if needed)

templates/index.html          # Single-page web UI (Jinja2)
static/css/app.css            # Styles
static/js/app.js              # UI logic and REST polling

logs/
    app/                      # Rotating logs for Flask + ApplicationController
    devices/                  # Per-device rotating log files
```

---

## Requirements

- **Python 3.10+** (uses `match`/`case` and `X | Y` union type hints)

Install all dependencies:

```bash
pip install -r requirements.txt
```

> The `ressources/` folder contains a bundled wheel for `pmclib` (planar motor library) that is not on PyPI.
> Install it manually if needed: `pip install ressources/pmclib-117.9.1-py3-none-any.whl`

---

## Quick Start

```bash
# 1. Clone the repository and navigate into it
cd mecademic_demo_app

# 2. Install dependencies
pip install -r requirements.txt

# 3. Edit config.yaml to declare your devices and IP addresses
#    (see the Configuration section below)

# 4. Start the server
python app.py
```

Open **http://localhost:5000** in a browser.

On Windows you can also double-click **`autostart.bat`** to launch the app.

### Typical workflow

1. Open the web UI.
2. Click **INITIALIZE** — the controller connects to every device declared in `config.yaml` and homes them.
3. Run a task: **HOME**, **SHIPMENT**, **CALIBRATION** (single-run) or **PROD** (continuous loop).
4. **STOP** finishes the current cycle gracefully; **ABORT** interrupts immediately.
5. Click **SHUTDOWN** when done.

---

## Configuration

All devices are declared in **`config.yaml`**. Each entry needs a unique name and a `type` that maps to a device driver class.

### Supported device types

| `type` value | Driver class | Required fields | Optional fields |
|---|---|---|---|
| `mecademic` | `MecaRobot` | `ip_address` | — |
| `asyril` | `AsyrilEyePlus` | `ip_address`, `recipe` | `port` (default 7171) |
| `arduino` | `ArduinoBoard` | `port` (e.g. `"COM3"`) | — |
| `planarmotor` | `PlanarMotor` | `ip_address` | — |
| `iologik` | `IoLogikE1212` | `ip_address` | `port` (default 502), `slave_id` (default 1) |
| `lmi` | `LMISensor` | `ip_address` | `control_port`, `data_port`, `health_port`, `delimiter`, `terminator` |

### Example

```yaml
devices:
  mirror_robot:
    type: "mecademic"
    ip_address: "192.168.0.101"

  dispenser_robot:
    type: "mecademic"
    ip_address: "192.168.0.102"

  feeder:
    type: "asyril"
    ip_address: "192.168.0.50"
    recipe: 63083

  io_board:
    type: "arduino"
    port: "COM3"

  planar:
    type: "planarmotor"
    ip_address: "192.168.10.200"

  remote_io:
    type: "iologik"
    ip_address: "192.168.127.254"

  sensor:
    type: "lmi"
    ip_address: "192.168.1.10"
```

---

## Web UI

The single-page UI at `http://localhost:5000` is divided into two tabs:

### Control tab

| Section | Description |
|---|---|
| **System Status** | Live state badge and last status message |
| **Tasks — Manual** | HOME, SHIPMENT, CALIBRATION (single-run) |
| **Tasks — Auto** | PROD (infinite loop), STOP (graceful), ABORT (immediate) |
| **System Control** | INITIALIZE, CLEAR FAULTS, SHUTDOWN |
| **Devices** | One card per device showing connected / ready / faulted badges and static info |

### Logs tab

Browse and auto-refresh any rotating log file from `logs/app/` or `logs/devices/`.

---

## REST API

### System

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/status` | Current `ControllerState` value |
| `GET` | `/api/info` | Static + live status of all devices |
| `GET` | `/api/state_values` | List all valid `ControllerState` enum values |
| `POST` | `/api/initialize` | Connect and home all devices → READY |
| `POST` | `/api/shutdown` | Graceful shutdown of all devices and threads → OFF |
| `POST` | `/api/clear_faults` | Reset faults on all devices |

### Tasks

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/task/home` | Run the HOME sequence (single run) |
| `POST` | `/api/task/shipment` | Run the SHIPMENT sequence (single run) |
| `POST` | `/api/task/calibration` | Run the CALIBRATION sequence (single run) |
| `POST` | `/api/task/prod` | Start the PROD loop (runs until stopped) |
| `POST` | `/api/stop` | Graceful stop — finish current cycle, then home |
| `POST` | `/api/abort` | Immediate abort — clear motion on all robots |

### Logs

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/logs` | List available log files grouped by category |
| `GET` | `/api/logs/<category>/<file>` | Read last N lines of a log file (query: `?lines=200`) |

---

## State Machine

```
                  initialize()
    OFF ─────────► INITIALIZING ─────────► READY
                                             │
                                start_task() │
                                             ▼
                    FAULTED ◄─────────────  BUSY
                       │                     │
        clear_faults() │    stop() / abort() │
                       ▼                     ▼
                     READY ◄──────────────  READY
```

- **OFF** — controller created but not initialised.
- **INITIALIZING** — connecting to devices and running homing sequences.
- **READY** — all devices are connected and idle; tasks can be started.
- **BUSY** — a task is currently running.
- **FAULTED** — a device fault was detected; must be cleared before resuming.

All state transitions are protected by a `threading.Lock`.

---

## Stop vs. Abort

| | STOP | ABORT |
|---|---|---|
| Finishes current cycle | ✅ | ❌ |
| Runs home sequence after | ✅ | ❌ |
| Calls `ClearMotion()` on robots | ❌ | ✅ |
| Unblocks `WaitIdle()` immediately | ❌ | ✅ |
| **Use case** | Planned end-of-run | Emergency / device fault |

A device fault detected by the monitor thread automatically triggers an **abort**.

---

## Adding a New Device

1. **Create the driver class** in `devices/` extending `Device`. Implement all abstract members:

   | Properties | Methods |
   |---|---|
   | `info` → `dict` | `initialize()` |
   | `connected` → `bool` | `shutdown()` |
   | `ready` → `bool` | `clear_fault()` |
   | `faulted` → `bool` | `abort()` |
   | `api` | |

2. **Export** the new class in [`devices/__init__.py`](devices/__init__.py).

3. **Register** the `type` string in `_create_devices()` in [`core/ApplicationController.py`](core/ApplicationController.py).

4. **Declare** one or more instances under `devices:` in [`config.yaml`](config.yaml).

---

## Logging

Every component writes to its own **rotating log file** (5 MB max, 2 backup files):

| Source | Log file |
|---|---|
| Flask server | `logs/app/app.log` |
| ApplicationController | `logs/app/ApplicationController.log` |
| Each device | `logs/devices/<device_id>.log` |

All log files use the format: `<timestamp> | <level> | <message>`.

Logs are viewable in the browser via the **Logs** tab or the `/api/logs` endpoints.