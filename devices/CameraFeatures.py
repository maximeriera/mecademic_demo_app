"""
devices/CameraFeatures.py
-------------------------
Camera features the Features dialog browses, applies and saves (Settings >
Devices > camera > Features...), shared by the GigE and the simulated camera.

A camera takes its startup features from one of two places, written at every
INITIALIZE in their order, before the stream starts:

* ``features:`` in config.yaml. The dialog can apply changes, not save them.
* ``features_file:``, a YAML mapping the app reads at every INITIALIZE and
  rewrites on Save, so it is the camera's only source of features::

      # Features of `camera`, written by the app (...)
      ExposureAuto: 'Off'
      ExposureTime: 20000.0

Apply writes the camera now and marks the change unsaved; Save merges the
unsaved changes into the file (an updated feature keeps its place, a new one
goes last, in the order it was applied); Discard puts the camera back.

A driver mixes in :class:`FeatureSettingsMixin` before :class:`Device`, calls
:meth:`_init_feature_settings` from its constructor and
:meth:`_load_and_apply_features` from ``initialize()``, and provides
``set_feature``, ``get_feature`` and ``feature_tree(visibility)``.
"""

from __future__ import annotations

import math
import os
import threading
from typing import Any, Dict, List

import yaml

#: GenICam visibility levels the dialog offers, from fewest features to most.
VISIBILITIES = ("Beginner", "Expert", "Guru")

#: A camera's ``trigger``: free-run, one TriggerSoftware command per frame, or an input line.
TRIGGER_MODES = ("off", "software", "hardware")
#: GenICam TriggerActivation values for a hardware trigger.
TRIGGER_ACTIVATIONS = ("RisingEdge", "FallingEdge", "AnyEdge", "LevelHigh", "LevelLow")


def trigger_features(trigger: str, source: str = "Line0", activation: str = "RisingEdge") -> list:
    """``(feature, value, optional)`` to write, in order, for a trigger mode (GenICam SFNC names).

    Frames start on the trigger (TriggerSelector FrameStart). TriggerMode goes
    Off first: most cameras only change the source with the trigger off (a
    GigE camera that locks it until the trigger is on gets TriggerMode On
    first instead, see ``GigEVisionDevice._set_trigger_setting``). An
    optional feature the camera lacks is skipped.
    """
    if trigger == "off":
        return [("TriggerMode", "Off", True)]
    steps = [("TriggerSelector", "FrameStart", True),
             ("TriggerMode", "Off", False),
             ("TriggerSource", "Software" if trigger == "software" else source, False)]
    if trigger == "hardware":
        steps.append(("TriggerActivation", activation, True))
    steps.append(("TriggerMode", "On", False))
    return steps


def check_trigger(trigger: str, activation: str) -> None:
    """ValueError for a ``trigger`` or ``trigger_activation`` a camera does not take."""
    if trigger not in TRIGGER_MODES:
        raise ValueError(f"trigger must be one of {', '.join(TRIGGER_MODES)}, got {trigger!r}")
    if activation not in TRIGGER_ACTIVATIONS:
        raise ValueError(f"trigger_activation must be one of {', '.join(TRIGGER_ACTIVATIONS)}, got {activation!r}")

_FORGET = object()   # pending marker: drop the feature from the file on Save


def _same(a: Any, b: Any) -> bool:
    """Equal values, with floats compared to their last few bits (cameras round)."""
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) \
            and not isinstance(b, bool):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    return a == b


