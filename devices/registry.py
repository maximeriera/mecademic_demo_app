"""
devices/registry.py
-------------------
Device types the cell can declare in ``config.yaml``, and the validation of
that ``devices:`` block.

Each type is a :class:`DeviceSpec`: the driver class as a lazy
``"module:Class"`` target (so pyvisa, harvesters, pmclib... are only imported
when a config actually uses them) plus the schema of its config keys. The
controller builds every device from here; adding a driver is one
:func:`register_device` call.

Validation never raises. It returns the entries it could build and a list of
issues — errors (unknown type, missing or ill-typed value) that keep a device
from being created, and warnings (an unknown key, usually a typo, which is
ignored) — so the UI can show every problem at once.

A workspace can also bring its own driver without touching this file::

    devices:
      my_gripper:
        type: "custom"
        driver: "my_package.gripper:MyGripper"   # importable from the workspace
        port: "COM7"                              # passed to the constructor as-is
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Dict, List, Tuple

#: Marks a parameter without default: the config must give it.
REQUIRED = object()

#: Keys every entry may carry, whatever its type.
RESERVED_KEYS = ("type", "driver", "enabled")

#: ``type`` of a workspace-provided driver (needs ``driver: "module:Class"``).
CUSTOM_TYPE = "custom"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Param:
    """One config key of a device type.

    ``type`` is one of ``str``, ``int``, ``float``, ``bool``, ``list``,
    ``dict``, ``int_list`` (an int or a list of ints) or ``any``. A ``None``
    default makes the key optional and nullable.
    """
    type: str
    default: Any = REQUIRED
    choices: Tuple[Any, ...] | None = None
    doc: str = ""

    @property
    def required(self) -> bool:
        return self.default is REQUIRED


@dataclass(frozen=True)
class DeviceSpec:
    """A device type: its driver class and the schema of its config keys."""
    type: str
    target: str                       # "module:Class", imported on first use
    params: Dict[str, Param] = field(default_factory=dict)
    aliases: Tuple[str, ...] = ()
    doc: str = ""

    def load(self):
        module_name, _, class_name = self.target.partition(":")
        return getattr(import_module(module_name), class_name)


@dataclass
class DeviceEntry:
    """A validated ``devices:`` entry, ready to be instantiated."""
    name: str
    type: str
    target: str
    kwargs: Dict[str, Any]


_SPECS: Dict[str, DeviceSpec] = {}
_ALIASES: Dict[str, str] = {}


def register_device(spec: DeviceSpec) -> None:
    """Add (or replace) a device type. Names and aliases are case-insensitive."""
    key = spec.type.lower()
    _SPECS[key] = spec
    _ALIASES[key] = key
    for alias in spec.aliases:
        _ALIASES[alias.lower()] = key


def resolve_type(name: Any) -> DeviceSpec | None:
    key = _ALIASES.get(str(name or "").strip().lower())
    return _SPECS.get(key) if key else None


def known_types() -> List[str]:
    """Every accepted ``type`` value, aliases included."""
    return sorted(_ALIASES)


def describe_types() -> List[Dict[str, Any]]:
    """The registry as plain data (for the API and the docs)."""
    out = []
    for spec in sorted(_SPECS.values(), key=lambda s: s.type):
        out.append({
            "type": spec.type,
            "aliases": list(spec.aliases),
            "doc": spec.doc,
            "params": {
                name: {
                    "type": p.type,
                    "required": p.required,
                    "default": None if p.required else p.default,
                    "choices": list(p.choices) if p.choices else None,
                    "doc": p.doc,
                }
                for name, p in spec.params.items()
            },
        })
    return out


# --- Validation ---------------------------------------------------------------

def _coerce(param: Param, value: Any) -> Any:
    """Return *value* converted to the param's type, or raise ValueError."""
    if value is None:
        if param.default is None:
            return None
        raise ValueError("must not be empty")
    kind = param.type
    if kind == "any":
        return value
    if kind == "str":
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            return str(value)
    elif kind == "int":
        if isinstance(value, bool):
            raise ValueError("expected an integer, got a boolean")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip(), 0)
            except ValueError:
                pass
        raise ValueError(f"expected an integer, got {value!r}")
    elif kind == "float":
        if isinstance(value, bool):
            raise ValueError("expected a number, got a boolean")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                pass
        raise ValueError(f"expected a number, got {value!r}")
    elif kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
            return value.strip().lower() in _TRUE
        raise ValueError(f"expected true or false, got {value!r}")
    elif kind == "list":
        if isinstance(value, list):
            return value
    elif kind == "dict":
        if isinstance(value, dict):
            return value
    elif kind == "int_list":
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, list) and all(isinstance(v, int) and not isinstance(v, bool) for v in value):
            return value
        raise ValueError(f"expected an integer or a list of integers, got {value!r}")
    else:  # pragma: no cover - a spec bug, not a config one
        raise ValueError(f"unknown param type {kind!r}")
    raise ValueError(f"expected {kind}, got {type(value).__name__} {value!r}")


