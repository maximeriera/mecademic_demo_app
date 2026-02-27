# app.py

from flask import Flask, render_template, jsonify, request
from ApplicationController import ApplicationController, TaskType, ControllerState 
import threading
import logging
from logging.handlers import RotatingFileHandler
import os

# --- Logging Setup ---
os.makedirs("app_logs", exist_ok=True)
logger = logging.getLogger("app")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    _handler = RotatingFileHandler("app_logs/app.log", maxBytes=5*1024*1024, backupCount=2)
    _handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(message)s'))
    logger.addHandler(_handler)

# --- Flask Setup ---
app = Flask(__name__)

# --- Robot Controller Instance (Singleton) ---
# Initialize the controller once outside the routes
# NOTE: Replace dummy config with your actual Meca 500 connection details
try:
    # We must start the controller in the main thread before starting Flask's server
    ROBOT = ApplicationController()
    logger.info("ApplicationController initialized successfully.")
except Exception as e:
    # If connection fails, set a permanent FAULT state
    logger.critical(f"Failed to initialize ApplicationController: {e}", exc_info=True)
    print(f"FATAL: Failed to initialize ApplicationController: {e}")
    class MockRobot:
        def get_state(self): return ControllerState.FAULTED
        def start_task(self, task): print("Mocked start task.")
        def stop_current_task(self): print("Mocked stop task.")
        def initialize(self): return True
        def shutdown(self): pass
        def get_devices_info(self): return {}
        def clear_faults(self): pass
    ROBOT = MockRobot()


# --- Flask Routes (API Endpoints) ---

@app.route('/')
def index():
    """Renders the main control page."""
    return render_template('index.html')

@app.route('/api/status', methods=['GET'])
def get_status():
    """API endpoint to check the robot's current state."""
    current_state = ROBOT.get_state().value
    logger.debug(f"GET /api/status -> {current_state}")
    return jsonify({'status': current_state})

@app.route('/api/task/<task_name>', methods=['POST'])
def handle_task(task_name):
    """API endpoint to start a specific task."""
    task_map = {
        'home': TaskType.HOME,
        'shipment': TaskType.SHIPMENT,
        'prod': TaskType.PROD
    }
    
    task_type = task_map.get(task_name)
    
    if task_type:
        success = ROBOT.start_task(task_type)
        if success:
            logger.info(f"Task started: {task_name.upper()}")
            return jsonify({'message': f'{task_name.upper()} task started.', 'success': True}), 200
        else:
            logger.warning(f"Could not start task '{task_name}'. Robot is {ROBOT.get_state().value}.")
            return jsonify({'message': f'Could not start task. Robot is {ROBOT.get_state().value}.', 'success': False}), 400
    else:
        logger.warning(f"Unknown task name requested: '{task_name}'")
        return jsonify({'message': 'Invalid task name.'}), 404
    
@app.route('/api/initialize', methods=['POST'])
def initialize_robot():
    """API endpoint to re-initialize the robot controller."""
    logger.info("POST /api/initialize - Initialization requested.")
    try:
        ROBOT.initialize()
        logger.info("Initialization successful.")
        return jsonify({'message': 'Robot initialization successful. State set to READY.', 'success': True}), 200
    except Exception as e:
        logger.error(f"Initialization failed: {e}", exc_info=True)
        return jsonify({'message': f'Initialization failed: {e}', 'success': False}), 500

@app.route('/api/shutdown', methods=['POST'])
def shutdown_system():
    """API endpoint to gracefully shut down the robot controller and monitoring threads."""
    logger.info("POST /api/shutdown - Shutdown requested via web interface.")
    ROBOT.shutdown()
    logger.info("Shutdown sequence completed.")
    return jsonify({'message': 'System shutdown sequence initiated. Controller threads stopped.', 'success': True}), 200

@app.route('/api/stop', methods=['POST'])
def stop_task():
    """API endpoint to stop the current task."""
    logger.info("POST /api/stop - Stop signal sent.")
    ROBOT.stop_current_task()
    return jsonify({'message': 'Stop signal sent.', 'success': True}), 200

@app.route('/api/info', methods=['GET'])
def get_robot_info():
    """API endpoint to get static device information as a list."""
    try:
        info_dict = ROBOT.get_devices_info()
        info_list = list(info_dict.values())
        logger.debug(f"GET /api/info -> {len(info_list)} device(s) returned.")
        return jsonify(info_list), 200
    except Exception as e:
        logger.error(f"Failed to retrieve device info: {e}", exc_info=True)
        return jsonify({'message': f'Failed to retrieve device info: {e}'}), 500

@app.route('/api/clear_faults', methods=['POST'])
def clear_faults():
    """API endpoint to clear faults on all devices."""
    logger.info("POST /api/clear_faults - Clear faults requested.")
    try:
        ROBOT.clear_faults()
        logger.info("Faults cleared successfully.")
        return jsonify({'message': 'Faults cleared.', 'success': True}), 200
    except Exception as e:
        logger.error(f"Failed to clear faults: {e}", exc_info=True)
        return jsonify({'message': f'Failed to clear faults: {e}', 'success': False}), 500

# --- Cleanup on Server Shutdown (Optional but Recommended) ---
# This ensures the monitor thread and any active task are properly stopped.

@app.teardown_appcontext
def shutdown_robot_controller(exception=None):
    if hasattr(threading.main_thread(), 'ROBOT'):
        # Ensure cleanup only runs once
        print("Flask context teardown: Shutting down robot controller.")
        ROBOT.shutdown()

if __name__ == '__main__':
    logger.info("Starting Flask server on 0.0.0.0:5000")
    app.run(debug=False, host='0.0.0.0', port=5000)