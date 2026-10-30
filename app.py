# app.py
import argparse
import atexit
import signal
import sys
import os
import threading
from pathlib import Path

# --- Workspace Setup ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
PROJECT_ROOT = Path(BASE_DIR).resolve().parent
os.environ.setdefault("MECADEMIC_DEMO_ROOT", str(PROJECT_ROOT))

parser = argparse.ArgumentParser(description="Mecademic Demo App")
parser.add_argument('--workspace', type=str, default=BASE_DIR, help="Path to the external workspace")
parser.add_argument('--host', type=str, default='0.0.0.0', help="Interface the server binds to")
parser.add_argument('--port', type=int, default=5000, help="Port the server listens on")
parser.add_argument('--workspaces-root', type=str, default=None,
                    help="Folder whose subfolders the workspace picker lists "
                         "(default: MECADEMIC_WORKSPACES_ROOT, else the parent of --workspace)")
args, _ = parser.parse_known_args()

workspace_path = os.path.abspath(args.workspace)
sys.path.insert(0, workspace_path)

from flask import Flask, abort, render_template, jsonify, request, send_from_directory, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

import logging
import math
import time
import uuid

from core.Task import TaskType
from core.ControllerState import ControllerState
from core.ContextStore import ContextError
from core.BackupRestoreService import BackupRestoreService
from core.LogSetup import describe_logging, get_app_logger, request_id_var
from core.Workspaces import (config_dir_of, default_root, discover as discover_workspaces,
                             purge_modules as purge_workspace_modules, resolve as resolve_workspace)

# --- Logging Setup ---
# Format, levels, the combined log and the stdout stream all come from
# core.LogSetup so the Flask layer, controller and devices stay consistent.
logger = get_app_logger()

# --- Flask Setup ---
app = Flask(__name__)

# --- APPLICATION Controller Instance (Singleton) ---
# Built from the active workspace; rebuilt by switch_workspace() (Workspaces
# picker, Reload code). Routes read these globals at call time, so rebinding
# them is all a switch needs.

class MockApplicationController:
    """Stands in when the controller cannot be built, so the UI still loads and says why."""

    config_issues = []
    history = None
    workspace_name = None

    def __init__(self, load_error):
        self.load_error = load_error

    def get_state(self): return ControllerState.OFF
    def start_task(self, task): return False
    def start_manual_action(self, action_key): return False
    def stop_current_task(self): pass
    def abort_current_task(self): pass
    def initialize(self): raise RuntimeError(f'Controller failed to start: {self.load_error}')
    def shutdown(self): pass
    def close(self): pass
    def is_busy(self): return False
    def get_devices_info(self): return {}
    def get_manual_actions(self): return []
    def clear_faults(self): pass
    def get_live_step(self): return None
    def get_current_task_label(self): return None
    def get_prod_snapshot(self):
        return {'state': ControllerState.OFF.value, 'metrics': {}, 'steps': {}}
    def get_context_snapshot(self):
        return {
            'enabled': False,
            'source': None,
            'state_file': None,
            'load_error': 'Controller failed to start; see app.log.',
            'state': ControllerState.OFF.value,
            'locked': False,
            'params': {},
            'variables': {},
        }
    def update_context(self, payload): return ([], ['Context unavailable: controller failed to start'])
    def reset_context(self, namespace, key=None): raise RuntimeError('Controller failed to start')
    def get_detached(self): return {}
    def _unavailable(self, device_id):
        return {'success': False, 'message': 'Controller failed to start; see app.log.'}
    connect_device = disconnect_device = clear_device_fault = _unavailable


def _build_controller(ws_path):
    """Controller for the workspace at *ws_path*, or the mock with the reason it failed."""
    config_dir = config_dir_of(ws_path) or ws_path
    try:
        from core.ApplicationController import ApplicationController
        # The optional context file (context.yaml) is located by the controller,
        # relative to config.yaml, honouring `context_file:` in it.
        controller = ApplicationController(config_path=os.path.join(config_dir, "config.yaml"))
        logger.info("ApplicationController initialized successfully.")
        return controller, config_dir
    except Exception as e:
        logger.critical(f"Failed to initialize ApplicationController: {e}", exc_info=True)
        print(f"FATAL: Failed to initialize ApplicationController: {e}")
        return MockApplicationController(f"{type(e).__name__}: {e}"), config_dir


