"""
core/ContextStore.py
--------------------
Optional, user-defined params and variables, declared in the workspace's
``context.yaml``.

* **params**    — tunable inputs read by task logic (speeds, delays, recipes).
* **variables** — runtime values task logic carries between calls (counters).

The feature is optional. A workspace without a context file gets a disabled
store: ``get_*`` returns the caller's default, ``set_*`` raises a clear
:class:`ContextError`, and nothing is ever written to disk.

The definition file is hand-written and never rewritten at runtime, so its
comments survive. Values of entries marked ``persist: true`` are saved to a
machine-written sidecar next to it (``context.yaml`` -> ``context.state.json``)
and restored on the next start.
"""

import copy
import json
import math
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import yaml


class ContextError(Exception):
    """Raised when a context read/write/reset is rejected."""


class ContextStore:
    """Thread-safe params/variables registry with reset events and persistence."""

    NAMESPACES = ("params", "variables")
    _SINGULAR = {"params": "param", "variables": "variable"}
    _ALLOWED_TYPES = {"int", "float", "bool", "str"}
    _NO_RESET_TOKEN = "none"
    _STATE_VERSION = 1

    def __init__(self, logger, path: str | None = None):
        self.logger = logger
        self._lock = threading.RLock()
        # Serialises state-file writes. Held only for the write itself, never
        # together with _lock, so a slow disk cannot stall get_param() callers.
        self._io_lock = threading.Lock()
        self._state_seq = 0          # bumped each time a state document is built
        self._state_written_seq = 0  # last document actually written
        self._state_write_failed = False

        self._path = os.path.abspath(path) if path else None
        self._state_path: str | None = None
        self._enabled = False
        self._load_error: str | None = None
        self._legacy_sequences: Any = None
        self._default_reset_events: List[str] = []
        self._entries: Dict[str, Dict[str, Dict[str, Any]]] = {ns: {} for ns in self.NAMESPACES}

        if self._path:
            self._load()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def path(self) -> str | None:
        return self._path

    @property
    def legacy_sequences(self) -> Any:
        """Top-level ``sequences:`` found in a legacy context file, or None."""
        return self._legacy_sequences

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load(self) -> None:
        path = self._path
        if not os.path.isfile(path):
            self._load_error = f"Context file not found: {path}"
            self.logger.warning(self._load_error)
            return

        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh)
        except Exception as exc:
            self._load_error = f"Failed to parse {os.path.basename(path)}: {exc}"
            self.logger.error("Context disabled. %s", self._load_error)
            return

        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            self._load_error = f"{os.path.basename(path)} must be a mapping with params/variables sections."
            self.logger.error("Context disabled. %s", self._load_error)
            return

        legacy_fields: set[str] = set()
        settings = raw.get("settings") or {}
        if not isinstance(settings, dict):
            self.logger.warning("Context 'settings' must be a mapping. Ignoring.")
            settings = {}
        for legacy_key in ("storage_scope", "auto_persist"):
            if legacy_key in settings:
                legacy_fields.add(f"settings.{legacy_key}")

        self._default_reset_events = self._normalize_event_list(settings.get("default_reset_events"))
        # Legacy: storage_scope: disk persisted every entry.
        default_persist = str(settings.get("storage_scope", "")).strip().lower() == "disk"

        for namespace in self.NAMESPACES:
            self._entries[namespace] = self._normalize_namespace(
                raw.get(namespace), namespace, default_persist, legacy_fields
            )

        if "sequences" in raw:
            legacy_fields.add("sequences")
            self._legacy_sequences = raw.get("sequences")

        if legacy_fields:
            self.logger.warning(
                "Context file %s uses legacy fields (%s). They still load, but see the README "
                "for the current schema: sequences belong in config.yaml, `value`/`persist_scope`/"
                "`storage_scope` are replaced by `default` and `persist`.",
                path, ", ".join(sorted(legacy_fields)),
            )

        self._state_path = os.path.splitext(path)[0] + ".state.json"
        self._enabled = True
        self._load_state()

        persisted = sum(1 for ns in self.NAMESPACES for e in self._entries[ns].values() if e["persist"])
        self.logger.info(
            "Context loaded from %s: %d param(s), %d variable(s), %d persisted to %s.",
            path, len(self._entries["params"]), len(self._entries["variables"]),
            persisted, os.path.basename(self._state_path),
        )

    def _normalize_namespace(self, namespace_data: Any, namespace: str,
                             default_persist: bool, legacy_fields: set) -> Dict[str, Dict[str, Any]]:
        if namespace_data is None:
            return {}
        if not isinstance(namespace_data, dict):
            self.logger.warning(f"Context '{namespace}' must be a mapping. Ignoring.")
            return {}

        result: Dict[str, Dict[str, Any]] = {}
        for key, entry in namespace_data.items():
            if not isinstance(entry, dict):
                self.logger.warning(f"Ignoring malformed {namespace}.{key} entry: expected mapping.")
                continue
            try:
                result[str(key)] = self._normalize_entry(entry, default_persist, legacy_fields)
            except ContextError as exc:
                # One bad entry must not take the whole context (or the
                # controller) down with it.
                self.logger.warning(f"Ignoring {namespace}.{key}: {exc}")
        return result

    def _normalize_entry(self, entry: Dict[str, Any], default_persist: bool,
                         legacy_fields: set) -> Dict[str, Any]:
        type_name = str(entry.get("type", "str"))
        if type_name not in self._ALLOWED_TYPES:
            raise ContextError(f"unsupported type '{type_name}' (use int, float, bool or str)")

        default_value = self._coerce(type_name, entry.get("default", "" if type_name == "str" else 0))

        value = default_value
        if "value" in entry:
            # Legacy: the runtime value used to live in the definition file.
            legacy_fields.add("value")
            value = self._coerce(type_name, entry["value"])

        if "persist" in entry:
            persist = self._coerce("bool", entry["persist"])
        elif "persist_scope" in entry:
            legacy_fields.add("persist_scope")
            persist = str(entry["persist_scope"]).strip().lower() == "disk"
        else:
            persist = default_persist

        reset_on = entry.get("reset_on")
        return {
            "type": type_name,
            "default": default_value,
            "value": value,
            "editable_when_idle": self._coerce("bool", entry.get("editable_when_idle", True)),
            "reset_on": self._normalize_event_list(reset_on) if reset_on is not None else None,
            "persist": persist,
            "description": str(entry.get("description", "") or ""),
        }

    def _load_state(self) -> None:
        if not self._state_path or not os.path.isfile(self._state_path):
            return
        try:
            with open(self._state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            values = data.get("values", {}) if isinstance(data, dict) else {}
        except Exception as exc:
            self.logger.warning(f"Ignoring unreadable context state file {self._state_path}: {exc}")
            return

        restored = 0
        ignored: List[str] = []
        for namespace in self.NAMESPACES:
            stored = values.get(namespace) or {}
            if not isinstance(stored, dict):
                continue
            for key, raw_value in stored.items():
                entry = self._entries[namespace].get(key)
                if entry is None or not entry["persist"]:
                    ignored.append(f"{namespace}.{key}")
                    continue
                try:
                    entry["value"] = self._coerce(entry["type"], raw_value)
                    restored += 1
                except ContextError as exc:
                    self.logger.warning(f"Ignoring saved {namespace}.{key}: {exc}")

        if restored:
            self.logger.info(f"Restored {restored} persisted context value(s) from {self._state_path}.")
        if ignored:
            self.logger.info(
                "Ignored saved value(s) no longer defined or no longer persisted: %s", ", ".join(ignored)
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _coerce(self, type_name: str, value: Any) -> Any:
        try:
            if type_name == "int":
                if isinstance(value, float) and not value.is_integer():
                    raise ValueError("not a whole number")
                return int(value)
            if type_name == "float":
                result = float(value)
                if not math.isfinite(result):
                    raise ValueError("not a finite number")
                return result
            if type_name == "bool":
                if isinstance(value, bool):
                    return value
                if isinstance(value, str):
                    normalized = value.strip().lower()
                    if normalized in {"1", "true", "yes", "on"}:
                        return True
                    if normalized in {"0", "false", "no", "off"}:
                        return False
                    raise ValueError(f"cannot read '{value}' as a bool")
                return bool(value)
            return str(value)
        except Exception as exc:
            raise ContextError(f"invalid value '{value}' for type '{type_name}': {exc}") from exc

    def _normalize_event_list(self, raw_events: Any) -> List[str]:
        """Normalize reset event config.

        Supported forms:
        - ["prod_start", "abort"]
        - "prod_start"
        - "none" / ["none"] (explicitly disable resets)
        """
        if raw_events is None:
            return []
        if isinstance(raw_events, str):
            return [raw_events.strip().lower()]
        if isinstance(raw_events, list):
            return [str(v).strip().lower() for v in raw_events]
        return []

    def _entry_should_reset(self, entry: Dict[str, Any], event: str) -> bool:
        events = entry.get("reset_on")
        if events is None:
            events = self._default_reset_events
        if self._NO_RESET_TOKEN in events:
            return False
        return str(event).strip().lower() in events

    def _undefined(self, namespace: str, name: str) -> ContextError:
        kind = self._SINGULAR.get(namespace, "entry")
        if not self._enabled:
            return ContextError(
                f"{kind} '{name}' is not defined: no context file is loaded "
                f"(add context.yaml next to config.yaml)."
            )
        return ContextError(f"{kind} '{name}' is not defined in {os.path.basename(self._path)}.")

    def _namespace(self, namespace: str) -> Dict[str, Dict[str, Any]]:
        if namespace not in self._entries:
            raise ContextError(f"namespace must be one of {', '.join(self.NAMESPACES)}")
        return self._entries[namespace]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _state_document_locked(self) -> Tuple[int, Dict[str, Any]]:
        """Build the state document. Caller holds _lock; write it with _write_state()."""
        self._state_seq += 1
        return self._state_seq, {
            "version": self._STATE_VERSION,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "source": os.path.basename(self._path) if self._path else None,
            "values": {
                ns: {key: copy.deepcopy(e["value"]) for key, e in self._entries[ns].items() if e["persist"]}
                for ns in self.NAMESPACES
            },
        }

    def _write_state(self, pending: Tuple[int, Dict[str, Any]] | None) -> None:
        """Write a state document built by _state_document_locked(). Call WITHOUT _lock."""
        if pending is None or not self._state_path:
            return
        seq, doc = pending
        with self._io_lock:
            # Two writers can build documents in one order and reach here in
            # the other; never let the older one overwrite the newer.
            if seq <= self._state_written_seq:
                return
            temp_path = f"{self._state_path}.tmp"
            try:
                with open(temp_path, "w", encoding="utf-8") as fh:
                    json.dump(doc, fh, indent=2)
                os.replace(temp_path, self._state_path)
                self._state_written_seq = seq
                if self._state_write_failed:
                    self._state_write_failed = False
                    self.logger.info(f"Context state file writable again: {self._state_path}")
            except OSError as exc:
                if not self._state_write_failed:
                    self._state_write_failed = True
                    self.logger.warning(
                        f"Cannot write context state file {self._state_path}: {exc}. "
                        "Values stay in memory and will not survive a restart."
                    )

    # ------------------------------------------------------------------
    # Workspace API (code writes are always allowed)
    # ------------------------------------------------------------------

    def get(self, namespace: str, name: str, default: Any = None) -> Any:
        with self._lock:
            entry = self._namespace(namespace).get(name)
            return copy.deepcopy(entry["value"]) if entry else default

    def set(self, namespace: str, name: str, value: Any) -> None:
        pending = None
        with self._lock:
            entry = self._namespace(namespace).get(name)
            if entry is None:
                raise self._undefined(namespace, name)
            new_value = self._coerce(entry["type"], value)
            changed = new_value != entry["value"]
            entry["value"] = new_value
            if changed and entry["persist"]:
                pending = self._state_document_locked()
        self._write_state(pending)

    def get_param(self, name: str, default: Any = None) -> Any:
        return self.get("params", name, default)

    def get_variable(self, name: str, default: Any = None) -> Any:
        return self.get("variables", name, default)

    def set_param(self, name: str, value: Any) -> None:
        self.set("params", name, value)

    def set_variable(self, name: str, value: Any) -> None:
        self.set("variables", name, value)

    def values(self, namespace: str = "variables") -> Dict[str, Any]:
        """Plain ``{name: value}`` copy of one namespace."""
        with self._lock:
            return {key: copy.deepcopy(e["value"]) for key, e in self._namespace(namespace).items()}

    # ------------------------------------------------------------------
    # Framework / API
    # ------------------------------------------------------------------

    def apply_reset(self, event: str) -> None:
        """Reset every entry whose reset policy includes ``event``."""
        pending = None
        with self._lock:
            persisted_changed = False
            for namespace in self.NAMESPACES:
                for entry in self._entries[namespace].values():
                    if self._entry_should_reset(entry, event) and entry["value"] != entry["default"]:
                        entry["value"] = copy.deepcopy(entry["default"])
                        persisted_changed = persisted_changed or entry["persist"]
            if persisted_changed:
                pending = self._state_document_locked()
        self._write_state(pending)

    def reset(self, namespace: str, key: str | None = None, locked: bool = False) -> None:
        """Reset one entry, one namespace, or everything (``namespace="all"``) to defaults."""
        if locked:
            raise ContextError("Context is locked while a task is running.")
        if namespace != "all" and namespace not in self.NAMESPACES:
            raise ContextError("namespace must be params, variables, or all")

        pending = None
        with self._lock:
            targets = list(self.NAMESPACES) if namespace == "all" else [namespace]
            if key is not None:
                targets = [ns for ns in targets if key in self._entries[ns]]
                if not targets:
                    raise self._undefined(namespace, key)

            persisted_changed = False
            for ns in targets:
                entries = self._entries[ns]
                for name in ([key] if key is not None else list(entries)):
                    entry = entries[name]
                    if entry["value"] != entry["default"]:
                        entry["value"] = copy.deepcopy(entry["default"])
                        persisted_changed = persisted_changed or entry["persist"]
            if persisted_changed:
                pending = self._state_document_locked()
        self._write_state(pending)

    def update_from_payload(self, payload: Dict[str, Any], locked: bool) -> Tuple[List[str], List[str]]:
        """Apply value edits from the UI/API: ``{"params": {"x": {"value": 1}}, ...}``.

        Unlike workspace code, the UI may only edit entries marked
        ``editable_when_idle`` and only while no task is running.
        """
        updated: List[str] = []
        errors: List[str] = []

        for section in payload:
            if section not in self.NAMESPACES:
                errors.append(f"{section}: unsupported section (only params and variables can be edited)")

        for namespace in self.NAMESPACES:
            entries = payload.get(namespace, {})
            if not isinstance(entries, dict):
                if namespace in payload:
                    errors.append(f"{namespace}: expected a mapping of name to value")
                continue
            for key, value in entries.items():
                label = f"{namespace}.{key}"
                try:
                    if isinstance(value, dict):
                        if set(value.keys()) != {"value"}:
                            raise ContextError("only 'value' can be modified")
                        value = value["value"]
                    if locked:
                        raise ContextError("context is locked while a task is running")
                    with self._lock:
                        entry = self._entries[namespace].get(key)
                        if entry is None:
                            raise self._undefined(namespace, key)
                        if not entry["editable_when_idle"]:
                            raise ContextError("read-only (editable_when_idle: false)")
                    self.set(namespace, key, value)
                    updated.append(label)
                except ContextError as exc:
                    errors.append(f"{label}: {exc}")

        return updated, errors

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            has_persisted = any(e["persist"] for ns in self.NAMESPACES for e in self._entries[ns].values())
            return {
                "enabled": self._enabled,
                "source": self._path,
                "state_file": self._state_path if (self._enabled and has_persisted) else None,
                "load_error": self._load_error,
                "default_reset_events": list(self._default_reset_events),
                "params": copy.deepcopy(self._entries["params"]),
                "variables": copy.deepcopy(self._entries["variables"]),
            }
