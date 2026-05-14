# app.py
import argparse
import sys
import os

# --- Workspace Setup ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

parser = argparse.ArgumentParser(description="Mecademic Demo App")
parser.add_argument('--workspace', type=str, default=BASE_DIR, help="Path to the external workspace")
args, _ = parser.parse_known_args()

workspace_path = os.path.abspath(args.workspace)
sys.path.insert(0, workspace_path)

from flask import Flask, render_template, jsonify, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from core.Task import TaskType
from core.ControllerState import ControllerState
from core.BackupRestoreService import BackupRestoreService

# --- Logging Setup ---
_APP_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "app")
os.makedirs(_APP_LOG_DIR, exist_ok=True)
logger = logging.getLogger("app")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    _handler = RotatingFileHandler(os.path.join(_APP_LOG_DIR, "app.log"), maxBytes=5*1024*1024, backupCount=2)
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
    app_logic_dir = os.path.join(workspace_path, "app_logic")
    if os.path.isdir(app_logic_dir):
        config_path = os.path.join(app_logic_dir, "config.yaml")
        prod_ctx_path = os.path.join(app_logic_dir, "production_context.yaml")
    else:
        config_path = os.path.join(workspace_path, "config.yaml")
        prod_ctx_path = os.path.join(workspace_path, "production_context.yaml")
    APPLICATION = ApplicationController(config_path=config_path, production_context_path=prod_ctx_path)
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
        def get_prod_context_snapshot(self):
            return {
                'state': ControllerState.OFF.value,
                'locked': False,
                'settings': {'storage_scope': 'memory', 'auto_persist': True, 'default_reset_events': []},
                'metadata': {},
                'metrics': {},
                'params': {},
                'variables': {},
            }
        def update_prod_context(self, payload): return ([], ['Production context unavailable'])
        def reset_prod_context(self, namespace, key=None, event=None): return None
    APPLICATION = MockApplicationController()

BACKUP_DIR = os.path.join(workspace_path, "backups")
BACKUP_RESTORE = BackupRestoreService(backup_dir=BACKUP_DIR, logger=logger)


def _parse_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


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


@app.route('/api/backup_restore/robots', methods=['GET'])
def get_backup_restore_robots():
    """Return Mecademic robots for backup/restore selectors."""
    try:
        robots = BACKUP_RESTORE.list_mecademic_robots(APPLICATION)
        connected_count = sum(1 for item in robots if item.get('connected'))
        return jsonify({'robots': robots, 'connected_count': connected_count, 'count': len(robots)}), 200
    except Exception as e:
        logger.error(f"Failed to list Mecademic robots: {e}", exc_info=True)
        return jsonify({'message': f'Failed to list Mecademic robots: {e}'}), 500


@app.route('/api/backup_restore/backup', methods=['POST'])
def run_backup():
    """Run backup in all or single mode."""
    payload = request.get_json(silent=True) or {}
    mode = str(payload.get('mode', 'all')).lower()
    robot_id = payload.get('robot_id')
    stop_on_error = _parse_bool(payload.get('stop_on_error'), default=False)

    if mode not in {'all', 'single'}:
        return jsonify({'message': "Invalid mode. Use 'all' or 'single'.", 'success': False}), 400
    if mode == 'single' and not robot_id:
        return jsonify({'message': 'robot_id is required for single mode.', 'success': False}), 400

    try:
        if mode == 'all':
            summary = BACKUP_RESTORE.backup_all(APPLICATION, stop_on_error=stop_on_error)
        else:
            summary = BACKUP_RESTORE.backup_one(APPLICATION, robot_id=robot_id)

        for item in summary.get('results', []):
            filename = item.get('archive_filename')
            if filename:
                item['download_url'] = url_for('download_backup_archive', filename=filename)

        summary['success'] = summary.get('failure_count', 0) == 0
        summary['message'] = 'Backup completed.' if summary['success'] else 'Backup completed with failures.'
        return jsonify(summary), 200
    except Exception as e:
        logger.error(f"Backup operation failed: {e}", exc_info=True)
        return jsonify({'message': f'Backup operation failed: {e}', 'success': False}), 500