def _activate_workspace(ws_path):
    """Build the controller for *ws_path* and point every workspace global at it."""
    global workspace_path, workspace_config_dir, APPLICATION, BACKUP_DIR, BACKUP_RESTORE, CUSTOM_VIEW_DIR
    workspace_path = ws_path
    APPLICATION, workspace_config_dir = _build_controller(ws_path)
    BACKUP_DIR = os.path.join(ws_path, "backups")
    BACKUP_RESTORE = BackupRestoreService(backup_dir=BACKUP_DIR, logger=logger)
    # A workspace may ship custom_view/ next to its config.yaml (see below).
    CUSTOM_VIEW_DIR = os.path.join(workspace_config_dir, "custom_view")


_activate_workspace(workspace_path)

WORKSPACES_ROOT = (os.path.abspath(args.workspaces_root) if args.workspaces_root
                   else os.path.abspath(os.environ['MECADEMIC_WORKSPACES_ROOT'])
                   if os.environ.get('MECADEMIC_WORKSPACES_ROOT')
                   else default_root(workspace_path))

# Serialises workspace switches against each other and against process exit.
_switch_lock = threading.RLock()


class WorkspaceBusyError(RuntimeError):
    """A switch was refused because the cell is busy."""


def switch_workspace(name):
    """Shut the current controller down and start the workspace called *name*.

    In-process: the old workspace's modules are purged from sys.modules and
    its sys.path entry replaced, so the new (or edited) code is what runs.
    Switching to the current workspace is "Reload code". Raises ValueError for
    an unknown name, WorkspaceBusyError while a task runs.
    """
    global _last_logged_state, _last_logged_health, _step_read_failed, _task_read_failed
    with _switch_lock:
        target = resolve_workspace(WORKSPACES_ROOT, workspace_path, name)
        busy = getattr(APPLICATION, 'is_busy', None)
        if busy is not None and busy():
            raise WorkspaceBusyError('A task is running or the cell is initializing. Stop it first.')

        old_path = workspace_path
        logger.info(f"Switching workspace: {old_path} -> {target}")
        try:
            APPLICATION.shutdown()
        except Exception as e:
            logger.warning(f"Error shutting down the old controller: {e}", exc_info=True)
        try:
            APPLICATION.close()
        except Exception as e:
            logger.warning(f"Error closing the old controller: {e}", exc_info=True)

        purged = purge_workspace_modules([old_path, target])
        logger.info(f"Purged {len(purged)} workspace module(s): {', '.join(purged) or '-'}")
        # Replace only the entry this file inserted for the old workspace.
        if old_path in sys.path:
            sys.path.remove(old_path)
        sys.path.insert(0, target)

        _activate_workspace(target)
        with _state_log_lock:
            _last_logged_state = None
            _last_logged_health = None
        _step_read_failed = False
        _task_read_failed = False
        return {
            'success': not isinstance(APPLICATION, MockApplicationController),
            'workspace': os.path.basename(target),
            'controller_error': getattr(APPLICATION, 'load_error', None),
        }


def _task_refusal_message(task_type):
    """Why the controller refused *task_type*, for the 400 response."""
    reason = None
    getter = getattr(APPLICATION, 'task_refusal_reason', None)
    if getter is not None:
        try:
            reason = getter(task_type)
        except Exception:
            reason = None
    return reason or f'APPLICATION is {APPLICATION.get_state().value}.'


def _parse_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# --- Request logging -------------------------------------------------------
# The UI polls /api/status at 10 Hz and /api/info at 1 Hz. Logging a line per
# request made those two endpoints ~92% of app.log, so a 5 MB x2 rotation held
# well under an hour of history and any real incident scrolled away. These
# endpoints are therefore excluded from per-request logging; what actually
# matters about them — the controller state and device health — is logged by
# _log_state_change() / _log_device_health_change() only when it *changes*.
_POLL_PATHS = frozenset({'/api/status', '/api/info', '/api/prod/metrics', '/api/context',
                         '/api/prod/timelines', '/api/system'})

