# app.py
import argparse
import sys
import os
from pathlib import Path
from io import BytesIO

# --- Workspace Setup ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
PROJECT_ROOT = Path(BASE_DIR).resolve().parent
os.environ.setdefault("MECADEMIC_DEMO_ROOT", str(PROJECT_ROOT))

parser = argparse.ArgumentParser(description="Mecademic Demo App")
parser.add_argument('--workspace', type=str, default=BASE_DIR, help="Path to the external workspace")
args, _ = parser.parse_known_args()

workspace_path = os.path.abspath(args.workspace)
sys.path.insert(0, workspace_path)

from flask import Flask, render_template, jsonify, request, send_from_directory, url_for, send_file
from werkzeug.utils import secure_filename
import yaml

import logging
from logging.handlers import RotatingFileHandler

from core.Task import TaskType
from core.ControllerState import ControllerState
from core.BackupRestoreService import BackupRestoreService

# --- Logging Setup ---
_APP_LOG_DIR = os.path.join(os.environ.get("MECADEMIC_DEMO_ROOT", str(PROJECT_ROOT)), "logs", "app")
os.makedirs(_APP_LOG_DIR, exist_ok=True)
logger = logging.getLogger("app")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    _handler = RotatingFileHandler(os.path.join(_APP_LOG_DIR, "app.log"), maxBytes=5*1024*1024, backupCount=2)
    _handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(message)s'))
    logger.addHandler(_handler)

# --- Flask Setup ---
app = Flask(__name__)

app_logic_dir = os.path.join(workspace_path, "app_logic")
if os.path.isdir(app_logic_dir):
    CONFIG_PATH = os.path.join(app_logic_dir, "config.yaml")
    PROD_CTX_PATH = os.path.join(app_logic_dir, "production_context.yaml")
else:
    CONFIG_PATH = os.path.join(workspace_path, "config.yaml")
    PROD_CTX_PATH = os.path.join(workspace_path, "production_context.yaml")


def _load_yaml_config(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, 'r', encoding='utf-8') as file:
        data = yaml.safe_load(file)
    return data if isinstance(data, dict) else {}


def _save_yaml_config(path: str, config_data: dict) -> None:
    with open(path, 'w', encoding='utf-8') as file:
        yaml.safe_dump(config_data, file, sort_keys=False, allow_unicode=False)

# --- APPLICATION Controller Instance (Singleton) ---
# Initialize the controller once outside the routes
# NOTE: Replace dummy config with your actual Meca 500 connection details


def _build_application_controller():
    from core.ApplicationController import ApplicationController
    return ApplicationController(config_path=CONFIG_PATH, production_context_path=PROD_CTX_PATH)


def _reload_application_controller() -> None:
    """Recreate the global controller from current YAML config on disk."""
    global APPLICATION
    current = APPLICATION
    replacement = _build_application_controller()
    APPLICATION = replacement
    try:
        if current is not None:
            current.shutdown()
    except Exception as shutdown_err:
        logger.warning(f"Previous ApplicationController shutdown failed during reload: {shutdown_err}")


try:
    # We must start the controller in the main thread before starting Flask's server
    APPLICATION = _build_application_controller()
    logger.info("ApplicationController initialized successfully.")
except Exception as e:
    # If connection fails, set a permanent FAULT state
    logger.critical(f"Failed to initialize ApplicationController: {e}", exc_info=True)
    print(f"FATAL: Failed to initialize ApplicationController: {e}")
    class MockApplicationController:
        def get_state(self): return ControllerState.OFF
        def start_task(self, task): print("Mocked start task.")
        def start_manual_action(self, action_key):
            print(f"Mocked manual action: {action_key}")
            return False
        def stop_current_task(self): print("Mocked stop task.")
        def abort_current_task(self): print("Mocked abort task.")
        def initialize(self): return True
        def shutdown(self): pass
        def get_devices_info(self): return {}
        def get_config_snapshot(self): return {'devices': {}, 'manual_actions': [], 'production_context_file': 'production_context.yaml'}
        def get_manual_actions(self): return []
        def clear_faults(self): pass
        def control_device(self, device_id, action):
            raise RuntimeError('Controller is unavailable.')
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


