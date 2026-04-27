# app.py
from flask import Flask, render_template, jsonify, request

import logging
from logging.handlers import RotatingFileHandler

import os
from pathlib import Path

from core.Task import TaskType
from core.ControllerState import ControllerState

# --- Logging Setup ---
os.makedirs("logs/app", exist_ok=True)
logger = logging.getLogger("app")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    _handler = RotatingFileHandler("logs/app/app.log", maxBytes=5*1024*1024, backupCount=2)
    _handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(message)s'))
    logger.addHandler(_handler)

# --- Flask Setup ---
app = Flask(__name__)

# --- APPLICATION Controller Instance (Singleton) ---
# Initialize the controller once outside the routes
# NOTE: Replace dummy config with your actual Meca 500 connection details
try:
    # We must start the controller in the main thread before starting Flask's server
    from core.ApplicationController import ApplicationController
    APPLICATION = ApplicationController()
    logger.info("ApplicationController initialized successfully.")
except Exception as e:
    # If connection fails, set a permanent FAULT state
    logger.critical(f"Failed to initialize ApplicationController: {e}", exc_info=True)
    print(f"FATAL: Failed to initialize ApplicationController: {e}")
    class MockApplicationController:
        def get_state(self): return ControllerState.OFF
        def start_task(self, task): print("Mocked start task.")
        def stop_current_task(self): print("Mocked stop task.")
        def abort_current_task(self): print("Mocked abort task.")
        def initialize(self): return True
        def shutdown(self): pass
        def get_devices_info(self): return {}
        def clear_faults(self): pass
    APPLICATION = MockApplicationController()


# --- Flask Routes (API Endpoints) ---

@app.route('/')
def index():
    """Renders the main control page."""
    return render_template('index.html')

@app.route('/api/status', methods=['GET'])
def get_status():
    """API endpoint to check the APPLICATION's current state."""
    current_state = APPLICATION.get_state().value
    logger.debug(f"GET /api/status -> {current_state}")
    return jsonify({'status': current_state})

@app.route('/api/task/<task_name>', methods=['POST'])
def handle_task(task_name):
    """API endpoint to start a specific task."""
    task_map = {
        'home': TaskType.HOME,
        'shipment': TaskType.SHIPMENT,
        'prod': TaskType.PROD,
        'calibration': TaskType.CALIBRATION,
    }
    
    task_type = task_map.get(task_name)
    
    if task_type:
        success = APPLICATION.start_task(task_type)
        if success:
            logger.info(f"Task started: {task_name.upper()}")
            return jsonify({'message': f'{task_name.upper()} task started.', 'success': True}), 200
        else:
            logger.warning(f"Could not start task '{task_name}'. APPLICATION is {APPLICATION.get_state().value}.")
            return jsonify({'message': f'Could not start task. APPLICATION is {APPLICATION.get_state().value}.', 'success': False}), 400
    else:
        logger.warning(f"Unknown task name requested: '{task_name}'")
        return jsonify({'message': 'Invalid task name.'}), 404
    
@app.route('/api/initialize', methods=['POST'])
def initialize_APPLICATION():
    """API endpoint to re-initialize the APPLICATION controller."""
    logger.info("POST /api/initialize - Initialization requested.")
    try:
        APPLICATION.initialize()
        logger.info("Initialization successful.")
        return jsonify({'message': 'APPLICATION initialization successful. State set to READY.', 'success': True}), 200
    except Exception as e:
        logger.error(f"Initialization failed: {e}", exc_info=True)
        return jsonify({'message': f'Initialization failed: {e}', 'success': False}), 500

@app.route('/api/shutdown', methods=['POST'])
def shutdown_system():
    """API endpoint to gracefully shut down the APPLICATION controller and monitoring threads."""
    logger.info("POST /api/shutdown - Shutdown requested via web interface.")
    APPLICATION.shutdown()
    logger.info("Shutdown sequence completed.")
    return jsonify({'message': 'System shutdown sequence initiated. Controller threads stopped.', 'success': True}), 200