def _suggest(word: str, candidates) -> str:
    candidates = list(candidates)
    lowered = {c.lower(): c for c in candidates}
    if word.lower() in lowered:
        return f" (did you mean '{lowered[word.lower()]}'?)"
    match = difflib.get_close_matches(word, candidates, n=1, cutoff=0.6)
    return f" (did you mean '{match[0]}'?)" if match else ""


def validate_devices_config(raw: Any) -> Tuple[List[DeviceEntry], List[Dict[str, str]]]:
    """Validate a ``devices:`` block.

    Returns ``(entries, issues)``. Entries with an error are left out; an
    entry with ``enabled: false`` is skipped silently. Each issue is
    ``{"device", "level": "error" | "warning", "message"}``.
    """
    entries: List[DeviceEntry] = []
    issues: List[Dict[str, str]] = []

    def issue(device: str, level: str, message: str) -> None:
        issues.append({"device": device, "level": level, "message": message})

    if raw is None:
        return entries, issues
    if not isinstance(raw, dict):
        issue("", "error", "'devices' must be a mapping of device name to settings.")
        return entries, issues

    for name, settings in raw.items():
        name = str(name)
        if not isinstance(settings, dict):
            issue(name, "error", "settings must be a mapping (type, ip_address, ...).")
            continue

        enabled = settings.get("enabled", True)
        if enabled is False or (isinstance(enabled, str) and enabled.strip().lower() in _FALSE):
            continue

        type_name = settings.get("type")
        if not type_name:
            issue(name, "error", "missing 'type'.")
            continue

        if str(type_name).strip().lower() == CUSTOM_TYPE:
            target = str(settings.get("driver") or "").strip()
            module_name, sep, class_name = target.partition(":")
            if not (module_name and sep and class_name):
                issue(name, "error", "type 'custom' needs driver: \"module:ClassName\".")
                continue
            kwargs = {k: v for k, v in settings.items() if k not in RESERVED_KEYS}
            entries.append(DeviceEntry(name, CUSTOM_TYPE, target, kwargs))
            continue

        spec = resolve_type(type_name)
        if spec is None:
            issue(name, "error",
                  f"unknown type '{type_name}'{_suggest(str(type_name), known_types() + [CUSTOM_TYPE])}.")
            continue

        kwargs: Dict[str, Any] = {}
        failed = False
        for key, value in settings.items():
            if key in RESERVED_KEYS:
                continue
            param = spec.params.get(key)
            if param is None:
                issue(name, "warning",
                      f"unknown key '{key}' for type '{spec.type}', ignored"
                      f"{_suggest(str(key), spec.params)}.")
                continue
            try:
                coerced = _coerce(param, value)
            except ValueError as e:
                issue(name, "error", f"'{key}': {e}.")
                failed = True
                continue
            if param.choices and coerced not in param.choices:
                choices = ", ".join(repr(c) for c in param.choices)
                issue(name, "error", f"'{key}' must be one of {choices}, got {coerced!r}.")
                failed = True
                continue
            kwargs[key] = coerced

        for key, param in spec.params.items():
            if key in kwargs or key in settings:
                continue
            if param.required:
                issue(name, "error", f"missing required '{key}'" + (f" ({param.doc})" if param.doc else "") + ".")
                failed = True
            else:
                kwargs[key] = param.default

        if not failed:
            entries.append(DeviceEntry(name, spec.type, spec.target, kwargs))

    return entries, issues


def create_device(entry: DeviceEntry):
    """Instantiate a validated entry. Import and constructor errors propagate."""
    module_name, _, class_name = entry.target.partition(":")
    cls = getattr(import_module(module_name), class_name)
    return cls(name=entry.name, **entry.kwargs)


# --- Built-in device types ----------------------------------------------------

def _p(type_: str, default: Any = REQUIRED, doc: str = "", choices=None) -> Param:
    return Param(type_, default, tuple(choices) if choices else None, doc)


