#!/usr/bin/env python
"""Check a Mecademic demo app workspace, then run every task of it on simulated devices.

Run it with the app's own Python, so the framework and its SDKs import:

    <app>/.venv/Scripts/python.exe check_workspace.py <workspace> --app <app>
    <app>/.venv/bin/python        check_workspace.py <workspace> --app <app>

Two passes:

1. Static (nothing runs): config.yaml parses and its devices validate against
   devices/registry.py; the four task modules exist (a missing one makes the
   app run the demo module from mecademic_demo_app/app_logic instead); no
   helper module shadows the framework or an installed package; every device
   name, context name and declared sequence step the code uses exists.

2. Simulated smoke test: the workspace is copied to a scratch folder with
   every device swapped for a hardware-free stand-in (cameras become
   ``simulated_camera``, ``cv_process`` stays, everything else becomes
   ``simulated``). The real ApplicationController then INITIALIZEs it and runs
   HOME, CALIBRATION, SHIPMENT, every manual action, and PROD for a few
   cycles followed by STOP, exactly as the UI would. A task that faults is
   re-run directly to print its traceback. Steps each task opened are
   compared with the sequences declared in config.yaml.

The workspace itself is never modified. Exit code 0 when no ERROR was found.

Options:
    --cycles N         PROD cycles to run (default 3)
    --task-timeout S   seconds one task may take in simulation (default 60)
    --keep-sim DIR     write the simulated copy to DIR (new or empty folder)
                       and keep it, e.g. to open it in the browser
    --static-only      skip the simulated run
    --json             print the findings as JSON instead of text
"""

from __future__ import annotations

import argparse
import ast
import copy
import importlib.util
import inspect
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from typing import Any, Dict, List

import yaml

TASK_FILES = {"prod.py": "prod_cycle", "home.py": "home", "shipment.py": "shipment", "calib.py": "calib"}
FRAMEWORK_NAMES = {"core", "devices", "app", "app_logic", "tests", "conftest", "static", "templates"}
#: Module names the app itself imports from a workspace: never "shadowing".
CONVENTIONAL_NAMES = {"prod", "home", "shipment", "calib", "manual_actions", "custom_view"}
SEQUENCE_TASK_KEYS = {"prod_cycle", "home", "shipment", "calib"}
CONTEXT_CALLS = {"get_param": "params", "set_param": "params",
                 "get_variable": "variables", "set_variable": "variables"}
#: Methods the repo's simulated demo calls that no real driver has.
DEMO_ONLY_METHODS = {"TriggerAcquisition", "WaitForFrame", "SolveHandEye"}
#: Top-level steps the task runner opens itself (core/Task.py), around the workspace's own.
FRAMEWORK_STEPS = {"Home (entry)", "Home (exit)"}
CAMERA_TYPES = {"gige_camera", "simulated_camera", "test_pattern_camera"}
SIMULATED_CAMERA_FEATURES = {"ExposureTime", "Gain", "PixelFormat"}
KEEP_TYPES = {"simulated", "dummy", "fake", "cv_process", "vision", "cv"}
SKIP_COPY = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "logs", "backups",
                                   "context.state.json", ".venv", "venv")


class Report:
    def __init__(self) -> None:
        self.items: List[Dict[str, str]] = []

    def add(self, level: str, area: str, message: str, hint: str = "") -> None:
        self.items.append({"level": level, "area": area, "message": message, "hint": hint})

    def error(self, area, message, hint=""):
        self.add("ERROR", area, message, hint)

    def warn(self, area, message, hint=""):
        self.add("WARN", area, message, hint)

    def info(self, area, message, hint=""):
        self.add("INFO", area, message, hint)

    @property
    def errors(self) -> int:
        return sum(1 for i in self.items if i["level"] == "ERROR")

    def print_text(self) -> None:
        for area in dict.fromkeys(i["area"] for i in self.items):
            print(f"\n== {area}")
            for item in (i for i in self.items if i["area"] == area):
                print(f"  {item['level']:<5} {item['message']}")
                if item["hint"]:
                    for line in item["hint"].splitlines():
                        print(f"        {line}")
        warnings = sum(1 for i in self.items if i["level"] == "WARN")
        print(f"\n{self.errors} error(s), {warnings} warning(s).")