@app.route('/api/backup_restore/restore', methods=['POST'])
def run_restore():
    """Restore one archive to a selected Mecademic robot."""
    robot_id = request.form.get('robot_id')
    stop_on_error = _parse_bool(request.form.get('stop_on_error'), default=False)
    dry_run = _parse_bool(request.form.get('dry_run'), default=False)
    archive_file = request.files.get('archive')

    if not robot_id:
        return jsonify({'message': 'robot_id is required.', 'success': False}), 400
    if archive_file is None or not archive_file.filename:
        return jsonify({'message': 'archive file is required.', 'success': False}), 400

    filename = secure_filename(archive_file.filename)
    if not filename.lower().endswith('.zip'):
        return jsonify({'message': 'Only .zip archives are supported.', 'success': False}), 400

    upload_dir = Path(BACKUP_DIR) / 'uploads'
    upload_dir.mkdir(parents=True, exist_ok=True)
    temp_path = upload_dir / f"upload_{filename}"

    try:
        archive_file.save(temp_path)
        result = BACKUP_RESTORE.restore_single_from_archive(
            APPLICATION,
            robot_id=robot_id,
            archive_path=str(temp_path),
            dry_run=dry_run,
            stop_on_error=stop_on_error,
        )
        result['success'] = bool(result.get('success'))
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Restore operation failed: {e}", exc_info=True)
        return jsonify({'message': f'Restore operation failed: {e}', 'success': False}), 500
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception as cleanup_err:
            logger.warning(f"Failed to remove temporary upload {temp_path}: {cleanup_err}")


@app.route('/api/backup_restore/download/<path:filename>', methods=['GET'])
def download_backup_archive(filename):
    """Download a generated backup archive from the managed backups directory."""
    safe_name = secure_filename(filename)
    if safe_name != filename:
        return jsonify({'message': 'Invalid archive name.'}), 400

    target_path = (Path(BACKUP_DIR) / safe_name).resolve()
    root = Path(BACKUP_DIR).resolve()
    if not str(target_path).startswith(str(root)):
        return jsonify({'message': 'Invalid archive path.'}), 400
    if not target_path.exists():
        return jsonify({'message': 'Archive not found.'}), 404

    return send_from_directory(str(root), safe_name, as_attachment=True)


@app.route('/api/prod/context', methods=['GET'])
def get_prod_context():
    """Return production params/variables, settings and runtime metadata."""
    try:
        return jsonify(APPLICATION.get_prod_context_snapshot()), 200
    except Exception as e:
        logger.error(f"Failed to get production context: {e}", exc_info=True)
        return jsonify({'message': f'Failed to get production context: {e}'}), 500


@app.route('/api/prod/runs', methods=['GET'])
def get_prod_runs():
    """Return archived production run summaries for export or reporting."""
    try:
        snapshot = APPLICATION.get_prod_context_snapshot()
        metrics = snapshot.get('metrics', {}) if isinstance(snapshot, dict) else {}
        runs = metrics.get('run_history', []) if isinstance(metrics, dict) else []
        return jsonify({'runs': runs, 'count': len(runs)}), 200
    except Exception as e:
        logger.error(f"Failed to get production run history: {e}", exc_info=True)
        return jsonify({'message': f'Failed to get production run history: {e}'}), 500


@app.route('/api/prod/context', methods=['PATCH'])
def patch_prod_context():
    """Update production context values/settings when controller is not BUSY."""
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({'message': 'Invalid payload format.', 'success': False}), 400

    try:
        updated, errors = APPLICATION.update_prod_context(payload)
        if errors:
            return jsonify({'message': 'Production context updated with errors.', 'success': False, 'updated': updated, 'errors': errors}), 400
        return jsonify({'message': 'Production context updated.', 'success': True, 'updated': updated}), 200
    except Exception as e:
        logger.error(f"Failed to update production context: {e}", exc_info=True)
        return jsonify({'message': f'Failed to update production context: {e}', 'success': False}), 500


@app.route('/api/prod/context/reset', methods=['POST'])
def reset_prod_context():
    """Reset production context values to defaults, optionally filtered by event/key."""
    payload = request.get_json(silent=True) or {}
    namespace = payload.get('namespace', 'all')
    key = payload.get('key')
    event = payload.get('event')

    try:
        APPLICATION.reset_prod_context(namespace=namespace, key=key, event=event)
        return jsonify({'message': 'Production context reset applied.', 'success': True}), 200
    except Exception as e:
        logger.error(f"Failed to reset production context: {e}", exc_info=True)
        return jsonify({'message': f'Failed to reset production context: {e}', 'success': False}), 400


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