register_device(DeviceSpec(
    "mecademic", "devices.MecaRobot:MecaRobot",
    {"ip_address": _p("str", doc="robot IP address")},
    doc="Mecademic robot (mecademicpy).",
))
register_device(DeviceSpec(
    "asyril", "devices.Asyril:AsyrilEyePlus",
    {
        "ip_address": _p("str", doc="Eye+ controller IP address"),
        "recipe": _p("int", 0, "recipe identifier"),
        "port": _p("int", 7171, "TCP port"),
    },
    doc="Asyril Eye+ flexible feeder.",
))
register_device(DeviceSpec(
    "arduino", "devices.ArduinoBoard:ArduinoBoard",
    {"port": _p("str", "COM3", "serial port")},
    doc="Arduino I/O board over Firmata.",
))
register_device(DeviceSpec(
    "planarmotor", "devices.PlanarMotor:PlanarMotor",
    {"ip_address": _p("str", "192.168.10.200", "PMC controller IP address")},
    doc="Planar Motor system (pmclib).",
))
register_device(DeviceSpec(
    "iologik", "devices.IoLogikE1212:IoLogikE1212",
    {
        "ip_address": _p("str", doc="ioLogik IP address"),
        "port": _p("int", 502, "Modbus TCP port"),
        "slave_id": _p("int", 1, "Modbus unit ID"),
    },
    doc="Moxa ioLogik E1212 remote I/O (Modbus TCP).",
))
register_device(DeviceSpec(
    "brainboxes_ed_digital", "devices.BrainboxesED:BrainboxesEDDigital",
    {
        "ip_address": _p("str", doc="module IP address"),
        "address": _p("int", 1, "ED address"),
        "port": _p("int", 9500, "TCP port"),
        "timeout": _p("float", 5.0, "seconds per exchange"),
    },
    aliases=("brainboxes_dio", "ed_digital"),
    doc="Brainboxes ED digital I/O module.",
))
register_device(DeviceSpec(
    "lmi", "devices.LMISensor:LMISensor",
    {
        "ip_address": _p("str", doc="sensor IP address"),
        "control_port": _p("int", 3190),
        "data_port": _p("int", 3192),
        "health_port": _p("int", 3194),
        "delimiter": _p("str", ",", "must match the GoPxL setting"),
        "terminator": _p("str", "\r\n", "must match the GoPxL setting"),
        "timeout": _p("float", 5.0, "seconds"),
    },
    doc="LMI Gocator 3D sensor (Ethernet ASCII).",
))
register_device(DeviceSpec(
    "gige_camera", "devices.GigEVisionDevice:GigEVisionDevice",
    {
        "cti_path": _p("str", doc="path to the GenTL producer (.cti)"),
        "camera_index": _p("int", 0),
        "autostart_stream": _p("bool", False),
    },
    doc="GigE Vision camera (harvesters).",
))
register_device(DeviceSpec(
    "thorlabs_pm", "devices.ThorlabsPowerMeter:ThorlabsPowerMeter",
    {
        "resource": _p("str", None, "VISA resource; omit to auto-detect the only meter"),
        "backend": _p("str", None, "PyVISA backend, e.g. '@py'"),
        "timeout_ms": _p("int", 5000),
        "wavelength": _p("float", None, "nm, set at every Initialize"),
        "averaging": _p("int", None, "samples per reading"),
        "power_unit": _p("str", None, "'W' or 'DBM'"),
    },
    aliases=("thorlabs_power_meter",),
    doc="Thorlabs PM-series power meter (SCPI via PyVISA).",
))
register_device(DeviceSpec(
    "smaract_mcs2", "devices.SmarActMCS2:SmarActMCS2",
    {
        "ip_address": _p("str", doc="MCS2 IP address"),
        "port": _p("int", 55551),
        "timeout": _p("float", 5.0, "seconds per reply"),
        "channels": _p("int_list", None, "channels used; default: all"),
        "reference": _p("str", "if_needed", "referencing at Initialize",
                        choices=("if_needed", "always", "never")),
        "reference_reverse": _p("bool", False),
        "reference_timeout_s": _p("float", 120.0),
        "velocity_mm_s": _p("float", None),
        "acceleration_mm_s2": _p("float", None),
    },
    aliases=("mcs2", "smaract"),
    doc="SmarAct MCS2 positioner controller (Ethernet ASCII).",
))
register_device(DeviceSpec(
    "zaber", "devices.OLD_ZaberAxis:ZaberAxis",
    {
        "port": _p("str", "COM3", "serial port"),
        "axis_number": _p("int", 1),
    },
    doc="Zaber linear axis (legacy driver).",
))
register_device(DeviceSpec(
    "simulated", "devices.SimulatedDevice:SimulatedDevice",
    {
        "connect_delay": _p("float", 0.0, "seconds"),
        "fail_to_connect": _p("bool", False),
        "fail_rate": _p("float", 0.0, "0-1 chance per check of reporting a fault"),
        "info": _p("dict", None, "static info shown on the device card"),
        "supports_monitoring": _p("bool", False, "behave like a robot that keeps a monitoring link"),
    },
    aliases=("dummy", "fake"),
    doc="Hardware-free stand-in for any device.",
))