_last_logged_state = None
_last_logged_health = None
_step_read_failed = False   # log a live-step read failure once, not 10x/second
_task_read_failed = False   # same, for the running task's name
_state_log_lock = threading.Lock()


def _is_noisy_path(path):
    return (path in _POLL_PATHS or path.startswith('/static/') or path.startswith('/api/logs/')
            or path.startswith('/api/data') or path.startswith('/view/')
            or (path.startswith('/api/devices/') and path.endswith('/live')))


def _log_state_change(state):
    """Log the controller state only when it differs from the last logged one."""
    global _last_logged_state
    with _state_log_lock:
        if state == _last_logged_state:
            return
        previous, _last_logged_state = _last_logged_state, state
    logger.info("Controller state observed: %s -> %s", previous or '(initial)', state)


def _log_device_health_change(entries):
    """Log device connected/ready/faulted only when the overall picture changes."""
    global _last_logged_health
    snapshot = tuple(
        (e.get('device_id'), bool(e.get('connected')), bool(e.get('ready')), bool(e.get('faulted')))
        for e in entries
    )
    with _state_log_lock:
        if snapshot == _last_logged_health:
            return
        _last_logged_health = snapshot
    if not snapshot:
        logger.info("Device health: no devices configured.")
        return
    logger.info(
        "Device health changed: %s",
        ' | '.join(
            f"{d}(conn={'Y' if c else 'N'},ready={'Y' if r else 'N'},fault={'Y' if f else 'N'})"
            for d, c, r, f in snapshot
        ),
    )


@app.before_request
def _assign_request_id():
    """Tag each request so its log lines can be correlated.

    The id is stored in a ContextVar that the log formatter renders as
    ``[req=ab12cd34]``. It is thread-local, so it does not bleed into the
    monitor or task threads — those are identified by thread name instead.
    """
    request._mecademic_token = request_id_var.set(uuid.uuid4().hex[:8])
    request._mecademic_start = time.perf_counter()


@app.after_request
def _log_request(response):
    """Log one line per meaningful request: method, path, status, duration."""
    started = getattr(request, '_mecademic_start', None)
    if started is not None and not _is_noisy_path(request.path):
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        log = logger.warning if response.status_code >= 400 else logger.info
        log("%s %s -> %s in %.1f ms", request.method, request.path,
            response.status_code, elapsed_ms)
    return response


@app.teardown_request
def _clear_request_id(exception=None):
    token = getattr(request, '_mecademic_token', None)
    if token is not None:
        request_id_var.reset(token)


@app.errorhandler(Exception)
def _log_unhandled_exception(error):
    """Never let an unhandled error reach the client without a logged traceback."""
    if isinstance(error, HTTPException):
        return error
    logger.error("Unhandled error on %s %s: %s", request.method, request.path, error,
                 exc_info=True)
    return jsonify({'message': f'Internal server error: {error}', 'success': False}), 500


# --- Flask Routes (API Endpoints) ---

@app.route('/')
def index():
    """Renders the main control page."""
    return render_template('index.html')

@app.route('/api/status', methods=['GET'])
def get_status():
    """API endpoint to check the APPLICATION's current state."""
    current_state = APPLICATION.get_state().value
    # Polled at 10 Hz; log only on change (see _log_state_change).
    _log_state_change(current_state)

    # The live step rides along here rather than on a new endpoint: /api/status
    # is already polled at 10 Hz on every tab and already excluded from request
    # logging via _POLL_PATHS, so this costs no new timer, route or log noise.
    #
    # This route is also the Docker healthcheck probe (see docker-compose.yml),
    # so step tracking must never be able to fail it, and must never log 10
    # lines/second when the controller has no step support.
    global _step_read_failed
    step = None
    try:
        getter = getattr(APPLICATION, 'get_live_step', None)
        if getter is not None:
            step = getter()
    except Exception as e:
        if not _step_read_failed:
            _step_read_failed = True
            logger.error("Live step read failed; step info suppressed: %s", e, exc_info=True)
    # Same defensive pattern for the running task's name (shown in the status box).
    global _task_read_failed
    task = None
    try:
        task_getter = getattr(APPLICATION, 'get_current_task_label', None)
        if task_getter is not None:
            task = task_getter()
    except Exception as e:
        if not _task_read_failed:
            _task_read_failed = True
            logger.error("Current task read failed; task name suppressed: %s", e, exc_info=True)
    # The workspace name lets every open page notice a switch made elsewhere and reload.
    return jsonify({'status': current_state, 'step': step, 'task': task,
                    'workspace': os.path.basename(os.path.normpath(workspace_path))})

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
            reason = _task_refusal_message(task_type)
            logger.warning(f"Could not start task '{task_name}'. {reason}")
            return jsonify({'message': f'Could not start task. {reason}', 'success': False}), 400
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