DEVICE_TYPE_CATALOG = {
    'mecademic': {
        'label': 'Mecademic Robot',
        'fields': [
            {'name': 'ip_address', 'label': 'IP Address', 'type': 'string', 'required': True},
        ],
    },
    'asyril': {
        'label': 'Asyril Eye+ Feeder',
        'fields': [
            {'name': 'ip_address', 'label': 'IP Address', 'type': 'string', 'required': True},
            {'name': 'recipe', 'label': 'Recipe', 'type': 'int', 'required': True},
            {'name': 'port', 'label': 'Port', 'type': 'int', 'required': False, 'default': 7171},
        ],
    },
    'arduino': {
        'label': 'Arduino Board',
        'fields': [
            {'name': 'port', 'label': 'Serial Port', 'type': 'string', 'required': True},
        ],
    },
    'planarmotor': {
        'label': 'Planar Motor',
        'fields': [
            {'name': 'ip_address', 'label': 'IP Address', 'type': 'string', 'required': True},
        ],
    },
    'iologik': {
        'label': 'ioLogik E1212',
        'fields': [
            {'name': 'ip_address', 'label': 'IP Address', 'type': 'string', 'required': True},
            {'name': 'port', 'label': 'Port', 'type': 'int', 'required': False, 'default': 502},
            {'name': 'slave_id', 'label': 'Slave ID', 'type': 'int', 'required': False, 'default': 1},
        ],
    },
    'brainboxes_ed_digital': {
        'label': 'Brainboxes ED Digital',
        'fields': [
            {'name': 'ip_address', 'label': 'IP Address', 'type': 'string', 'required': True},
            {'name': 'address', 'label': 'Address', 'type': 'int', 'required': False, 'default': 1},
            {'name': 'port', 'label': 'Port', 'type': 'int', 'required': False, 'default': 9500},
            {'name': 'timeout', 'label': 'Timeout (s)', 'type': 'float', 'required': False, 'default': 5.0},
        ],
    },
    'brainboxes_ed_analogue_input': {
        'label': 'Brainboxes ED Analogue Input',
        'fields': [
            {'name': 'ip_address', 'label': 'IP Address', 'type': 'string', 'required': True},
            {'name': 'address', 'label': 'Address', 'type': 'int', 'required': False, 'default': 1},
            {'name': 'port', 'label': 'Port', 'type': 'int', 'required': False, 'default': 9500},
            {'name': 'timeout', 'label': 'Timeout (s)', 'type': 'float', 'required': False, 'default': 5.0},
        ],
    },
    'brainboxes_ed_analogue_output': {
        'label': 'Brainboxes ED Analogue Output',
        'fields': [
            {'name': 'ip_address', 'label': 'IP Address', 'type': 'string', 'required': True},
            {'name': 'address', 'label': 'Address', 'type': 'int', 'required': False, 'default': 1},
            {'name': 'port', 'label': 'Port', 'type': 'int', 'required': False, 'default': 9500},
            {'name': 'timeout', 'label': 'Timeout (s)', 'type': 'float', 'required': False, 'default': 5.0},
        ],
    },
    'lmi': {
        'label': 'LMI Sensor',
        'fields': [
            {'name': 'ip_address', 'label': 'IP Address', 'type': 'string', 'required': True},
            {'name': 'control_port', 'label': 'Control Port', 'type': 'int', 'required': False, 'default': 3190},
            {'name': 'data_port', 'label': 'Data Port', 'type': 'int', 'required': False, 'default': 3192},
            {'name': 'health_port', 'label': 'Health Port', 'type': 'int', 'required': False, 'default': 3194},
            {'name': 'delimiter', 'label': 'Delimiter', 'type': 'string', 'required': False, 'default': ','},
            {'name': 'terminator', 'label': 'Terminator', 'type': 'string', 'required': False, 'default': '\\r\\n'},
        ],
    },
    'gige_camera': {
        'label': 'GigE Vision Camera',
        'fields': [
            {'name': 'cti_path', 'label': 'CTI Path', 'type': 'string', 'required': True},
            {'name': 'camera_index', 'label': 'Camera Index', 'type': 'int', 'required': False, 'default': 0},
            {'name': 'autostart_stream', 'label': 'Auto Start Stream', 'type': 'bool', 'required': False, 'default': False},
        ],
    },
    'zaber': {
        'label': 'Zaber Axis',
        'fields': [
            {'name': 'port', 'label': 'Serial Port', 'type': 'string', 'required': True},
            {'name': 'axis_number', 'label': 'Axis Number', 'type': 'int', 'required': False, 'default': 1},
        ],
    },
}


def _coerce_value(value, type_name):
    if type_name == 'int':
        return int(value)
    if type_name == 'float':
        return float(value)
    if type_name == 'bool':
        return _parse_bool(value, default=False)
    return str(value)


