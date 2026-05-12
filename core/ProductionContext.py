import copy
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import yaml


class ProductionContextError(Exception):
    """Raised when production context operations fail validation."""


class ProductionContext:
    """Thread-safe production params/variables registry with reset policies."""

    _ALLOWED_TYPES = {"int", "float", "bool", "str"}
    _ALLOWED_SCOPES = {"memory", "disk"}
    _NO_RESET_TOKEN = "none"
    _EDITABLE_FIELDS = {"value", "default", "editable_when_idle", "reset_on", "persist_scope", "description"}
    _MAX_CYCLE_HISTORY = 100
    _MAX_RUN_HISTORY = 20

    def __init__(self, logger, config_path: str = "production_context.yaml"):
        self.logger = logger
        self._lock = threading.RLock()
        self._config_path = config_path

        self._settings: Dict[str, Any] = {
            "storage_scope": "memory",
            "auto_persist": True,
            "default_reset_events": [],
        }
        self._params: Dict[str, Dict[str, Any]] = {}
        self._variables: Dict[str, Dict[str, Any]] = {}
        self._metadata: Dict[str, Any] = {
            "running": False,
            "cycle_count": 0,
            "last_cycle_start": None,
            "last_cycle_end": None,
            "last_cycle_duration_s": None,
            "total_cycle_time_s": 0.0,
            "cycle_history": [],
            "run_history": [],
            "last_prod_start": None,
            "last_prod_stop": None,
            "last_archived_run_start": None,
            "last_abort": None,
            "last_fault": None,
            "last_initialize": None,
            "last_error": None,
        }

        self._load_from_disk()

    def _utc_now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _parse_utc(self, value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except Exception:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _seconds_between(self, start: Any, end: Any) -> float | None:
        start_dt = self._parse_utc(start)
        end_dt = self._parse_utc(end)
        if start_dt is None or end_dt is None:
            return None
        return max(0.0, (end_dt - start_dt).total_seconds())

    def _build_current_run_summary_locked(self, end_event: str, end_time: str) -> Dict[str, Any] | None:
        run_start = self._metadata.get("last_prod_start")
        if not run_start:
            return None

        elapsed_s = self._seconds_between(run_start, end_time)
        cycle_count = int(self._metadata.get("cycle_count", 0))
        total_cycle_time_s = float(self._metadata.get("total_cycle_time_s", 0.0))

        return {
            "run_start": run_start,
            "run_end": end_time,
            "end_event": end_event,
            "running": bool(self._metadata.get("running")),
            "cycle_count": cycle_count,
            "part_count": self.get_variable("part_count", 0),
            "elapsed_production_s": elapsed_s,
            "last_cycle_duration_s": self._metadata.get("last_cycle_duration_s"),
            "average_cycle_duration_s": (total_cycle_time_s / cycle_count) if cycle_count > 0 else None,
            "total_cycle_time_s": total_cycle_time_s,
            "last_cycle_start": self._metadata.get("last_cycle_start"),
            "last_cycle_end": self._metadata.get("last_cycle_end"),
            "last_error": self._metadata.get("last_error"),
        }

    def _archive_run_locked(self, end_event: str, end_time: str) -> None:
        summary = self._build_current_run_summary_locked(end_event, end_time)
        if summary is None:
            return

        run_start = summary.get("run_start")
        if run_start and self._metadata.get("last_archived_run_start") == run_start:
            return

        run_history = self._metadata.setdefault("run_history", [])
        run_history.append(summary)
        if run_start:
            self._metadata["last_archived_run_start"] = run_start
        if len(run_history) > self._MAX_RUN_HISTORY:
            del run_history[:-self._MAX_RUN_HISTORY]

    def _default_document(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "settings": {
                "storage_scope": "memory",
                "auto_persist": True,
                "default_reset_events": ["prod_start"],
            },
            "params": {
                "cycle_wait_s": {
                    "type": "float",
                    "default": 1.0,
                    "value": 1.0,
                    "editable_when_idle": True,
                    "description": "Delay applied in demo prod cycle.",
                }
            },
            "variables": {
                "part_count": {
                    "type": "int",
                    "default": 0,
                    "value": 0,
                    "editable_when_idle": True,
                    "reset_on": ["prod_start"],
                    "description": "Parts processed since prod start.",
                },
            },
        }

    def _load_from_disk(self) -> None:
        with self._lock:
            if not os.path.exists(self._config_path):
                self.logger.warning(
                    f"Production context file '{self._config_path}' not found. Creating default file."
                )
                self._write_document_locked(self._default_document())

            try:
                with open(self._config_path, "r", encoding="utf-8") as fh:
                    raw = yaml.safe_load(fh) or {}
            except Exception as exc:
                self.logger.warning(
                    f"Failed to load production context config '{self._config_path}': {exc}. Using defaults."
                )
                raw = self._default_document()

            settings = raw.get("settings", {})
            self._settings["storage_scope"] = settings.get("storage_scope", "memory")
            if self._settings["storage_scope"] not in self._ALLOWED_SCOPES:
                self.logger.warning("Invalid storage_scope in production context. Falling back to 'memory'.")
                self._settings["storage_scope"] = "memory"

            self._settings["auto_persist"] = bool(settings.get("auto_persist", True))

            default_reset_events = settings.get("default_reset_events", [])
            self._settings["default_reset_events"] = self._normalize_event_list(default_reset_events)

            self._params = self._normalize_namespace(raw.get("params", {}), "params")
            self._variables = self._normalize_namespace(raw.get("variables", {}), "variables")

    def _normalize_namespace(self, namespace_data: Any, namespace: str) -> Dict[str, Dict[str, Any]]:
        if not isinstance(namespace_data, dict):
            return {}

        result: Dict[str, Dict[str, Any]] = {}
        for key, entry in namespace_data.items():
            if not isinstance(entry, dict):
                self.logger.warning(f"Ignoring malformed {namespace}.{key} entry: expected mapping.")
                continue

            type_name = str(entry.get("type", "str"))
            if type_name not in self._ALLOWED_TYPES:
                self.logger.warning(f"Unsupported type '{type_name}' in {namespace}.{key}, using 'str'.")
                type_name = "str"

            default_raw = entry.get("default", "" if type_name == "str" else 0)
            default_value = self._coerce(type_name, default_raw)

            current_raw = entry.get("value", default_value)
            current_value = self._coerce(type_name, current_raw)

            reset_on = entry.get("reset_on")

            persist_scope = str(entry.get("persist_scope", self._settings["storage_scope"]))
            if persist_scope not in self._ALLOWED_SCOPES:
                persist_scope = self._settings["storage_scope"]

            result[str(key)] = {
                "type": type_name,
                "default": default_value,
                "value": current_value,
                "editable_when_idle": bool(entry.get("editable_when_idle", True)),
                "reset_on": self._normalize_event_list(reset_on) if reset_on is not None else None,
                "persist_scope": persist_scope,
                "description": str(entry.get("description", "")),
            }

        return result

    def _coerce(self, type_name: str, value: Any) -> Any:
        try:
            if type_name == "int":
                if isinstance(value, bool):
                    return int(value)
                return int(value)
            if type_name == "float":
                return float(value)
            if type_name == "bool":
                if isinstance(value, bool):
                    return value
                if isinstance(value, str):
                    normalized = value.strip().lower()
                    if normalized in {"1", "true", "yes", "on"}:
                        return True
                    if normalized in {"0", "false", "no", "off"}:
                        return False
                    raise ValueError(f"Cannot coerce '{value}' to bool")
                return bool(value)
            return str(value)
        except Exception as exc:
            raise ProductionContextError(f"Invalid value '{value}' for type '{type_name}': {exc}") from exc

    def _normalize_event_list(self, raw_events: Any) -> List[str]:
        """Normalize reset event config.

        Supported forms:
        - ["prod_start", "abort"]
        - "none" (explicitly disable resets)
        - ["none"] (same behavior)
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
            events = self._settings.get("default_reset_events", [])
        if not isinstance(events, list):
            events = []

        normalized_events = [str(v).strip().lower() for v in events]
        if self._NO_RESET_TOKEN in normalized_events:
            return False

        return str(event).strip().lower() in normalized_events

    def _write_document_locked(self, doc: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self._config_path) or ".", exist_ok=True)
        temp_path = f"{self._config_path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(doc, fh, sort_keys=False)
        os.replace(temp_path, self._config_path)

    def _to_document_locked(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "settings": copy.deepcopy(self._settings),
            "params": copy.deepcopy(self._params),
            "variables": copy.deepcopy(self._variables),
        }

    def _persist_locked(self) -> None:
        if self._settings.get("storage_scope") != "disk":
            return
        self._write_document_locked(self._to_document_locked())

    def apply_event(self, event: str) -> None:
        with self._lock:
            changed = False
            for namespace in (self._params, self._variables):
                for entry in namespace.values():
                    if self._entry_should_reset(entry, event):
                        entry["value"] = copy.deepcopy(entry["default"])
                        changed = True

            now = self._utc_now()
            if event == "initialize":
                self._metadata["last_initialize"] = now
            elif event == "prod_start":
                if self._metadata.get("last_prod_start") and (
                    self._metadata.get("cycle_count", 0) > 0
                    or self._metadata.get("last_cycle_start")
                    or self._metadata.get("last_cycle_end")
                ) and self._metadata.get("last_archived_run_start") != self._metadata.get("last_prod_start"):
                    self._archive_run_locked("superseded", now)
                self._metadata["last_prod_start"] = now
                self._metadata["last_prod_stop"] = None
                self._metadata["cycle_count"] = 0
                self._metadata["last_cycle_start"] = None
                self._metadata["last_cycle_end"] = None
                self._metadata["last_cycle_duration_s"] = None
                self._metadata["total_cycle_time_s"] = 0.0
                self._metadata["cycle_history"] = []
            elif event == "prod_stop":
                self._metadata["last_prod_stop"] = now
                self._archive_run_locked(event, now)
            elif event == "abort":
                self._metadata["last_abort"] = now
                self._archive_run_locked(event, now)
            elif event == "fault":
                self._metadata["last_fault"] = now
                self._archive_run_locked(event, now)

            if changed and self._settings.get("auto_persist", True):
                self._persist_locked()

    def mark_prod_running(self, running: bool) -> None:
        with self._lock:
            self._metadata["running"] = bool(running)

    def mark_cycle_start(self) -> None:
        with self._lock:
            self._metadata["running"] = True
            self._metadata["last_cycle_start"] = self._utc_now()

    def mark_cycle_end(self) -> None:
        with self._lock:
            cycle_start = self._metadata.get("last_cycle_start")
            cycle_end = self._utc_now()
            duration_s = self._seconds_between(cycle_start, cycle_end)
            self._metadata["cycle_count"] += 1
            self._metadata["last_cycle_end"] = cycle_end
            self._metadata["last_cycle_duration_s"] = duration_s
            if duration_s is not None:
                self._metadata["total_cycle_time_s"] = float(self._metadata.get("total_cycle_time_s", 0.0)) + duration_s
            history = self._metadata.setdefault("cycle_history", [])
            history.append(
                {
                    "cycle_number": self._metadata["cycle_count"],
                    "started_at": cycle_start,
                    "ended_at": cycle_end,
                    "duration_s": duration_s,
                }
            )
            if len(history) > self._MAX_CYCLE_HISTORY:
                del history[:-self._MAX_CYCLE_HISTORY]
            if self._settings.get("auto_persist", True):
                self._persist_locked()

    def mark_cycle_error(self, error_text: str) -> None:
        with self._lock:
            self._metadata["last_error"] = str(error_text)

    def _build_metrics_locked(self) -> Dict[str, Any]:
        now = self._utc_now()
        prod_start = self._metadata.get("last_prod_start")
        prod_stop = self._metadata.get("last_prod_stop")
        cycle_start = self._metadata.get("last_cycle_start")
        cycle_end = self._metadata.get("last_cycle_end")

        elapsed_s = None
        if prod_start:
            elapsed_s = self._seconds_between(prod_start, now if self._metadata.get("running") else (prod_stop or now))

        current_cycle_elapsed_s = None
        if self._metadata.get("running") and cycle_start:
            current_cycle_elapsed_s = self._seconds_between(cycle_start, now)

        completed_cycles = int(self._metadata.get("cycle_count", 0))
        total_cycle_time_s = float(self._metadata.get("total_cycle_time_s", 0.0))
        average_cycle_duration_s = None
        if completed_cycles > 0:
            average_cycle_duration_s = total_cycle_time_s / completed_cycles

        return {
            "running": bool(self._metadata.get("running")),
            "completed_cycles": completed_cycles,
            "part_count": self.get_variable("part_count", 0),
            "elapsed_production_s": elapsed_s,
            "current_cycle_elapsed_s": current_cycle_elapsed_s,
            "last_cycle_duration_s": self._metadata.get("last_cycle_duration_s"),
            "average_cycle_duration_s": average_cycle_duration_s,
            "total_cycle_time_s": total_cycle_time_s,
            "last_cycle_start": cycle_start,
            "last_cycle_end": cycle_end,
            "last_prod_start": prod_start,
            "last_prod_stop": prod_stop,
            "last_abort": self._metadata.get("last_abort"),
            "last_fault": self._metadata.get("last_fault"),
            "last_error": self._metadata.get("last_error"),
            "cycle_history": copy.deepcopy(self._metadata.get("cycle_history", [])),
            "run_history": copy.deepcopy(self._metadata.get("run_history", [])),
        }

    def get_param(self, name: str, default: Any = None) -> Any:
        with self._lock:
            entry = self._params.get(name)
            return copy.deepcopy(entry["value"]) if entry else default

    def get_variable(self, name: str, default: Any = None) -> Any:
        with self._lock:
            entry = self._variables.get(name)
            return copy.deepcopy(entry["value"]) if entry else default

    def set_param(self, name: str, value: Any, force: bool = False) -> None:
        self._set_entry("params", name, {"value": value}, force=force)

    def set_variable(self, name: str, value: Any, force: bool = False) -> None:
        self._set_entry("variables", name, {"value": value}, force=force)

    def _set_entry(self, namespace_name: str, key: str, payload: Dict[str, Any], force: bool = False, locked: bool = False) -> None:
        namespace = self._params if namespace_name == "params" else self._variables
        with self._lock:
            if key not in namespace:
                raise ProductionContextError(f"Unknown {namespace_name} key '{key}'")

            entry = namespace[key]
            if locked and not force:
                raise ProductionContextError("Context is locked while production is running")
            if not entry.get("editable_when_idle", True) and not force:
                raise ProductionContextError(f"{namespace_name}.{key} is not editable")

            for field, field_value in payload.items():
                if field not in self._EDITABLE_FIELDS:
                    raise ProductionContextError(f"Field '{field}' is not editable")

                if field == "value":
                    entry["value"] = self._coerce(entry["type"], field_value)
                elif field == "default":
                    entry["default"] = self._coerce(entry["type"], field_value)
                elif field == "editable_when_idle":
                    entry["editable_when_idle"] = bool(field_value)
                elif field == "reset_on":
                    if field_value is None:
                        entry["reset_on"] = None
                    elif isinstance(field_value, list) or isinstance(field_value, str):
                        entry["reset_on"] = self._normalize_event_list(field_value)
                    else:
                        raise ProductionContextError("reset_on must be null, string, or list")
                elif field == "persist_scope":
                    scope = str(field_value)
                    if scope not in self._ALLOWED_SCOPES:
                        raise ProductionContextError("persist_scope must be 'memory' or 'disk'")
                    entry["persist_scope"] = scope
                elif field == "description":
                    entry["description"] = str(field_value)

            if self._settings.get("auto_persist", True):
                self._persist_locked()

    def update_from_payload(self, payload: Dict[str, Any], locked: bool) -> Tuple[List[str], List[str]]:
        updated: List[str] = []
        errors: List[str] = []

        for namespace_name in ("params", "variables"):
            entries = payload.get(namespace_name, {})
            if not isinstance(entries, dict):
                continue
            for key, value in entries.items():
                try:
                    if isinstance(value, dict):
                        # UI/API updates are value-only for existing entries.
                        if set(value.keys()) != {"value"}:
                            raise ProductionContextError(
                                "Only 'value' can be modified for existing entries"
                            )
                        self._set_entry(namespace_name, key, {"value": value.get("value")}, locked=locked)
                    else:
                        self._set_entry(namespace_name, key, {"value": value}, locked=locked)
                    updated.append(f"{namespace_name}.{key}")
                except Exception as exc:
                    errors.append(f"{namespace_name}.{key}: {exc}")

        settings_update = payload.get("settings")
        if isinstance(settings_update, dict):
            try:
                self.update_settings(settings_update, locked=locked)
                updated.append("settings")
            except Exception as exc:
                errors.append(f"settings: {exc}")

        return updated, errors

    def update_settings(self, settings_update: Dict[str, Any], locked: bool) -> None:
        with self._lock:
            if locked:
                raise ProductionContextError("Context settings are locked while production is running")

            if "storage_scope" in settings_update:
                scope = str(settings_update["storage_scope"])
                if scope not in self._ALLOWED_SCOPES:
                    raise ProductionContextError("storage_scope must be 'memory' or 'disk'")
                self._settings["storage_scope"] = scope

            if "auto_persist" in settings_update:
                self._settings["auto_persist"] = bool(settings_update["auto_persist"])

            if "default_reset_events" in settings_update:
                events = settings_update["default_reset_events"]
                if not isinstance(events, list) and not isinstance(events, str):
                    raise ProductionContextError("default_reset_events must be a string or list")
                self._settings["default_reset_events"] = self._normalize_event_list(events)

            if self._settings.get("auto_persist", True):
                self._persist_locked()

    def reset(self, namespace_name: str, key: str | None = None, event: str | None = None, locked: bool = False) -> None:
        with self._lock:
            if locked:
                raise ProductionContextError("Context is locked while production is running")

            if namespace_name not in {"params", "variables", "all"}:
                raise ProductionContextError("namespace must be params, variables, or all")

            if namespace_name == "all":
                targets = [("params", self._params), ("variables", self._variables)]
            elif namespace_name == "params":
                targets = [("params", self._params)]
            else:
                targets = [("variables", self._variables)]

            for _, namespace in targets:
                if key is not None:
                    if key not in namespace:
                        raise ProductionContextError(f"Unknown key '{key}'")
                    namespace[key]["value"] = copy.deepcopy(namespace[key]["default"])
                else:
                    for entry in namespace.values():
                        if event is None or self._entry_should_reset(entry, event):
                            entry["value"] = copy.deepcopy(entry["default"])

            if self._settings.get("auto_persist", True):
                self._persist_locked()

    def snapshot(self, controller_state: str) -> Dict[str, Any]:
        with self._lock:
            locked = controller_state.lower() == "busy"
            return {
                "state": controller_state,
                "locked": locked,
                "settings": copy.deepcopy(self._settings),
                "metadata": copy.deepcopy(self._metadata),
                "metrics": self._build_metrics_locked(),
                "params": copy.deepcopy(self._params),
                "variables": copy.deepcopy(self._variables),
            }