@app.route('/api/manual/actions', methods=['GET'])
def get_manual_actions():
    """Return configured manual runtime actions for the Manual tab."""
    try:
        actions = APPLICATION.get_manual_actions()
        return jsonify({'actions': actions, 'count': len(actions), 'success': True}), 200
    except Exception as e:
        logger.error(f"Failed to load manual actions: {e}", exc_info=True)
        return jsonify({'message': f'Failed to load manual actions: {e}', 'actions': [], 'success': False}), 500


@app.route('/api/manual/actions/<action_key>/run', methods=['POST'])
def run_manual_action(action_key):
    """Start one configured manual action as an exclusive task."""
    action_key = str(action_key or '').strip()
    if not action_key:
        return jsonify({'message': 'Manual action key is required.', 'success': False}), 400

    success = APPLICATION.start_manual_action(action_key)
    if success:
        logger.info(f"Manual action started: {action_key}")
        return jsonify({'message': f"Manual action '{action_key}' started.", 'success': True}), 200

    state = APPLICATION.get_state().value
    available = []
    try:
        available = [item.get('key') for item in APPLICATION.get_manual_actions()]
    except Exception:
        available = []
    if action_key not in available:
        return jsonify({'message': f"Unknown manual action '{action_key}'.", 'success': False}), 404

    return jsonify({'message': f'Could not start manual action. {_task_refusal_message(TaskType.MANUAL_ACTION)}',
                    'success': False, 'state': state}), 400

@app.route('/api/info', methods=['GET'])
def get_APPLICATION_info():
    """API endpoint to get device information including live status."""
    try:
        info_list = []
        detached = APPLICATION.get_detached()
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
                try:
                    entry['link'] = dev.link
                except Exception:
                    entry['link'] = 'disconnected'
                entry['detached'] = device_id in detached
                entry['in_transition'] = APPLICATION.is_in_transition(device_id)
                entry['supports_reconnect'] = bool(getattr(dev, 'supports_reconnect', False))
                entry['supports_monitoring'] = bool(getattr(dev, 'supports_monitoring', False))
                entry['has_live_data'] = bool(getattr(dev, 'has_live_data', False))
            info_list.append(entry)
        # Polled at 1 Hz; log only when device health actually changes.
        _log_device_health_change(info_list)
        return jsonify(info_list), 200
    except Exception as e:
        logger.error(f"Failed to retrieve device info: {e}", exc_info=True)
        return jsonify({'message': f'Failed to retrieve device info: {e}'}), 500

@app.route('/api/clear_faults', methods=['POST'])
def clear_faults():
    """API endpoint to clear faults on all devices."""
    logger.info("POST /api/clear_faults - Clear faults requested.")
    try:
        result = APPLICATION.clear_faults() or {}
        # Older/mock controllers return None; treat that as a plain success.
        if not isinstance(result, dict) or 'success' not in result:
            logger.info("Faults cleared successfully.")
            return jsonify({'message': 'Faults cleared.', 'success': True}), 200

        if result['success']:
            logger.info("Faults cleared successfully. Controller is %s.", result.get('state'))
            return jsonify({
                'message': f"Faults cleared. Controller is {result.get('state')}.",
                'success': True,
                **result,
            }), 200

        # Report *which* devices are still unhealthy rather than claiming success.
        problems = []
        if result.get('still_faulted'):
            problems.append('still faulted: ' + ', '.join(result['still_faulted']))
        if result.get('errors'):
            problems.append('errors: ' + '; '.join(f'{k}: {v}' for k, v in result['errors'].items()))
        detail = ' | '.join(problems) or 'cell did not return to a healthy state'
        logger.warning("Clear faults incomplete - %s", detail)
        return jsonify({
            'message': f"Faults not fully cleared ({detail}). Controller is {result.get('state')}.",
            'success': False,
            **result,
        }), 200
    except Exception as e:
        logger.error(f"Failed to clear faults: {e}", exc_info=True)
        return jsonify({'message': f'Failed to clear faults: {e}', 'success': False}), 500

