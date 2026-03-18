# Mecademic Demo App

A Flask-based web application for controlling a robotic cell built around Mecademic robots. Provides a web UI and REST API to initialize, monitor, and run tasks across all devices.

---

## Features

- **Multi-device architecture** — abstract `Device` base class; each device has its own rotating log file
- **Mecademic robot support** — wraps [mecademicpy](https://github.com/Mecademic/mecademicpy); handles connection, homing, motion, fault detection, and clearing
- **Web UI** — real-time status, manual/auto task controls, device cards with live badges, integrated log viewer
- **REST API** — full set of endpoints for status, task management, initialization, shutdown, and fault clearing
- **Thread-safe state machine** — `ControllerState` (OFF → INITIALIZING → READY → BUSY → FAULTED) with lock-protected transitions
- **Task system** — background `Task` thread supports PROD loop, HOME, SHIPMENT, and CALIBRATION
- **Graceful stop vs. immediate abort** — STOP lets the current cycle finish; ABORT calls `ClearMotion()` on all robots to unblock `WaitIdle()` instantly
- **Fault monitoring** — background thread polls every 200 ms; any device fault automatically aborts the running task and sets FAULTED state

---

## Project Structure

```
app.py                        # Flask server and all REST API endpoints
config.yaml                   # Device configuration (type, IP address, …)
requirements.txt              # Python dependencies
autostart.bat                 # Windows quick-launch script

core/
    ApplicationController.py  # Orchestrates all devices, tasks, and the monitor thread
    ControllerState.py        # ControllerState enum (OFF, INITIALIZING, READY, BUSY, FAULTED)
    Task.py                   # Task thread — TaskType enum + execution logic for each task

devices/
    Device.py                 # Abstract base class — implementor contract for all devices
    MecaRobot.py              # Mecademic robot wrapper (mecademicpy)
    ArduinoBoard.py           # Arduino I/O board wrapper
    api/                      # Low-level device APIs

application_code/
    home.py                   # HOME task implementation
    shipment.py               # SHIPMENT task implementation
    prod.py                   # PROD cycle implementation
    calib.py                  # CALIBRATION task implementation

templates/
    index.html                # Single-page web UI (Jinja2 template)

static/
    css/app.css               # Application styles
    js/app.js                 # UI logic and REST polling

logs/
    app/                      # Rotating logs for ApplicationController and Flask
    devices/                  # Per-device rotating log files
```

---

## Requirements

- Python 3.10+ (uses `match`/`case` and `X | Y` type hints)
- See `requirements.txt` for the full dependency list

Quick install:
```bash
pip install -r requirements.txt
```

---

## Configuration

All devices are declared in `config.yaml`:

```yaml
devices:
  mirror_robot:
    type: "mecademic"
    ip_address: "192.168.0.101"

  dispenser_robot:
    type: "mecademic"
    ip_address: "192.168.0.102"

```

---

## Usage

```bash
cd mecademic_demo_app
python app.py
```

The server starts on `http://0.0.0.0:5000`. Open `http://localhost:5000` in a browser.

---

## Web UI

The single-page UI is divided into two tabs:

### Control tab

| Section | Controls |
|---|---|
| **System Status** | Live state badge + last message log |
| **Tasks — Manual** | HOME, SHIPMENT, CALIBRATION (single run) |
| **Tasks — Auto** | PROD (infinite loop), STOP (finish current cycle then home), ABORT (interrupt immediately) |
| **System Control** | INITIALIZE, CLEAR FAULTS, SHUTDOWN |
| **Devices** | Live card per device showing connected / ready / faulted badges and static info |

### Logs tab

Browse and auto-refresh any rotating log file from `logs/app/` or `logs/devices/`.

---

## REST API

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/status` | Current `ControllerState` value |
| `GET` | `/api/info` | Static + live status for all devices |
| `POST` | `/api/initialize` | Connect and home all devices |
| `POST` | `/api/task/<name>` | Start a task: `home`, `shipment`, `prod`, `calibration` |
| `POST` | `/api/stop` | Graceful stop — current cycle finishes, then goes home |
| `POST` | `/api/abort` | Immediate abort — clears motion on all robots right away |
| `POST` | `/api/clear_faults` | Reset faults on all devices |
| `POST` | `/api/shutdown` | Graceful shutdown of all devices and threads |
| `GET` | `/api/logs` | List available log files grouped by directory |
| `GET` | `/api/logs/<category>/<file>` | Read last N lines of a log file (`?lines=200`) |

---

## Stop vs. Abort

| | STOP | ABORT |
|---|---|---|
| Finishes current cycle | ✅ | ❌ |
| Runs home sequence after | ✅ | ❌ |
| Calls `ClearMotion()` on robots | ❌ | ✅ |
| Unblocks `WaitIdle()` immediately | ❌ | ✅ |
| Use case | Planned end-of-run | Emergency / device fault |

A device fault detected by the monitor thread automatically triggers an **abort**.

---

## Adding a New Device

1. Create a class in `devices/` that extends `Device` and implements all abstract methods:
   `info`, `connected`, `ready`, `faulted`, `api`, `initialize()`, `shutdown()`, `clear_fault()`, `abort()`.
2. Add the new `type` string to `_create_devices()` in `core/ApplicationController.py`.
3. Add an entry under `devices:` in `config.yaml`.

---

## License

MIT License