def _normalize_devices_payload(devices_payload: dict) -> dict:
    if not isinstance(devices_payload, dict):
        raise ValueError("'devices' must be an object keyed by device name.")

    normalized = {}
    for raw_device_id, raw_cfg in devices_payload.items():
        device_id = str(raw_device_id or '').strip()
        if not device_id:
            raise ValueError('Device id cannot be empty.')
        if not isinstance(raw_cfg, dict):
            raise ValueError(f"Device '{device_id}' configuration must be an object.")

        device_type = str(raw_cfg.get('type', '')).strip().lower()
        if not device_type:
            raise ValueError(f"Device '{device_id}' is missing required field 'type'.")

        if device_type not in DEVICE_TYPE_CATALOG:
            raise ValueError(f"Device '{device_id}' has unsupported type '{device_type}'.")

        schema = DEVICE_TYPE_CATALOG[device_type]
        cfg = {'type': device_type}
        for field in schema.get('fields', []):
            field_name = field['name']
            required = bool(field.get('required'))
            if field_name in raw_cfg and raw_cfg[field_name] not in (None, ''):
                try:
                    cfg[field_name] = _coerce_value(raw_cfg[field_name], field.get('type', 'string'))
                except Exception as conv_err:
                    raise ValueError(
                        f"Invalid value for '{device_id}.{field_name}': {conv_err}"
                    ) from conv_err
            elif required:
                raise ValueError(f"Device '{device_id}' is missing required field '{field_name}'.")
            elif 'default' in field:
                cfg[field_name] = field['default']

        normalized[device_id] = cfg

    return normalized


def _normalize_manual_actions_payload(actions_payload) -> list[dict]:
    if not isinstance(actions_payload, list):
        raise ValueError("'manual_actions' must be a list.")

    normalized = []
    seen_keys = set()
    for index, item in enumerate(actions_payload):
        if not isinstance(item, dict):
            raise ValueError(f"manual_actions[{index}] must be an object.")

        key = str(item.get('key', '')).strip()
        if not key:
            raise ValueError(f"manual_actions[{index}] is missing required field 'key'.")
        if key in seen_keys:
            raise ValueError(f"Duplicate manual action key '{key}'.")
        seen_keys.add(key)

        entry = {'key': key}
        for optional in ('label', 'description', 'confirm_title', 'confirm_message'):
            if optional in item and item[optional] is not None:
                entry[optional] = str(item[optional])
        normalized.append(entry)

    return normalized


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

    return jsonify({'message': f'Could not start manual action. APPLICATION is {state}.', 'success': False}), 400


@app.route('/api/config', methods=['GET'])
def get_config():
    """Return editable config and device type schema for the Devices tab."""
    try:
        config_data = _load_yaml_config(CONFIG_PATH)
        if 'devices' not in config_data or not isinstance(config_data.get('devices'), dict):
            config_data['devices'] = {}
        if 'manual_actions' not in config_data or not isinstance(config_data.get('manual_actions'), list):
            config_data['manual_actions'] = []
        if 'production_context_file' not in config_data:
            config_data['production_context_file'] = 'production_context.yaml'

        return jsonify({
            'config': config_data,
            'device_type_catalog': DEVICE_TYPE_CATALOG,
            'config_path': CONFIG_PATH,
            'success': True,
        }), 200
    except Exception as e:
        logger.error(f"Failed to load config: {e}", exc_info=True)
        return jsonify({'message': f'Failed to load config: {e}', 'success': False}), 500


@app.route('/api/config/devices', methods=['POST'])
def save_devices_config():
    """Persist updated devices config and recreate the controller from disk."""
    payload = request.get_json(silent=True) or {}
    try:
        normalized_devices = _normalize_devices_payload(payload.get('devices', {}))
        config_data = _load_yaml_config(CONFIG_PATH)
        config_data['devices'] = normalized_devices
        _save_yaml_config(CONFIG_PATH, config_data)
        _reload_application_controller()
        return jsonify({
            'message': 'Devices configuration saved and controller reloaded.',
            'count': len(normalized_devices),
            'success': True,
        }), 200
    except Exception as e:
        logger.error(f"Failed to save devices config: {e}", exc_info=True)
        return jsonify({'message': f'Failed to save devices config: {e}', 'success': False}), 400


@app.route('/api/config/manual_actions', methods=['POST'])
def save_manual_actions_config():
    """Persist updated manual actions metadata and recreate the controller from disk."""
    payload = request.get_json(silent=True) or {}
    try:
        normalized_actions = _normalize_manual_actions_payload(payload.get('manual_actions', []))
        config_data = _load_yaml_config(CONFIG_PATH)
        config_data['manual_actions'] = normalized_actions
        _save_yaml_config(CONFIG_PATH, config_data)
        _reload_application_controller()
        return jsonify({
            'message': 'Manual actions configuration saved and controller reloaded.',
            'count': len(normalized_actions),
            'success': True,
        }), 200
    except Exception as e:
        logger.error(f"Failed to save manual actions config: {e}", exc_info=True)
        return jsonify({'message': f'Failed to save manual actions config: {e}', 'success': False}), 400