def _device_link_response(result):
    """Map a controller link-action result to an HTTP response.

    Refusals (task running, unknown device...) are 409/404; a device error
    after the action ran is still 200 with ``success: false``, so the UI shows
    the message rather than a transport error.
    """
    if result.get('success'):
        return jsonify(result), 200
    if 'link' in result:
        return jsonify(result), 200
    status = 404 if str(result.get('message', '')).startswith('Unknown device') else 409
    return jsonify(result), status


@app.route('/api/devices/<device_id>/connect', methods=['POST'])
def connect_device(device_id):
    """Bring one device (back) under this app's control."""
    return _device_link_response(APPLICATION.connect_device(device_id))


@app.route('/api/devices/<device_id>/disconnect', methods=['POST'])
def disconnect_device(device_id):
    """Release one device (a robot keeps a monitoring link)."""
    return _device_link_response(APPLICATION.disconnect_device(device_id))


@app.route('/api/devices/<device_id>/clear_fault', methods=['POST'])
def clear_device_fault(device_id):
    """Clear the fault of one device."""
    result = APPLICATION.clear_device_fault(device_id)
    if result.get('success'):
        return jsonify(result), 200
    status = 404 if str(result.get('message', '')).startswith('Unknown device') else 409
    return jsonify(result), status


@app.route('/api/devices/<device_id>/live', methods=['GET'])
def get_device_live(device_id):
    """Telemetry and I/O points of one device (Devices tab, polled only while its panel is open)."""
    getter = getattr(APPLICATION, 'get_device_live', None)
    live = getter(device_id) if getter else None
    if live is None:
        return jsonify({'message': f"Unknown device '{device_id}'."}), 404
    return jsonify(_jsonable(live)), 200


@app.route('/api/devices/<device_id>/io', methods=['POST'])
def write_device_io(device_id):
    """Set one writable I/O point: ``{"name": "do3", "value": true}``."""
    payload = request.get_json(silent=True) or {}
    name = str(payload.get('name') or '').strip()
    if not name or 'value' not in payload:
        return jsonify({'message': "Payload needs 'name' and 'value'.", 'success': False}), 400
    writer = getattr(APPLICATION, 'write_device_io', None)
    if writer is None:
        return jsonify({'message': 'Controller failed to start; see app.log.', 'success': False}), 409
    result = writer(device_id, name, payload['value'])
    if result.get('success'):
        return jsonify(result), 200
    if str(result.get('message', '')).startswith('Unknown device'):
        return jsonify(result), 404
    status = 400 if result.pop('bad_request', False) else 409
    return jsonify(result), status


@app.route('/api/system', methods=['GET'])
def get_system():
    """The workspace in use and the problems found in its config.yaml."""
    return jsonify({
        'workspace': os.path.basename(os.path.normpath(workspace_path)),
        'workspace_path': workspace_path,
        'config_issues': list(getattr(APPLICATION, 'config_issues', None) or []),
        'controller_error': getattr(APPLICATION, 'load_error', None),
    }), 200


@app.route('/api/workspaces', methods=['GET'])
def list_workspaces():
    """Workspaces the picker offers: subfolders of the workspaces root holding a config.yaml."""
    return jsonify({
        'current': os.path.basename(os.path.normpath(workspace_path)),
        'root': WORKSPACES_ROOT,
        'workspaces': discover_workspaces(WORKSPACES_ROOT, workspace_path),
    }), 200


