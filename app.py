# app.py

from flask import Flask, render_template, jsonify
# Import your classes from the previous example
from RobotController import RobotController, TaskType, RobotState 
import threading

# --- Flask Setup ---
app = Flask(__name__)

# --- Robot Controller Instance (Singleton) ---
# Initialize the controller once outside the routes
# NOTE: Replace dummy config with your actual Meca 500 connection details
try:
    # We must start the controller in the main thread before starting Flask's server
    ROBOT = RobotController()
except Exception as e:
    # If connection fails, set a permanent FAULT state
    print(f"FATAL: Failed to initialize RobotController: {e}")
    class MockRobot:
        def get_state(self): return RobotState.FAULTED
        def start_task(self, task): print("Mocked start task.")
        def stop_current_task(self): print("Mocked stop task.")
        def initialize(self): return True
        def shutdown(self): pass
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
            return jsonify({'message': f'{task_name.upper()} task started.', 'success': True}), 200
        else:
            return jsonify({'message': f'Could not start task. Robot is {ROBOT.get_state().value}.', 'success': False}), 400
    else:
        return jsonify({'message': 'Invalid task name.'}), 404
    
@app.route('/api/initialize', methods=['POST'])
def initialize_robot():
    """API endpoint to re-initialize the robot controller."""
    try:
        ROBOT.initialize()
        return jsonify({'message': 'Robot initialization successful. State set to READY.', 'success': True}), 200
    except Exception as e:
        return jsonify({'message': f'Initialization failed: {e}', 'success': False}), 500

@app.route('/api/shutdown', methods=['POST'])
def shutdown_system():
    """API endpoint to gracefully shut down the robot controller and monitoring threads."""
    print("Received shutdown request via web interface.")
    
    # We execute the shutdown method
    ROBOT.shutdown()
    
    # NOTE: Since the controller is shut down, we can't rely on it to reset.
    # We will log the action and return a success message.
    return jsonify({'message': 'System shutdown sequence initiated. Controller threads stopped.', 'success': True}), 200

@app.route('/api/stop', methods=['POST'])
def stop_task():
    """API endpoint to stop the current task."""
    ROBOT.stop_current_task()
    return jsonify({'message': 'Stop signal sent.', 'success': True}), 200

@app.route('/api/info', methods=['GET'])
def get_robot_info():
    """API endpoint to get static robot information (now a list of devices)."""
    try:
        # This will now return a list of info dictionaries
        info_list = ROBOT.get_robot_info() 
        return jsonify(info_list), 200
    except Exception as e:
        return jsonify({'message': f'Failed to retrieve robot info: {e}'}), 500

# --- Cleanup on Server Shutdown (Optional but Recommended) ---
# This ensures the monitor thread and any active task are properly stopped.

@app.teardown_appcontext
def shutdown_robot_controller(exception=None):
    if hasattr(threading.main_thread(), 'ROBOT'):
        # Ensure cleanup only runs once
        print("Flask context teardown: Shutting down robot controller.")
        ROBOT.shutdown()

if __name__ == '__main__':
    # Flask runs in the main thread; ROBOT is ready.
    app.run(debug=False, host='0.0.0.0', port=5000)