@app.route('/api/config/context', methods=['GET'])
def get_context_config():
    """Return parsed production context configuration file for editing."""
    try:
        context_data = _load_yaml_config(PROD_CTX_PATH)
        if not context_data:
            context_data = {'version': 1, 'settings': {}, 'params': {}, 'variables': {}}
        raw_yaml = yaml.safe_dump(context_data, sort_keys=False, allow_unicode=False)
        return jsonify({'context': context_data, 'raw_yaml': raw_yaml, 'path': PROD_CTX_PATH, 'success': True}), 200
    except Exception as e:
        logger.error(f"Failed to load production context config: {e}", exc_info=True)
        return jsonify({'message': f'Failed to load production context config: {e}', 'success': False}), 500


@app.route('/api/config/context', methods=['POST'])
def save_context_config():
    """Persist updated production context configuration and recreate controller."""
    payload = request.get_json(silent=True) or {}
    try:
        context_data = payload.get('context')
        raw_yaml = payload.get('raw_yaml')

        if isinstance(raw_yaml, str) and raw_yaml.strip():
            parsed = yaml.safe_load(raw_yaml)
            if not isinstance(parsed, dict):
                raise ValueError('Context YAML must decode to an object.')
            context_data = parsed

        if not isinstance(context_data, dict):
            raise ValueError("'context' must be an object or provide 'raw_yaml'.")
        if 'settings' not in context_data:
            context_data['settings'] = {}
        if 'params' not in context_data:
            context_data['params'] = {}
        if 'variables' not in context_data:
            context_data['variables'] = {}

        _save_yaml_config(PROD_CTX_PATH, context_data)
        _reload_application_controller()
        return jsonify({'message': 'Production context config saved and controller reloaded.', 'success': True}), 200
    except Exception as e:
        logger.error(f"Failed to save production context config: {e}", exc_info=True)
        return jsonify({'message': f'Failed to save production context config: {e}', 'success': False}), 400


@app.route('/api/config/context/import', methods=['POST'])
def import_context_config():
    """Import production context YAML file and recreate controller."""
    uploaded = request.files.get('context')
    if uploaded is None or not uploaded.filename:
        return jsonify({'message': 'Context file is required.', 'success': False}), 400

    name = secure_filename(uploaded.filename)
    if not name.lower().endswith(('.yaml', '.yml')):
        return jsonify({'message': 'Only .yaml/.yml files are supported.', 'success': False}), 400

    try:
        raw_text = uploaded.read().decode('utf-8')
        imported = yaml.safe_load(raw_text) or {}
        if not isinstance(imported, dict):
            raise ValueError('Imported context must be a YAML object.')

        _save_yaml_config(PROD_CTX_PATH, imported)
        _reload_application_controller()
        return jsonify({'message': 'Production context imported and controller reloaded.', 'success': True}), 200
    except Exception as e:
        logger.error(f"Failed to import production context config: {e}", exc_info=True)
        return jsonify({'message': f'Failed to import production context config: {e}', 'success': False}), 400


@app.route('/api/config/context/export', methods=['GET'])
def export_context_config():
    """Download current production context YAML file."""
    try:
        context_data = _load_yaml_config(PROD_CTX_PATH)
        payload = yaml.safe_dump(context_data, sort_keys=False, allow_unicode=False)
        return send_file(
            BytesIO(payload.encode('utf-8')),
            mimetype='application/x-yaml',
            as_attachment=True,
            download_name='production_context_export.yaml',
        )
    except Exception as e:
        logger.error(f"Failed to export production context config: {e}", exc_info=True)
        return jsonify({'message': f'Failed to export production context config: {e}', 'success': False}), 500