@app.route('/api/stop', methods=['POST'])
def stop_task():
    """API endpoint to stop the current task gracefully (finishes current cycle)."""
    logger.info("POST /api/stop - Stop signal sent.")
    APPLICATION.stop_current_task()
    return jsonify({'message': 'Stop signal sent. Current cycle will finish.', 'success': True}), 200

@app.route('/api/abort', methods=['POST'])
def abort_task():
    """API endpoint to abort the current task immediately."""
    logger.info("POST /api/abort - Abort signal sent.")
    APPLICATION.abort_current_task()
    return jsonify({'message': 'Abort signal sent. Task interrupted immediately.', 'success': True}), 200

@app.route('/api/info', methods=['GET'])
def get_APPLICATION_info():
    """API endpoint to get device information including live status."""
    try:
        info_list = []
        for device_id, device_info in APPLICATION.get_devices_info().items():
            # Enrich static info with live status fields
            entry = {'device_id': device_id}
            entry.update(device_info)
            # Attach live status if devices are accessible
            if hasattr(APPLICATION, 'devices') and device_id in APPLICATION.devices:
                dev = APPLICATION.devices[device_id]
                entry['connected'] = dev.connected
                entry['ready'] = dev.ready
                entry['faulted'] = dev.faulted
            info_list.append(entry)
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
        APPLICATION.clear_faults()
        logger.info("Faults cleared successfully.")
        return jsonify({'message': 'Faults cleared.', 'success': True}), 200
    except Exception as e:
        logger.error(f"Failed to clear faults: {e}", exc_info=True)
        return jsonify({'message': f'Failed to clear faults: {e}', 'success': False}), 500

@app.route('/api/state_values', methods=['GET'])
def get_state_values():
    """Returns all valid ControllerState values for the UI."""
    return jsonify([s.value for s in ControllerState])


# --- Log directories (resolved relative to this file so they work regardless of cwd) ---
_BASE_DIR = Path(__file__).parent
_LOG_DIRS = {
    'app':     _BASE_DIR / 'logs' / 'app',
    'devices': _BASE_DIR / 'logs' / 'devices',
}

@app.route('/api/logs', methods=['GET'])
def list_logs():
    """Returns a list of available log files grouped by directory."""
    result = {}
    for category, path in _LOG_DIRS.items():
        if path.exists():
            files = sorted(f.name for f in path.glob('*.log'))
        else:
            files = []
        result[category] = files
    return jsonify(result)

@app.route('/api/logs/<category>/<filename>', methods=['GET'])
def get_log(category, filename):
    """Returns the last N lines of a log file. Query param: ?lines=200"""
    if category not in _LOG_DIRS:
        return jsonify({'message': 'Unknown log category.'}), 404
    # Prevent path traversal
    log_path = (_LOG_DIRS[category] / filename).resolve()
    if not str(log_path).startswith(str(_LOG_DIRS[category].resolve())):
        return jsonify({'message': 'Invalid path.'}), 400
    if not log_path.exists():
        return jsonify({'message': 'Log file not found.'}), 404
    try:
        lines = int(request.args.get('lines', 200))
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.readlines()
        return jsonify({'filename': filename, 'lines': content[-lines:]}), 200
    except Exception as e:
        logger.error(f"Failed to read log {filename}: {e}", exc_info=True)
        return jsonify({'message': f'Failed to read log: {e}'}), 500


# --- Cleanup on Server Shutdown ---
# Uses a flag to ensure shutdown runs only once.
_shutdown_called = False

@app.teardown_appcontext
def shutdown_APPLICATION_controller(exception=None):
    global _shutdown_called
    if not _shutdown_called:
        _shutdown_called = True
        print("Flask context teardown: Shutting down APPLICATION controller.")
        logger.info("Flask context teardown: Shutting down APPLICATION controller.")
        APPLICATION.shutdown()

if __name__ == '__main__':
    logger.info("Starting Flask server on 0.0.0.0:5000")
    app.run(debug=False, host='0.0.0.0', port=5000)