class FeatureSettingsMixin:
    """Startup features, plus the Apply / Save / Discard / Forget of the Features dialog."""

    def _init_feature_settings(self, features: dict | None, features_file: str | None) -> None:
        self._features_file = features_file
        self._saved: Dict[str, Any] = dict(features or {})   # file (or config) features, in order
        self._pending: Dict[str, Any] = {}                   # unsaved: name -> value, or _FORGET
        self._original: Dict[str, Any] = {}                  # camera value before the first unsaved Apply
        self._features_mtime: float | None = None            # the file as INITIALIZE read it
        self._settings_lock = threading.RLock()

    # --- Startup -------------------------------------------------------------------

    @property
    def _features_source(self) -> str:
        return os.path.basename(self._features_file) if self._features_file else "config.yaml"

    def _read_features_file(self) -> Dict[str, Any]:
        path = self._features_file
        if not os.path.isfile(path):
            self._features_mtime = None
            return {}   # created by the first Save
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError) as e:
            raise RuntimeError(f"cannot read {path}: {e}") from e
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise RuntimeError(f"{path} must map feature names to values, not hold a {type(data).__name__}")
        self._features_mtime = os.path.getmtime(path)
        return {str(name): value for name, value in data.items()}

    def _load_and_apply_features(self) -> None:
        """Read the features (the file again, or config.yaml's) and write them in order.

        Call it from ``initialize()`` once ``set_feature`` works, before the
        stream starts. Raises RuntimeError naming the source and the feature.
        """
        with self._settings_lock:
            if self._features_file:
                self._saved = self._read_features_file()
            self._pending.clear()
            self._original.clear()
            for name, value in self._saved.items():
                try:
                    self.set_feature(name, value)
                except Exception as e:
                    raise RuntimeError(f"{self._features_source}: feature {name}={value!r}: {e}") from e
        if self._saved:
            self.logger.info(f"[{self.device_id}] {len(self._saved)} feature(s) set from {self._features_source}.")

    def _features_summary(self) -> str | None:
        """For the device card: the file and its size, or config.yaml's features inline."""
        if self._features_file:
            return f"{os.path.basename(self._features_file)}, {len(self._saved)} saved"
        return ", ".join(f"{k}={v}" for k, v in self._saved.items()) or None

    # --- Features dialog ---------------------------------------------------------------

    def settings(self, visibility: str = "Beginner") -> dict:
        """The feature tree up to *visibility*, each feature with its saved and unsaved state."""
        if visibility not in VISIBILITIES:
            raise ValueError(f"visibility must be one of {', '.join(VISIBILITIES)}, got {visibility!r}")
        groups = self.feature_tree(visibility)
        with self._settings_lock:
            for group in groups:
                for feature in group["features"]:
                    name = feature["name"]
                    pending = self._pending.get(name)
                    feature["in_file"] = name in self._saved
                    feature["saved"] = self._saved.get(name)
                    feature["pending"] = name in self._pending
                    feature["forget"] = pending is _FORGET
            return {
                "groups": groups,
                "visibility": visibility,
                "source": "file" if self._features_file else "config",
                "file": self._features_file,
                "can_save": bool(self._features_file),
                "unsaved": len(self._pending),
                "saved_count": len(self._saved),
            }

    def _read_back(self, name: str, fallback: Any) -> Any:
        try:
            return self.get_feature(name)
        except Exception:
            return fallback

    def write_setting(self, name: str, value: Any) -> dict:
        """Apply: write the camera now and mark the change unsaved. Errors as ``set_feature``."""
        with self._settings_lock:
            before = self._read_back(name, None)
            self.set_feature(name, value)
            after = self._read_back(name, value)   # what the camera kept (it may round to its step)
            self._original.setdefault(name, before)
            baseline = self._saved[name] if name in self._saved else self._original[name]
            if _same(after, baseline):
                self._pending.pop(name, None)   # back where it started: nothing to save
            else:
                self._pending[name] = after
            return {"name": name, "value": after, "unsaved": len(self._pending)}

    def forget_setting(self, name: str) -> dict:
        """Drop a feature from the file on the next Save. The camera keeps its value."""
        with self._settings_lock:
            if not self._features_file:
                raise ValueError("features come from config.yaml (features:): set features_file to save from the UI.")
            if name not in self._saved:
                raise ValueError(f"'{name}' is not in {self._features_source}.")
            self._pending[name] = _FORGET
            return {"name": name, "unsaved": len(self._pending)}

    def save_settings(self) -> dict:
        """Merge the unsaved changes into the features file. ValueError if it cannot be written."""
        with self._settings_lock:
            if not self._features_file:
                raise ValueError("features come from config.yaml (features:): set features_file to save from the UI.")
            path = self._features_file
            if not self._pending:
                return {"saved": 0, "file": path}
            on_disk = os.path.getmtime(path) if os.path.isfile(path) else None
            if on_disk != self._features_mtime:
                raise ValueError(f"{os.path.basename(path)} changed on disk: INITIALIZE to reload it, or Discard.")
            merged = dict(self._saved)
            for name, value in self._pending.items():
                if value is _FORGET:
                    merged.pop(name, None)
                else:
                    merged[name] = value   # an existing feature keeps its place, a new one goes last
            self._write_features_file(path, merged)
            count = len(self._pending)
            self._saved = merged
            self._features_mtime = os.path.getmtime(path)
            self._pending.clear()
            self._original.clear()
        self.logger.info(f"[{self.device_id}] {count} feature change(s) saved to {path}.")
        return {"saved": count, "file": path}

    def _write_features_file(self, path: str, features: Dict[str, Any]) -> None:
        header = (f"# Features of `{self.device_id}`, written by the app "
                  f"(Settings > Devices > {self.device_id} > Features > Save).\n"
                  "# Applied in this order at every INITIALIZE. Edit it by hand, then INITIALIZE to reload it.\n")
        body = yaml.safe_dump(features, sort_keys=False, default_flow_style=False, allow_unicode=True) \
            if features else "{}\n"
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        temp_path = f"{path}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as fh:
                fh.write(header + body)
            os.replace(temp_path, path)
        except OSError as e:
            raise ValueError(f"cannot write {path}: {e}") from e

    def discard_settings(self) -> dict:
        """Put every unsaved feature back to its saved value, or to the value it had before."""
        failed: List[str] = []
        with self._settings_lock:
            # Last applied first: ExposureTime goes back before ExposureAuto turns auto on again.
            for name, value in reversed(list(self._pending.items())):
                if value is _FORGET:
                    continue   # the camera never changed
                target = self._saved[name] if name in self._saved else self._original.get(name)
                if target is None:
                    failed.append(f"{name}: its previous value is unknown")
                    continue
                try:
                    self.set_feature(name, target)
                except Exception as e:
                    failed.append(f"{name}: {e}")
            count = len(self._pending)
            self._pending.clear()
            self._original.clear()
        return {"discarded": count, "failed": failed}