@app.route('/api/config/manual_actions/import', methods=['POST'])
def import_manual_actions_config():
    """Import manual actions list from YAML file and recreate controller."""
    uploaded = request.files.get('manual_actions')
    if uploaded is None or not uploaded.filename:
        return jsonify({'message': 'Manual actions file is required.', 'success': False}), 400

    name = secure_filename(uploaded.filename)
    if not name.lower().endswith(('.yaml', '.yml')):
        return jsonify({'message': 'Only .yaml/.yml files are supported.', 'success': False}), 400

    try:
        raw_text = uploaded.read().decode('utf-8')
        imported = yaml.safe_load(raw_text) or []
        if isinstance(imported, dict) and 'manual_actions' in imported:
            imported = imported['manual_actions']
        normalized_actions = _normalize_manual_actions_payload(imported)

        config_data = _load_yaml_config(CONFIG_PATH)
        config_data['manual_actions'] = normalized_actions
        _save_yaml_config(CONFIG_PATH, config_data)
        _reload_application_controller()
        return jsonify({'message': 'Manual actions imported and controller reloaded.', 'count': len(normalized_actions), 'success': True}), 200
    except Exception as e:
        logger.error(f"Failed to import manual actions config: {e}", exc_info=True)
        return jsonify({'message': f'Failed to import manual actions config: {e}', 'success': False}), 400


@app.route('/api/config/manual_actions/export', methods=['GET'])
def export_manual_actions_config():
    """Download manual actions list as YAML file."""
    try:
        config_data = _load_yaml_config(CONFIG_PATH)
        actions = config_data.get('manual_actions', []) if isinstance(config_data, dict) else []
        payload = yaml.safe_dump({'manual_actions': actions}, sort_keys=False, allow_unicode=False)
        return send_file(
            BytesIO(payload.encode('utf-8')),
            mimetype='application/x-yaml',
            as_attachment=True,
            download_name='manual_actions_export.yaml',
        )
    except Exception as e:
        logger.error(f"Failed to export manual actions config: {e}", exc_info=True)
        return jsonify({'message': f'Failed to export manual actions config: {e}', 'success': False}), 500


@app.route('/api/config/import', methods=['POST'])
def import_config():
    """Import a full YAML config file, validate devices section, and reload controller."""
    uploaded = request.files.get('config')
    if uploaded is None or not uploaded.filename:
        return jsonify({'message': 'Config file is required.', 'success': False}), 400

    name = secure_filename(uploaded.filename)
    if not name.lower().endswith(('.yaml', '.yml')):
        return jsonify({'message': 'Only .yaml/.yml files are supported.', 'success': False}), 400

    try:
        raw_text = uploaded.read().decode('utf-8')
        imported = yaml.safe_load(raw_text) or {}
        if not isinstance(imported, dict):
            raise ValueError('Imported content must be a YAML object.')

        imported_devices = imported.get('devices', {})
        imported['devices'] = _normalize_devices_payload(imported_devices)
        if 'manual_actions' in imported and not isinstance(imported['manual_actions'], list):
            raise ValueError("'manual_actions' must be a list when provided.")

        _save_yaml_config(CONFIG_PATH, imported)
        _reload_application_controller()
        return jsonify({
            'message': 'Configuration imported and controller reloaded.',
            'count': len(imported['devices']),
            'success': True,
        }), 200
    except Exception as e:
        logger.error(f"Failed to import config: {e}", exc_info=True)
        return jsonify({'message': f'Failed to import config: {e}', 'success': False}), 400


@app.route('/api/config/export', methods=['GET'])
def export_config():
    """Download current YAML config."""
    try:
        config_data = _load_yaml_config(CONFIG_PATH)
        payload = yaml.safe_dump(config_data, sort_keys=False, allow_unicode=False)
        return send_file(
            BytesIO(payload.encode('utf-8')),
            mimetype='application/x-yaml',
            as_attachment=True,
            download_name='config_export.yaml',
        )
    except Exception as e:
        logger.error(f"Failed to export config: {e}", exc_info=True)
        return jsonify({'message': f'Failed to export config: {e}', 'success': False}), 500


@app.route('/api/config/global/import', methods=['POST'])
def import_global_config_alias():
    """Alias endpoint for importing full global config."""
    return import_config()


@app.route('/api/config/global/export', methods=['GET'])
def export_global_config_alias():
    """Alias endpoint for exporting full global config."""
    return export_config()


@app.route('/api/devices/<device_id>/control', methods=['POST'])
def control_device(device_id):
    """Run direct control/diagnostic action on one configured device."""
    payload = request.get_json(silent=True) or {}
    action = str(payload.get('action', 'probe')).strip().lower()
    try:
        result = APPLICATION.control_device(device_id, action)
        return jsonify({'message': f"Action '{action}' executed for {device_id}.", 'result': result, 'success': True}), 200
    except KeyError as e:
        return jsonify({'message': str(e), 'success': False}), 404
    except ValueError as e:
        return jsonify({'message': str(e), 'success': False}), 400
    except Exception as e:
        logger.error(f"Device action failed for {device_id}/{action}: {e}", exc_info=True)
        return jsonify({'message': f'Device action failed: {e}', 'success': False}), 500

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