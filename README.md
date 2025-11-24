# Mecademic Demo App

A Flask-based web application for controlling a Mecademic Meca 500 robot. This project provides a simple web interface and REST API to initialize, control, and monitor the robot using the `mecademicpy` library.

## Features
- Web UI for robot control (see `templates/index.html`)
- REST API endpoints for robot status, task control, initialization, and shutdown
- Thread-safe robot state management
- Task execution (Home, Shipment, Production) with background threading
- Robust error handling and state transitions

## Project Structure
```
app.py                # Flask app and API endpoints
RobotController.py    # Main robot controller logic
Task.py               # Task thread implementation
TaskType.py           # Enum for task types
RobotState.py         # Enum for robot states
templates/index.html  # Web UI (Flask template)
README.md             # Project documentation
```

## Requirements
- Python 3.8+
- Flask
- mecademicpy (Mecademic Python API)

Install dependencies:
```bash
pip install flask mecademicpy
```

## Usage
1. **Configure Robot IP:**
	- Edit `app.py` and set `ROBOT_IP` to your Meca 500's IP address.

2. **Run the App:**
	```bash
	python app.py
	```
	The server will start on `http://0.0.0.0:5000`.

3. **Web Interface:**
	- Open your browser to `http://localhost:5000` to access the control UI.

4. **API Endpoints:**
	- `GET    /api/status`         — Get current robot state
	- `POST   /api/task/<task>`    — Start a task (`home`, `shipment`, `prod`)
	- `POST   /api/stop`           — Stop current task
	- `POST   /api/initialize`     — Re-initialize robot
	- `POST   /api/shutdown`       — Shutdown controller
	- `GET    /api/info`           — Get robot info

## Notes
- The robot controller uses a background thread to monitor robot status and manage state transitions.
- Task execution is handled in separate threads for responsiveness.
- If the robot cannot be initialized, a mock controller is used for safe testing.
- Update the placeholder logic in `Task.py` and `RobotController.py` with your actual Meca 500 commands as needed.

## License
MIT License
# mecademic_demo_app
repository for the demo app interface for Mecademic apps team  