@app.route('/api/workspaces/switch', methods=['POST'])
def switch_workspace_route():
    """Load another workspace (or reload the current one): ``{"name": "<folder name>"}``.

    Disconnects every device; the new workspace starts OFF and needs INITIALIZE.
    """
    payload = request.get_json(silent=True) or {}
    name = str(payload.get('name') or '').strip()
    try:
        result = switch_workspace(name)
    except WorkspaceBusyError as e:
        return jsonify({'message': str(e), 'success': False}), 409
    except ValueError as e:
        return jsonify({'message': str(e), 'success': False}), 400
    if result['success']:
        result['message'] = f"Workspace '{result['workspace']}' loaded. Initialize to connect its devices."
    else:
        result['message'] = (f"Workspace '{result['workspace']}' failed to start: "
                             f"{result['controller_error']}")
    return jsonify(result), 200


@app.route('/api/devices/types', methods=['GET'])
def get_device_types():
    """Every device type config.yaml accepts, with its keys (from devices.registry)."""
    from devices.registry import describe_types
    return jsonify({'types': describe_types()}), 200


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


@app.route('/api/prod/metrics', methods=['GET'])
def get_prod_metrics():
    """Return PROD cycle/run metrics and the step tracker state for the Production tab."""
    try:
        return jsonify(APPLICATION.get_prod_snapshot()), 200
    except Exception as e:
        logger.error(f"Failed to get production metrics: {e}", exc_info=True)
        return jsonify({'message': f'Failed to get production metrics: {e}'}), 500


@app.route('/api/prod/timelines', methods=['GET'])
def get_prod_timelines():
    """Last finished PROD cycles with their step offsets, for the Gantt chart.

    ``?after_rev=N`` answers ``{"rev": N, "unchanged": true}`` when nothing
    finished since revision N, so the 1 Hz Production tab poll stays cheap.
    """
    getter = getattr(APPLICATION, 'get_prod_timelines', None)
    if getter is None:
        return jsonify({'rev': 0, 'order': [], 'timelines': [], 'avg_s': {}}), 200
    try:
        data = getter()
    except Exception as e:
        logger.error(f"Failed to get cycle timelines: {e}", exc_info=True)
        return jsonify({'message': f'Failed to get cycle timelines: {e}'}), 500
    after = request.args.get('after_rev', type=int)
    if after is not None and after == data.get('rev'):
        return jsonify({'rev': after, 'unchanged': True}), 200
    return jsonify(_jsonable(data)), 200


def _history_store():
    return getattr(APPLICATION, 'history', None)


