# Mecademic Demo App

A Flask-based web application for controlling a multi-device robotic cell (Mecademic robots, Asyril Eye+ feeder, Planar Motor system). Provides a web UI and REST API to initialize, monitor, and run tasks across all devices.

---

## Features

- **Multi-device architecture** — abstract `Device` base class; each device has its own rotating log file
- **Web UI** — real-time status, manual/auto task controls, device cards with live badges, integrated log viewer
- **REST API** — full set of endpoints for status, task management, initialization, shutdown, and fault clearing
- **Thread-safe state machine** — `ControllerState` (OFF → INITIALIZING → READY → BUSY → FAULTED) with lock-protected transitions
- **Task system** — background `Task` thread supports PROD loop, HOME, SHIPMENT, CALIBRATION
- **Graceful stop vs. immediate abort** — STOP lets the current cycle finish; ABORT calls `ClearMotion()` on all robots to unblock `WaitIdle()` instantly
- **Fault monitoring** — background thread polls every 200 ms; any device fault automatically aborts the running task and sets FAULTED state
- **Async vision** — `AsyrilEyePlusApi.get_part_async()` returns a `Future` so the robot can move to the pick position while the feeder searches for a part in parallel

---

## Project Structure

```
app.py                        # Flask server and all REST API endpoints
ApplicationController.py      # Orchestrates all devices, tasks, and the monitor thread
ControllerState.py            # ControllerState enum (OFF, INITIALIZING, READY, BUSY, FAULTED)
Task.py                       # Task thread — TaskType enum + execution logic for each task
config.yaml                   # Device configuration (type, IP address, recipe, …)
requirements.txt              # Python dependencies

devices/
    Device.py                 # Abstract base class for all devices
    MecaRobot.py              # Mecademic robot wrapper (mecademicpy)
    Asyril.py                 # Asyril Eye+ feeder wrapper
    PlanarMotor.py            # Planar Motor system wrapper
    api/
        AsyrilAPI.py          # Low-level socket API for the Asyril Eye+
        PlanarMotorApi.py     # Low-level API for the Planar Motor system

demo_code/
    home.py                   # HOME task implementation
    shipment.py               # SHIPMENT task implementation
    prod.py                   # PROD cycle implementation
    calib.py                  # CALIBRATION task implementation

templates/
    index.html                # Single-page web UI

accessories_api/              # Optional stand-alone accessory drivers (Arduino, Zaber, …)
app_logs/                     # Rotating logs for the ApplicationController and Flask app
device_logs/                  # Per-device rotating log files
```

---

## Requirements

- Python 3.10+ (uses `match`/`case` and `X | Y` type hints)
- See `requirements.txt` for the full pinned dependency list

Quick install:
```bash
pip install -r requirements.txt
```

---

## Configuration

All devices are declared in `config.yaml`:

```yaml
devices:
  meca_robot_1:
    type: "mecademic"
    ip_address: "192.168.0.100"

  asyril_1:
    type: "asyril"
    ip_address: "192.168.0.50"
    recipe: 63083

  planar_motor:
    type: "planarmotor"
    ip_address: "192.168.10.200"
```

Supported `type` values: `mecademic`, `asyril`, `planarmotor`.

---

## Usage

```bash
cd mecademic_demo_app
python app.py
```

The server starts on `http://0.0.0.0:5000`. Open `http://localhost:5000` in a browser.

On Windows you can also use `autostart.bat`.

---

## Web UI

The single-page UI is divided into two tabs:

### Control tab

| Section | Controls |
|---|---|
| **System Status** | Live state badge + last message log |
| **Tasks — Manual** | HOME, SHIPMENT, CALIBRATION (start a single run) |
| **Tasks — Auto** | PROD (infinite loop), STOP (finish current cycle then home), ABORT (interrupt immediately) |
| **System Control** | INITIALIZE, CLEAR FAULTS, SHUTDOWN |
| **Devices** | Live cards per device showing connected / ready / faulted badges and static info |

### Logs tab

Browse and auto-refresh any rotating log file from `app_logs/` or `device_logs/`.

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

1. Create a class in `devices/` that extends `Device` and implements all abstract methods: `info`, `connected`, `ready`, `faulted`, `api`, `initialize()`, `shutdown()`, `clear_fault()`, `abort()`.
2. Add the new `type` string to `_create_devices()` in `ApplicationController.py`.
3. Add an entry under `devices:` in `config.yaml`.

---

## License

MIT License
