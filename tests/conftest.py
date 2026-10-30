"""Shared fixtures: a controller over a hardware-free simulated cell.

Every test runs against ``SimulatedDevice`` instances declared in a temporary
config.yaml, with the repo's own ``app_logic`` task modules (prod, home,
manual_actions) — the same code the default workspace runs.
"""

import os
import sys
import tempfile
import time

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Logs go to a throwaway folder, never the developer's logs/. Set before any
# core module creates its loggers.
os.environ.setdefault("MECADEMIC_DEMO_ROOT", tempfile.mkdtemp(prefix="mecademic-tests-"))
os.environ.setdefault("MECADEMIC_CONSOLE_LOG_LEVEL", "WARNING")

CELL_CONFIG = """
manual_actions:
  - key: "open_gripper"
    label: "Open Gripper"
  - key: "inspect_only"
    label: "Inspect Part"

devices:
  my_meca_robot:
    type: "simulated"
    supports_monitoring: true
  part_feeder:
    type: "simulated"
  inspection_camera:
    type: "simulated"
"""

# Fast cycles: the example prod_cycle sleeps on these params.
CELL_CONTEXT = """
params:
  move_time_s: {type: float, default: 0.02}
  cycle_wait_s: {type: float, default: 0.02}
  reject_every_n: {type: int, default: 3}
variables:
  part_count: {type: int, default: 0}
  reject_count: {type: int, default: 0}
"""


def wait_for(predicate, timeout=5.0, interval=0.02):
    """Poll *predicate* until it is truthy; fail the test on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition not met within %.1fs" % timeout)


@pytest.fixture
def cell_dir(tmp_path):
    (tmp_path / "config.yaml").write_text(CELL_CONFIG, encoding="utf-8")
    (tmp_path / "context.yaml").write_text(CELL_CONTEXT, encoding="utf-8")
    return tmp_path


@pytest.fixture
def controller(cell_dir):
    from core.ApplicationController import ApplicationController

    ctrl = ApplicationController(config_path=str(cell_dir / "config.yaml"))
    yield ctrl
    try:
        ctrl.shutdown()
    except Exception:
        pass
    ctrl.close()


def make_workspace(folder, marker, layout="flat", title=None):
    """A minimal workspace (one simulated device) with a local module and package."""
    config_dir = folder / "app_logic" if layout == "app_logic" else folder
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        (f"title: {title}\n" if title else "") + "devices:\n  cell: {type: simulated}\n",
        encoding="utf-8",
    )
    (folder / "marker.py").write_text(f"VERSION = {marker!r}\n", encoding="utf-8")
    (folder / "ws_helpers").mkdir()
    (folder / "ws_helpers" / "__init__.py").write_text(f"VERSION = {marker!r}\n", encoding="utf-8")
    return folder


@pytest.fixture(scope="session")
def flask_app(tmp_path_factory):
    """app.py imported once, against a temporary workspaces root (ws_main, ws_other)."""
    root = tmp_path_factory.mktemp("workspaces")
    make_workspace(root / "ws_main", "main")
    make_workspace(root / "ws_other", "other", layout="app_logic")
    argv = sys.argv
    sys.argv = ["app.py", "--workspace", str(root / "ws_main"), "--workspaces-root", str(root)]
    try:
        import app as app_module
    finally:
        sys.argv = argv
    yield app_module
    with app_module._switch_lock:
        app_module._shutdown_active_controller()


@pytest.fixture
def ready_controller(controller):
    from core.ControllerState import ControllerState

    controller.initialize()
    assert controller.get_state() == ControllerState.READY
    return controller