# --------------------------------------------------------------------------
# Locating things
# --------------------------------------------------------------------------

def find_app(workspace: str, explicit: str | None) -> str:
    candidates = [explicit] if explicit else []
    here = os.path.abspath(workspace)
    for base in (here, os.path.dirname(here), os.getcwd()):
        candidates += [os.path.join(base, "mecademic_demo_app"),
                       os.path.join(base, "mecademic_demo", "mecademic_demo_app"),
                       os.path.join(os.path.dirname(base), "mecademic_demo", "mecademic_demo_app"), base]
    for path in candidates:
        if path and os.path.isfile(os.path.join(path, "core", "ApplicationController.py")):
            return os.path.abspath(path)
    raise SystemExit("Cannot find mecademic_demo_app: pass --app <path to mecademic_demo_app>.")


def config_dir_of(path: str) -> str | None:
    if os.path.isfile(os.path.join(path, "app_logic", "config.yaml")):
        return os.path.join(path, "app_logic")
    if os.path.isfile(os.path.join(path, "config.yaml")):
        return path
    return None


def top_level_modules(folder: str) -> List[str]:
    names = []
    for entry in sorted(os.listdir(folder)):
        full = os.path.join(folder, entry)
        if entry.startswith((".", "_")):
            continue
        if entry.endswith(".py"):
            names.append(entry[:-3])
        elif os.path.isdir(full) and entry not in {"logs", "backups", "__pycache__", "venv", ".venv"} \
                and any(f.endswith(".py") for f in os.listdir(full)):
            names.append(entry)
    return names


# --------------------------------------------------------------------------
# Static pass
# --------------------------------------------------------------------------