def _csv_response(text, filename):
    from flask import Response
    return Response(text, mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@app.route('/api/history/days', methods=['GET'])
def get_history_days():
    """Per-day cycle summary from the production history on disk."""
    store = _history_store()
    if store is None:
        return jsonify({'enabled': False, 'days': []}), 200
    return jsonify({'enabled': True, 'workspace': getattr(APPLICATION, 'workspace_name', None),
                    'days': store.days()}), 200


@app.route('/api/history/runs', methods=['GET'])
def get_history_runs():
    """Archived runs from disk, newest last: ``?limit=50``."""
    store = _history_store()
    if store is None:
        return jsonify({'enabled': False, 'runs': []}), 200
    limit = max(1, min(request.args.get('limit', default=50, type=int) or 50, 1000))
    return jsonify({'enabled': True, 'runs': _jsonable(store.runs(limit=limit))}), 200


@app.route('/api/history/runs.csv', methods=['GET'])
def get_history_runs_csv():
    store = _history_store()
    if store is None:
        return jsonify({'message': 'Production history is disabled.'}), 404
    name = getattr(APPLICATION, 'workspace_name', 'workspace')
    return _csv_response(store.runs_csv(), f'{secure_filename(name)}_runs.csv')


@app.route('/api/history/cycles.csv', methods=['GET'])
def get_history_cycles_csv():
    """Every cycle of one day: ``?date=YYYY-MM-DD``."""
    store = _history_store()
    if store is None:
        return jsonify({'message': 'Production history is disabled.'}), 404
    day = request.args.get('date', '')
    try:
        text = store.cycles_csv(day)
    except ValueError as e:
        return jsonify({'message': str(e)}), 400
    name = getattr(APPLICATION, 'workspace_name', 'workspace')
    return _csv_response(text, f'{secure_filename(name)}_cycles_{day}.csv')


@app.route('/api/prod/runs', methods=['GET'])
def get_prod_runs():
    """Return archived production run summaries for export or reporting."""
    try:
        snapshot = APPLICATION.get_prod_snapshot()
        metrics = snapshot.get('metrics', {}) if isinstance(snapshot, dict) else {}
        runs = metrics.get('run_history', []) if isinstance(metrics, dict) else []
        return jsonify({'runs': runs, 'count': len(runs)}), 200
    except Exception as e:
        logger.error(f"Failed to get production run history: {e}", exc_info=True)
        return jsonify({'message': f'Failed to get production run history: {e}'}), 500


@app.route('/api/context', methods=['GET'])
def get_context():
    """Return the optional params/variables context (``enabled: false`` without a context file)."""
    try:
        return jsonify(APPLICATION.get_context_snapshot()), 200
    except Exception as e:
        logger.error(f"Failed to get context: {e}", exc_info=True)
        return jsonify({'message': f'Failed to get context: {e}'}), 500


@app.route('/api/context', methods=['PATCH'])
def patch_context():
    """Update param/variable values. Refused while the controller is BUSY."""
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({'message': 'Invalid payload format.', 'success': False}), 400

    try:
        updated, errors = APPLICATION.update_context(payload)
        if errors:
            return jsonify({'message': 'Context updated with errors.', 'success': False, 'updated': updated, 'errors': errors}), 400
        return jsonify({'message': 'Context updated.', 'success': True, 'updated': updated}), 200
    except Exception as e:
        logger.error(f"Failed to update context: {e}", exc_info=True)
        return jsonify({'message': f'Failed to update context: {e}', 'success': False}), 500


@app.route('/api/context/reset', methods=['POST'])
def reset_context():
    """Reset params/variables to their defaults: one key, one namespace, or all."""
    payload = request.get_json(silent=True) or {}
    namespace = payload.get('namespace', 'all')
    key = payload.get('key')

    try:
        APPLICATION.reset_context(namespace=namespace, key=key)
        return jsonify({'message': 'Context reset applied.', 'success': True}), 200
    except ContextError as e:
        # Expected refusals (locked while BUSY, unknown key): no traceback.
        logger.warning(f"Context reset refused: {e}")
        return jsonify({'message': f'Context reset refused: {e}', 'success': False}), 400
    except Exception as e:
        logger.error(f"Failed to reset context: {e}", exc_info=True)
        return jsonify({'message': f'Failed to reset context: {e}', 'success': False}), 400


# --- Custom view (optional, per workspace) ----------------------------------
# A workspace may ship custom_view/ next to its config.yaml: a Python module
# (data sources + analysis, loaded by the controller) and a web page served
# here. The standard UI only links to it.


def _data_hub():
    return getattr(getattr(APPLICATION, 'context', None), 'data', None)


def _jsonable(value):
    """Coerce workspace-published values (numpy, tuples, NaN) into strict JSON."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # JSON.parse rejects NaN/Infinity, which would break the whole response.
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, 'tolist'):
        return _jsonable(value.tolist())
    try:
        return _jsonable(float(value))
    except (TypeError, ValueError):
        return str(value)


@app.route('/api/data', methods=['GET'])
def get_data_manifest():
    """Custom view manifest: whether it is available/running, and its channels."""
    hub = _data_hub()
    manifest = hub.describe() if hub is not None else {'active': False, 'setup_error': None, 'channels': {}}
    manifest['view_available'] = os.path.isfile(os.path.join(CUSTOM_VIEW_DIR, 'index.html'))
    return jsonify(_jsonable(manifest)), 200


@app.route('/api/data/read', methods=['GET'])
def read_data():
    """Samples stored after ``after`` for each ``ch``: ``/api/data/read?ch=a&ch=b&after=1234``.

    Pass the returned ``cursor`` back as ``after`` on the next poll.
    """
    channels = request.args.getlist('ch')
    after = request.args.get('after', type=int)
    hub = _data_hub()
    if hub is None:
        return jsonify({'now': None, 'cursor': after, 'data': {ch: [] for ch in channels}}), 200
    cursor, data = hub.read(channels, after)
    return jsonify({'now': hub.now(), 'cursor': cursor, 'data': _jsonable(data)}), 200


@app.route('/view/')
@app.route('/view/<path:filename>')
def custom_view(filename='index.html'):
    """Serve the workspace's custom view page and its assets (never its Python code)."""
    if filename.endswith(('.py', '.pyc')) or '__pycache__' in filename.split('/'):
        abort(404)
    if not os.path.isdir(CUSTOM_VIEW_DIR):
        abort(404)
    return send_from_directory(CUSTOM_VIEW_DIR, filename)


# --- Log directories (resolved relative to this file so they work regardless of cwd) ---
_BASE_DIR = Path(os.environ.get("MECADEMIC_DEMO_ROOT", str(PROJECT_ROOT)))
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

@app.route('/api/logs/config', methods=['GET'])
def get_log_config():
    """Report the active logging configuration (levels, paths, format)."""
    try:
        return jsonify(describe_logging()), 200
    except Exception as e:
        logger.error(f"Failed to describe logging config: {e}", exc_info=True)
        return jsonify({'message': f'Failed to describe logging config: {e}'}), 500


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
        try:
            lines = max(1, min(int(request.args.get('lines', 200)), 10000))
        except (TypeError, ValueError):
            logger.warning("Invalid 'lines' parameter %r; defaulting to 200.",
                           request.args.get('lines'))
            lines = 200
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.readlines()
        return jsonify({'filename': filename, 'lines': content[-lines:]}), 200
    except Exception as e:
        logger.error(f"Failed to read log {filename}: {e}", exc_info=True)
        return jsonify({'message': f'Failed to read log: {e}'}), 500


# --- Cleanup on Process Exit ---
# NOTE: this must NOT be a Flask `teardown_appcontext` hook — that hook runs at the
# end of *every request*, which would shut the controller down on the first page load.
_shutdown_lock = threading.Lock()
_shutdown_called = False


def shutdown_APPLICATION_controller():
    """Shut the controller down exactly once, when the process exits."""
    global _shutdown_called
    with _shutdown_lock:
        if _shutdown_called:
            return
        _shutdown_called = True
    # Never in the middle of a workspace switch: wait for it, then shut the
    # controller it installed down.
    with _switch_lock:
        _shutdown_active_controller()


def _shutdown_active_controller():
    print("Process exit: Shutting down APPLICATION controller.")
    logger.info("Process exit: Shutting down APPLICATION controller.")
    try:
        APPLICATION.shutdown()
    except Exception as e:
        logger.warning(f"Error during controller shutdown at exit: {e}", exc_info=True)
    closer = getattr(APPLICATION, 'close', None)
    if closer is not None:
        try:
            closer()
        except Exception as e:
            logger.warning(f"Error closing the controller at exit: {e}", exc_info=True)


atexit.register(shutdown_APPLICATION_controller)


def _handle_termination_signal(signum, _frame):
    """Shut the cell down gracefully on SIGTERM / SIGINT, then exit.

    Needed to run as a container service.  ``atexit`` only fires on a normal
    interpreter exit, and a process running as PID 1 in a container does not
    get the kernel's default SIGTERM action unless it installs a handler — so
    without this, ``docker stop`` tore the process down with robots still
    connected and motion still queued, and the graceful shutdown never ran.

    Shutting the controller down explicitly here (rather than leaving it to
    ``atexit``) also matters because :class:`~core.Task` threads are
    non-daemon: ``shutdown()`` stops and joins the running task with a
    timeout, so interpreter exit cannot block on an in-flight production cycle.
    """
    signal_name = signal.Signals(signum).name
    print(f"Received {signal_name}: shutting down APPLICATION controller.")
    logger.info(f"Received {signal_name} - shutting down APPLICATION controller.")
    shutdown_APPLICATION_controller()
    sys.exit(0)


if __name__ == '__main__':
    # Registered only under __main__ so a WSGI host (gunicorn, uWSGI) keeps
    # control of its own signal handling.
    for _sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(_sig, _handle_termination_signal)

    logger.info(f"Starting Flask server on {args.host}:{args.port}")
    app.run(debug=False, host=args.host, port=args.port)