class CodeFacts(ast.NodeVisitor):
    """What the workspace code references: steps, context names, devices, functions."""

    def __init__(self, filename: str):
        self.filename = filename
        self.steps: Dict[str, int] = {}
        self.dynamic_steps: List[int] = []
        self.context_names: Dict[str, Dict[str, List[int]]] = {"get": {}, "set": {}}
        self.context_ns: Dict[str, str] = {}
        self.device_names: Dict[str, int] = {}
        self.functions: set[str] = set()
        self.legacy: List[str] = []
        self.demo_calls: List[str] = []
        self.path_hacks: List[int] = []
        self.ignored_force: List[int] = []

    @staticmethod
    def _receiver(node) -> str | None:
        return node.func.value.id if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) else None

    def visit_FunctionDef(self, node):
        if isinstance(node, ast.FunctionDef) and node.col_offset == 0:
            self.functions.add(node.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module and "ProductionContext" in node.module:
            self.legacy.append(f"line {node.lineno}: imports {node.module} (removed; use core.AppContext)")
        self.generic_visit(node)

    def visit_Import(self, node):
        for alias in node.names:
            if "ProductionContext" in alias.name:
                self.legacy.append(f"line {node.lineno}: imports {alias.name} (removed; use core.AppContext)")
        self.generic_visit(node)

    def visit_Subscript(self, node):
        if isinstance(node.value, ast.Name) and node.value.id == "devices" \
                and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            self.device_names.setdefault(node.slice.value, node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node):
        func = node.func
        receiver = self._receiver(node)
        attr = func.attr if isinstance(func, ast.Attribute) else None
        first = node.args[0] if node.args else None
        literal = first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None
        if receiver in ("context", "ctx") and attr in ("step", "set_step"):
            if literal is not None:
                self.steps.setdefault(literal, node.lineno)
            else:
                self.dynamic_steps.append(node.lineno)
        if receiver in ("context", "ctx") and attr in CONTEXT_CALLS and literal is not None:
            kind = "set" if attr.startswith("set") else "get"
            self.context_names[kind].setdefault(literal, []).append(node.lineno)
            self.context_ns[literal] = CONTEXT_CALLS[attr]
        if receiver == "devices" and attr == "get" and literal is not None:
            self.device_names.setdefault(literal, node.lineno)
        if attr in DEMO_ONLY_METHODS:
            self.demo_calls.append(f"line {node.lineno}: .{attr}()")
        if attr == "insert" and isinstance(func.value, ast.Attribute) and func.value.attr == "path":
            self.path_hacks.append(node.lineno)
        for kw in node.keywords:
            if kw.arg == "force" and attr in ("set_variable", "set_param"):
                self.ignored_force.append(node.lineno)
        self.generic_visit(node)


def load_yaml(path: str, report: Report, area: str) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as e:
        report.error(area, f"{os.path.basename(path)} does not parse: {e}")
        return None


def static_checks(ws: str, cfg_dir: str, app: str, report: Report) -> Dict[str, Any]:
    area = "Static checks"
    facts: Dict[str, CodeFacts] = {}
    config = load_yaml(os.path.join(cfg_dir, "config.yaml"), report, area) or {}
    if not isinstance(config, dict):
        report.error(area, "config.yaml must be a mapping at top level.")
        config = {}

    if cfg_dir != ws:
        report.warn(area, "app_logic/ layout: new workspaces should be flat (config.yaml at the top).")
    if not config.get("title"):
        report.warn(area, "config.yaml has no `title:`; the workspace picker shows the folder name.")
    if "production_context_file" in config or os.path.isfile(os.path.join(cfg_dir, "production_context.yaml")):
        report.warn(area, "Legacy production_context.yaml / production_context_file.",
                    "Rename the file to context.yaml, drop `value:`/`persist_scope:`/`storage_scope:`, "
                    "move `sequences:` to config.yaml.")

    # --- Python files -----------------------------------------------------
    for name in sorted(os.listdir(cfg_dir)):
        if name.endswith(".py"):
            path = os.path.join(cfg_dir, name)
            try:
                with open(path, encoding="utf-8") as f:
                    tree = ast.parse(f.read(), filename=path)
            except SyntaxError as e:
                report.error(area, f"{name}: syntax error line {e.lineno}: {e.msg}")
                continue
            visitor = CodeFacts(name)
            visitor.visit(tree)
            facts[name] = visitor
    view_init = os.path.join(cfg_dir, "custom_view", "__init__.py")
    if os.path.isfile(view_init):
        with open(view_init, encoding="utf-8") as f:
            visitor = CodeFacts("custom_view/__init__.py")
            visitor.visit(ast.parse(f.read()))
            facts["custom_view/__init__.py"] = visitor

    for filename, function in TASK_FILES.items():
        if filename not in facts:
            report.error(area, f"{filename} is missing.",
                         f"Without it the app imports app_logic.{filename[:-3]} from mecademic_demo_app "
                         f"(the simulated demo) and {function}() fails on this cell's devices.")
        elif function not in facts[filename].functions:
            report.error(area, f"{filename} does not define a top-level {function}(devices, context).")
    for name, visitor in facts.items():
        for line in visitor.legacy:
            report.error(area, f"{name} {line}",
                         "The import fails, so the app silently runs the demo module of the same name "
                         "from mecademic_demo_app/app_logic instead of this file.")
        if visitor.ignored_force:
            report.warn(area, f"{name} lines {visitor.ignored_force}: set_variable/set_param(..., force=...) "
                              "is ignored since AppContext; drop it.")
        for line in visitor.demo_calls:
            report.warn(area, f"{name} {line}: only the simulated demo has this method; no real driver does.")
        if visitor.path_hacks:
            report.warn(area, f"{name} edits sys.path (line {visitor.path_hacks[0]}); the app already puts the "
                              "workspace and the framework on it.")
        for line in visitor.dynamic_steps:
            report.warn(area, f"{name} line {line}: step name is not a string literal.",
                        "Step names must be stable identifiers (no part numbers): each distinct name is "
                        "a timing bucket, capped at 200.")

    # --- Shadowing ------------------------------------------------------
    for module in top_level_modules(cfg_dir):
        if module in CONVENTIONAL_NAMES:
            continue
        if module in FRAMEWORK_NAMES:
            report.error(area, f"Module '{module}' shadows the framework's own '{module}'. Rename it.")
        elif module in getattr(sys, "stdlib_module_names", ()):
            report.error(area, f"Module '{module}' shadows the standard library module '{module}'. Rename it.")
        else:
            spec = importlib.util.find_spec(module)
            if spec is not None and spec.origin and not os.path.abspath(spec.origin).startswith(os.path.abspath(ws)):
                report.error(area, f"Module '{module}' shadows the installed module at {spec.origin}. Rename it.")

    # --- Devices --------------------------------------------------------
    from devices.registry import validate_devices_config
    raw_devices = config.get("devices")
    if not raw_devices:
        report.error(area, "config.yaml declares no `devices:`.")
        raw_devices = {}
    entries, issues = validate_devices_config(raw_devices, base_dir=cfg_dir)
    for issue in issues:
        where = f"devices.{issue['device']}: " if issue.get("device") else ""
        (report.error if issue["level"] == "error" else report.warn)(area, f"{where}{issue['message']}")
    declared = {n for n, e in raw_devices.items() if isinstance(e, dict) and e.get("enabled", True) is not False}
    used: Dict[str, str] = {}
    for name, visitor in facts.items():
        for device, line in visitor.device_names.items():
            used.setdefault(device, f"{name}:{line}")
    for device, where in sorted(used.items()):
        if device not in declared:
            report.error(area, f"Code uses devices['{device}'] ({where}) but config.yaml declares no enabled "
                               f"device '{device}'.")
    unused = sorted(declared - set(used))
    if unused:
        report.info(area, f"Declared but never named in code: {', '.join(unused)}.")

    # --- Manual actions -------------------------------------------------
    actions = config.get("manual_actions") or []
    action_keys = [a.get("key") for a in actions if isinstance(a, dict)]
    if actions and "manual_actions.py" not in facts:
        report.error(area, "config.yaml lists manual_actions but manual_actions.py is missing.",
                     "The app would load app_logic.manual_actions from mecademic_demo_app instead.")
    for action in actions:
        if isinstance(action, dict) and not action.get("description"):
            report.warn(area, f"Manual action '{action.get('key')}' has no description.")

    # --- Context --------------------------------------------------------
    context_path = os.path.join(cfg_dir, config.get("context_file") or "context.yaml")
    context_doc = load_yaml(context_path, report, area) if os.path.isfile(context_path) else None
    declared_ctx = {"params": set(), "variables": set()}
    if context_doc is None and not os.path.isfile(context_path):
        report.info(area, "No context.yaml: the Variables tab is empty and context.set_variable() raises.")
    elif isinstance(context_doc, dict):
        for ns in declared_ctx:
            declared_ctx[ns] = set((context_doc.get(ns) or {}).keys())
    for name, visitor in facts.items():
        for kind, names in visitor.context_names.items():
            for ctx_name, lines in names.items():
                ns = visitor.context_ns[ctx_name]
                if ctx_name in declared_ctx[ns]:
                    continue
                if kind == "set":
                    report.error(area, f"{name} line {lines[0]} writes {ns[:-1]} '{ctx_name}', not declared in "
                                       f"context.yaml: ContextError at runtime.")
                else:
                    report.warn(area, f"{name} line {lines[0]} reads {ns[:-1]} '{ctx_name}', not declared in "
                                      f"context.yaml: the code's default is always used.")

    # --- Sequences ------------------------------------------------------
    sequences = config.get("sequences") or {}
    all_steps = set()
    for visitor in facts.values():
        all_steps.update(visitor.steps)
    for key, steps in sequences.items():
        if key not in SEQUENCE_TASK_KEYS and not (str(key).startswith("manual:") and key[7:] in action_keys):
            report.warn(area, f"sequences: '{key}' matches no task (prod_cycle, home, shipment, calib) "
                              f"nor any manual action ('manual:<key>').")
        for step in steps or []:
            if step not in all_steps:
                report.warn(area, f"sequences.{key}: step '{step}' is never opened by context.step() in the "
                                  f"code (typo?).")
    if "prod_cycle" not in sequences:
        report.info(area, "No `sequences: prod_cycle`: the UI shows steps but no 'Step n of m'.")

    return {"config": config, "facts": facts, "sequences": sequences, "action_keys": action_keys}


# --------------------------------------------------------------------------
# Simulated copy
# --------------------------------------------------------------------------

def simulated_device(name: str, entry: Dict[str, Any], notes: List[str]) -> Dict[str, Any]:
    if not isinstance(entry, dict):
        return entry
    kind = str(entry.get("type", "")).lower()
    keep = {k: entry[k] for k in ("enabled",) if k in entry}
    if kind in KEEP_TYPES:
        return copy.deepcopy(entry)
    if kind in CAMERA_TYPES:
        out = {"type": "simulated_camera", **keep}
        for key in ("trigger", "trigger_source", "trigger_activation", "width", "height", "fps"):
            if key in entry:
                out[key] = entry[key]
        features = entry.get("features") or {}
        kept = {k: v for k, v in features.items() if k in SIMULATED_CAMERA_FEATURES}
        if kept:
            out["features"] = kept
        if set(features) - set(kept) or entry.get("features_file"):
            notes.append(f"{name}: camera features other than {sorted(SIMULATED_CAMERA_FEATURES)} dropped")
        return out
    out = {"type": "simulated", **keep, "info": {"model": f"simulated {kind or 'device'}", "serial_number": name}}
    if kind == "mecademic":
        out["supports_monitoring"] = True
    return out


def make_simulated_copy(ws: str, cfg_dir: str, target: str, config: Dict[str, Any]) -> tuple[str, List[str]]:
    shutil.copytree(ws, target, ignore=SKIP_COPY, dirs_exist_ok=True)
    rel = os.path.relpath(cfg_dir, ws)
    sim_cfg_dir = os.path.normpath(os.path.join(target, rel))
    notes: List[str] = []
    sim = copy.deepcopy(config)
    sim["title"] = f"{config.get('title') or os.path.basename(ws)} (simulated)"
    sim["devices"] = {n: simulated_device(n, e, notes) for n, e in (config.get("devices") or {}).items()}
    with open(os.path.join(sim_cfg_dir, "config.yaml"), "w", encoding="utf-8") as f:
        f.write(f"# Simulated copy of {ws}, written by check_workspace.py. Do not edit: re-run the check.\n")
        yaml.safe_dump(sim, f, sort_keys=False, allow_unicode=True)
    return sim_cfg_dir, notes


# --------------------------------------------------------------------------
# Simulated run
# --------------------------------------------------------------------------

def wait_until(predicate, timeout: float, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def simulated_run(sim_root: str, sim_cfg_dir: str, facts: Dict[str, Any], args, report: Report) -> None:
    area = "Simulated run"
    sys.path.insert(0, sim_root)
    from core.ApplicationController import ApplicationController
    from core.ControllerState import ControllerState
    from core.ManualActions import load_manual_actions_registry
    from core.Task import TaskType, _accepts_context, load_task_function

    notices: List[str] = []
    try:
        ctrl = ApplicationController(config_path=os.path.join(sim_cfg_dir, "config.yaml"))
    except Exception as e:
        report.error(area, f"The app cannot load this workspace: {type(e).__name__}: {e}",
                     "".join(traceback.format_exception(e)).strip()[-1500:])
        return
    ctrl.on_notice = lambda level, message: notices.append(f"{level}: {message}")

    def state():
        return ctrl.get_state()

    try:
        errors = [i for i in ctrl.config_issues if i["level"] == "error"]
        for issue in errors:
            report.error(area, f"devices.{issue['device']}: {issue['message']}")
        if errors:
            return

        # Which file each task really comes from: a missing module falls back to the demo.
        sources = {}
        for function in TASK_FILES.values():
            try:
                fn = load_task_function(function)
                sources[function] = fn
                path = os.path.abspath(inspect.getsourcefile(fn) or "")
                if not path.startswith(os.path.abspath(sim_root)):
                    report.error(area, f"{function}() is loaded from {path}, not from the workspace.")
            except Exception as e:
                report.error(area, f"{function}() cannot be imported: {type(e).__name__}: {e}",
                             "".join(traceback.format_exception(e)).strip()[-1500:])
        actions = {}
        if facts["action_keys"]:
            actions = load_manual_actions_registry()
            module_file = inspect.getsourcefile(next(iter(actions.values()))) if actions else ""
            if module_file and not os.path.abspath(module_file).startswith(os.path.abspath(sim_root)):
                report.error(area, f"Manual actions are loaded from {module_file}, not from the workspace.")
            extra = sorted(set(actions) - set(facts["action_keys"]))
            if extra:
                report.warn(area, f"get_manual_actions() returns {extra}, not listed in config.yaml: no button.")

        try:
            ctrl.initialize()
        except Exception as e:
            report.error(area, f"INITIALIZE failed: {e}")
            return
        report.info(area, f"INITIALIZE: {len(ctrl.devices)} device(s) ready ({', '.join(ctrl.devices)}).")
        setup_error = getattr(ctrl.context.data, "setup_error", None)
        has_view = os.path.isfile(os.path.join(sim_cfg_dir, "custom_view", "index.html"))
        if setup_error and has_view:
            report.error(area, f"Custom view setup failed: {setup_error}")
        elif setup_error:
            report.info(area, "No custom view in this workspace; the app tried the demo's app_logic.custom_view "
                              f"and got: {setup_error}. Harmless: no view link is shown.")

        home_steps: List[str] = []

        def observed() -> List[str]:
            return list(ctrl.context.steps.snapshot().get("observed") or [])

        def traceback_of(label: str, fn) -> str:
            ctrl.context.steps.begin_task(f"debug {label}")
            try:
                if _accepts_context(fn):
                    fn(ctrl.devices, ctrl.context)
                else:
                    fn(ctrl.devices)
                return "(the direct re-run passed: the failure depends on timing or state)"
            except Exception as e:
                return "".join(traceback.format_exception(e)).strip()[-2500:]
            finally:
                ctrl.context.steps.end_task()

        def run(label: str, task_type, key: str | None, seq_key: str, fn) -> None:
            nonlocal home_steps
            notices.clear()
            if not ctrl.start_task(task_type, manual_action_key=key):
                report.error(area, f"{label}: refused to start (state {state().value}).")
                return
            t0 = time.monotonic()
            if task_type == TaskType.PROD:
                wait_until(lambda: state() != ControllerState.BUSY
                           or ctrl.context.metrics.get_cycle_count() >= args.cycles, args.task_timeout)
                if state() == ControllerState.BUSY:
                    ctrl.stop_current_task()
            finished = wait_until(lambda: state() != ControllerState.BUSY, args.task_timeout)
            duration = time.monotonic() - t0
            if not finished:
                report.error(area, f"{label}: still running after {args.task_timeout:.0f} s in simulation.",
                             "A wait that never ends on a simulated device: a loop polling an input "
                             "(reads return None), or a time.sleep() far too long.")
                ctrl.abort_current_task()
                wait_until(lambda: state() != ControllerState.BUSY, 10)
                return
            steps = [s for s in observed() if s not in FRAMEWORK_STEPS]
            wrapper = f"Manual: {key}" if key else None
            if wrapper and steps == [wrapper]:
                # The framework wraps a manual action in its own top-level step
                # (core/Task.py, _run_manual_action): the action's steps sit one level below.
                history = ctrl.context.steps.snapshot().get("history") or []   # oldest first
                inner = [h["name"] for h in history
                         if h.get("depth") == 1 and str(h.get("key", h.get("path", ""))).startswith(wrapper + " / ")]
                steps = list(dict.fromkeys(inner))
                if facts["sequences"].get(seq_key):
                    report.warn(area, f"{label}: `sequences: {seq_key}` never advances: the app wraps every "
                                      f"manual action in a top-level '{wrapper}' step, so the action's own steps "
                                      f"are nested and the UI shows 'Off sequence'.",
                                "Framework limitation (core/Task.py _run_manual_action). Leave manual:<key> "
                                "sequences out of config.yaml until it is fixed.")
                    seq_key = ""
            if state() == ControllerState.FAULTED:
                message = next((n for n in notices if n.startswith("error")), "faulted")
                report.error(area, f"{label}: FAULTED: {message}", traceback_of(label, fn))
                ctrl.clear_faults()
                wait_until(lambda: state() == ControllerState.READY, 10)
                return
            extra = ""
            if task_type == TaskType.PROD:
                cycles = ctrl.context.metrics.get_cycle_count()
                extra = f", {cycles} cycle(s)"
                if cycles < args.cycles:
                    report.error(area, f"{label}: only {cycles} of {args.cycles} cycles completed.")
            report.info(area, f"{label}: OK in {duration:.1f} s{extra}. Steps: {' > '.join(steps) or '(none)'}")
            if task_type == TaskType.HOME:
                home_steps = steps
            declared = facts["sequences"].get(seq_key) or [] if seq_key else []
            if declared:
                ignore = set(home_steps) if task_type == TaskType.PROD else set()
                off = [s for s in steps if s not in declared and s not in ignore]
                if off:
                    report.warn(area, f"{label}: steps not in `sequences: {seq_key}`: {off} "
                                      f"(shown as 'Off sequence').")
                never = [s for s in declared if s not in steps]
                if never:
                    report.info(area, f"{label}: declared steps not reached in simulation: {never} "
                                      f"(fine for a conditional branch).")
            elif not steps:
                report.warn(area, f"{label}: opened no context.step(): the UI shows nothing while it runs.")

        run("HOME", TaskType.HOME, None, "home", sources.get("home"))
        run("CALIBRATION", TaskType.CALIBRATION, None, "calib", sources.get("calib"))
        run("SHIPMENT", TaskType.SHIPMENT, None, "shipment", sources.get("shipment"))
        for key in facts["action_keys"]:
            if key in actions:
                run(f"Manual action '{key}'", TaskType.MANUAL_ACTION, key, f"manual:{key}", actions[key])
        run("PROD", TaskType.PROD, None, "prod_cycle", sources.get("prod_cycle"))
    finally:
        try:
            ctrl.shutdown()
        except Exception:
            pass
        try:
            ctrl.close()
        except Exception:
            pass


# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workspace")
    parser.add_argument("--app", default=None)
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--task-timeout", type=float, default=60.0)
    parser.add_argument("--keep-sim", default=None)
    parser.add_argument("--static-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    ws = os.path.abspath(args.workspace)
    cfg_dir = config_dir_of(ws)
    if cfg_dir is None:
        raise SystemExit(f"{ws} holds no config.yaml (nor app_logic/config.yaml): not a workspace.")
    app = find_app(ws, args.app)
    os.environ.setdefault("MECADEMIC_DEMO_ROOT", tempfile.mkdtemp(prefix="check-workspace-logs-"))
    # The app logs to the console too; its errors already reach this report.
    os.environ.setdefault("MECADEMIC_CONSOLE_LOG_LEVEL", "CRITICAL")
    sys.path.insert(0, app)

    report = Report()
    report.info("Workspace", f"{ws} (config in {cfg_dir}); app {app}")
    static = static_checks(ws, cfg_dir, app, report)

    if not args.static_only:
        if args.keep_sim:
            target = os.path.abspath(args.keep_sim)
            if os.path.isdir(target) and os.listdir(target):
                raise SystemExit(f"--keep-sim {target} exists and is not empty.")
            os.makedirs(target, exist_ok=True)
        else:
            target = os.path.join(tempfile.mkdtemp(prefix="check-workspace-"), os.path.basename(ws) + "_sim")
        sim_cfg_dir, notes = make_simulated_copy(ws, cfg_dir, target, static["config"])
        report.info("Simulated run", f"Simulated copy: {target}" + (" (kept)" if args.keep_sim else ""))
        for note in notes:
            report.info("Simulated run", note)
        simulated_run(target, sim_cfg_dir, static, args, report)
        if not args.keep_sim:
            shutil.rmtree(os.path.dirname(target), ignore_errors=True)

    if args.json:
        print(json.dumps({"errors": report.errors, "items": report.items}, indent=2))
    else:
        report.print_text()
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
