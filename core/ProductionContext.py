import contextlib
import copy
import os
import threading
import time
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

    # --- Sequence step tracking ---
    _MAX_STEP_DEPTH = 16        # refuse deeper nesting rather than grow without bound
    _MAX_STEP_HISTORY = 200     # completed step frames retained
    _MAX_STEP_STATS = 200       # distinct step paths aggregated
    _MAX_STEP_NAME_LEN = 120

    def __init__(self, logger, config_path: str = "production_context.yaml"):
        self.logger = logger
        self._lock = threading.RLock()
        self._config_path = config_path

        # --- Sequence step state ---------------------------------------
        # Deliberately NOT stored in _metadata and NOT guarded by self._lock.
        # _metadata is deepcopied wholesale by snapshot(), and _lock is held
        # across blocking file I/O in _persist_locked() when storage_scope is
        # "disk" — so live step reads at 10 Hz (/api/status) would block behind
        # a YAML write. A separate lock, never held during I/O, keeps that path
        # fast and lets the monitor thread close a step the task thread opened.
        self._step_lock = threading.Lock()
        self._step_stack: List[Dict[str, Any]] = []      # open frames, innermost last
        self._step_stats: Dict[str, Dict[str, Any]] = {}  # path -> aggregate
        self._step_observed: List[str] = []               # depth-0 names, first-seen order
        self._step_history: List[Dict[str, Any]] = []     # completed frames, capped
        self._step_sequences: Dict[str, List[str]] = {}   # declared, from YAML
        self._step_sequences_runtime: Dict[str, List[str]] = {}  # declare_sequence() overrides
        self._step_task: Dict[str, Any] | None = None     # active task + sequence cursor
        self._step_epoch = 0        # bumped by abandon_open_steps() to stale outstanding tokens
        self._step_frame_seq = 0    # monotonic frame ids
        self._step_run_seq = 0      # monotonic task-run ids, see log_cycle_step_summary
        self._step_rev = 0          # bumped on every transition, for cheap change detection

        # IMMUTABLE live snapshot, or None when idle.
        #
        # INVARIANT: never mutate the dict this points at. Always build a new
        # dict and rebind. Readers (get_live_step) take no lock and rely on the
        # rebind being atomic; a future `self._step_live["x"] = y` would
        # silently break lock-free reads for every Flask thread.
        self._step_live: Dict[str, Any] | None = None

        # Top-level YAML keys we do not model, preserved verbatim on persist so
        # _to_document_locked() cannot delete them (see _to_document_locked).
        self._raw_extra_keys: Dict[str, Any] = {}

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
            self._step_sequences = self._normalize_sequences(raw.get("sequences"))

            # Anything we do not model is kept verbatim so a persist cannot
            # delete it. Without this, _to_document_locked()'s fixed key list
            # silently dropped every unrecognised top-level key.
            modelled = {"version", "settings", "params", "variables", "sequences"}
            self._raw_extra_keys = {
                key: copy.deepcopy(value)
                for key, value in raw.items()
                if key not in modelled
            } if isinstance(raw, dict) else {}

    def _normalize_sequences(self, raw_sequences: Any) -> Dict[str, List[str]]:
        """Validate the optional top-level ``sequences:`` block.

        Shape: ``{task_key: [step name, ...]}`` where task_key is a task
        function name (``prod_cycle``, ``home``, ``shipment``, ``calib``) or
        ``manual:<action_key>``. Malformed entries are warned about and
        skipped rather than raising — a bad sequence must not stop production.
        """
        if raw_sequences is None:
            return {}
        if not isinstance(raw_sequences, dict):
            self.logger.warning("'sequences' must be a mapping of task name to step list. Ignoring.")
            return {}

        result: Dict[str, List[str]] = {}
        for key, steps in raw_sequences.items():
            if not isinstance(steps, list):
                self.logger.warning(f"sequences.{key} must be a list of step names. Ignoring.")
                continue
            names = [str(s).strip()[: self._MAX_STEP_NAME_LEN] for s in steps if str(s).strip()]
            if names:
                result[str(key).strip()] = names
        return result

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
        """Build the YAML document to persist.

        Re-emits ``sequences`` and any unrecognised top-level keys captured at
        load. This used to be a fixed four-key dict, which meant the first
        persist with ``storage_scope: disk`` silently deleted everything else
        the operator had put in the file.
        """
        doc: Dict[str, Any] = {
            "version": 1,
            "settings": copy.deepcopy(self._settings),
            "params": copy.deepcopy(self._params),
            "variables": copy.deepcopy(self._variables),
        }
        if self._step_sequences:
            doc["sequences"] = copy.deepcopy(self._step_sequences)
        for key, value in self._raw_extra_keys.items():
            doc.setdefault(key, copy.deepcopy(value))
        return doc

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
                # Previously never cleared, so a single bad cycle left the
                # "Last Error State" card populated for the process lifetime.
                self._metadata["last_error"] = None
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

    # ------------------------------------------------------------------
    # Sequence step tracking
    # ------------------------------------------------------------------
    #
    # Workspace logic reports what the cell is doing:
    #
    #     with context.step("Pick part from feeder"):
    #         robot.MoveJoints(...)
    #         robot.WaitIdle()
    #
    # Steps nest (prod_cycle already calls into manual-action subroutines), so
    # open frames form a stack. The innermost frame is "what the cell is doing
    # right now"; stats are keyed by the full path so a parent and child never
    # collide.
    # ------------------------------------------------------------------

    def _rebuild_live_locked(self) -> None:
        """Rebuild the immutable live-step snapshot. Caller holds _step_lock."""
        self._step_rev += 1
        if not self._step_stack:
            self._step_live = None
            return

        frame = self._step_stack[-1]
        task = self._step_task or {}
        declared = task.get("declared") or []
        seq_total = len(declared) or None

        # Sequence position belongs to the TASK, not to the innermost frame.
        # Only depth-0 frames carry a seq_index, so reading it off a nested
        # frame yielded None while seq_total stayed 3 — the UI rendered
        # "Step null of 3". The cursor is advanced by depth-0 steps and is the
        # right answer for every frame beneath them.
        seq_index = task.get("cursor") if (declared and not task.get("off_sequence")) else None

        self._step_live = {
            "name": frame["name"],
            "path": list(frame["path"]),
            "depth": frame["depth"],
            "task": task.get("label"),
            "started_at": frame["started_at"],
            "index": (seq_index + 1) if seq_index is not None else None,
            "total": seq_total,
            "progress": ((seq_index + 1) / seq_total) if (seq_index is not None and seq_total) else None,
            "off_sequence": bool(task.get("off_sequence")),
            "cycle": self._metadata.get("cycle_count", 0) + 1,
            "rev": self._step_rev,
            "_started_monotonic": frame["_started_monotonic"],
        }

    def get_live_step(self) -> Dict[str, Any] | None:
        """Current step, or ``None`` when idle.

        Lock-free and allocation-light: safe to call at 10 Hz from Flask
        threads while a task thread is mid-cycle. Relies on ``_step_live``
        being rebound rather than mutated (see the invariant in __init__).
        """
        snapshot = self._step_live
        if snapshot is None:
            return None
        live = dict(snapshot)
        started = live.pop("_started_monotonic", None)
        # monotonic, not wall-clock: no ISO re-parsing per request, and immune
        # to clock adjustments during a long step.
        live["elapsed_s"] = max(0.0, time.monotonic() - started) if started is not None else None
        return live

    def _advance_sequence_cursor_locked(self, frame: Dict[str, Any]) -> None:
        """Match a depth-0 step against the declared sequence, if any."""
        task = self._step_task
        if not task or frame["depth"] != 0:
            return
        declared = task.get("declared") or []
        if not declared:
            return

        name = frame["name"]
        try:
            index = declared.index(name, task.get("cursor", 0))
        except ValueError:
            try:
                index = declared.index(name)  # allow looping back to an earlier step
            except ValueError:
                # Unknown step: flag it rather than inventing a position.
                task["off_sequence"] = True
                frame["seq_index"] = None
                return
        task["cursor"] = index
        task["off_sequence"] = False
        frame["seq_index"] = index

    def _close_frame_locked(self, frame: Dict[str, Any], status: str,
                            error: BaseException | None = None) -> None:
        """Record a finished frame into history and stats. Caller holds _step_lock."""
        duration_s = max(0.0, time.monotonic() - frame["_started_monotonic"])

        record = {
            "name": frame["name"],
            "path": frame["key"],
            "depth": frame["depth"],
            "started_at": frame["started_at"],
            "duration_s": duration_s,
            "status": status,
            "cycle": frame.get("cycle"),
            "run_id": frame.get("run_id"),
        }
        if error is not None:
            record["error"] = str(error)
        self._step_history.append(record)
        if len(self._step_history) > self._MAX_STEP_HISTORY:
            del self._step_history[: -self._MAX_STEP_HISTORY]

        key = frame["key"]
        stat = self._step_stats.get(key)
        if stat is None:
            if len(self._step_stats) >= self._MAX_STEP_STATS:
                # Almost always means step names embed data (e.g. a serial
                # number). Degrade to "stop aggregating new names" rather than
                # growing without bound.
                if not getattr(self, "_step_stats_capped_warned", False):
                    self._step_stats_capped_warned = True
                    self.logger.warning(
                        "Step stats cap (%d distinct steps) reached; new step names are no longer "
                        "aggregated. Step names should be stable identifiers, not per-part data.",
                        self._MAX_STEP_STATS,
                    )
                return
            stat = {
                "path": key, "name": frame["name"], "depth": frame["depth"],
                "count": 0, "total_s": 0.0, "last_s": None,
                "min_s": None, "max_s": None, "avg_s": None,
                "fail_count": 0, "abandon_count": 0,
            }
            self._step_stats[key] = stat

        if status == "ok":
            # Only successful runs feed the timing aggregates. An abort 48s into
            # a 1.9s step would otherwise wreck the averages the operator reads.
            stat["count"] += 1
            stat["total_s"] += duration_s
            stat["last_s"] = duration_s
            stat["min_s"] = duration_s if stat["min_s"] is None else min(stat["min_s"], duration_s)
            stat["max_s"] = duration_s if stat["max_s"] is None else max(stat["max_s"], duration_s)
            stat["avg_s"] = stat["total_s"] / stat["count"]
        elif status == "failed":
            stat["fail_count"] += 1
        else:
            stat["abandon_count"] += 1

        if status == "ok":
            self.logger.debug("Step end: %s in %.3f s", key, duration_s)
        else:
            self.logger.warning("Step %s: %s after %.3f s%s", status, key, duration_s,
                                f" ({error})" if error else "")

    def begin_step(self, name: str, managed: bool = True) -> Tuple[int, int] | None:
        """Open a step. Returns an opaque token for :meth:`end_step`, or None if rejected."""
        name = str(name).strip()[: self._MAX_STEP_NAME_LEN] or "(unnamed step)"
        with self._step_lock:
            # An unmanaged (set_step) frame is replaced by the next set_step at
            # the same level, so the operator sees one current step, not a pile.
            if not managed and self._step_stack and not self._step_stack[-1].get("managed"):
                self._close_frame_locked(self._step_stack.pop(), "ok")

            if len(self._step_stack) >= self._MAX_STEP_DEPTH:
                self.logger.warning(
                    "Step nesting limit (%d) reached; '%s' is not tracked.",
                    self._MAX_STEP_DEPTH, name,
                )
                return None

            self._step_frame_seq += 1
            parent_path = self._step_stack[-1]["path"] if self._step_stack else []
            path = list(parent_path) + [name]
            frame = {
                "frame_id": self._step_frame_seq,
                "epoch": self._step_epoch,
                "depth": len(self._step_stack),
                "name": name,
                "path": path,
                "key": " / ".join(path),
                "managed": managed,
                "started_at": self._utc_now(),
                "_started_monotonic": time.monotonic(),
                "cycle": self._metadata.get("cycle_count", 0) + 1,
                "run_id": (self._step_task or {}).get("run_id"),
                "seq_index": None,
            }
            self._step_stack.append(frame)

            if frame["depth"] == 0 and name not in self._step_observed:
                self._step_observed.append(name)
            self._advance_sequence_cursor_locked(frame)
            self._rebuild_live_locked()
            token = (frame["frame_id"], frame["epoch"])

        self.logger.debug("Step begin: %s", frame["key"])
        return token

    def end_step(self, token: Tuple[int, int] | None, failed: bool = False,
                 error: BaseException | None = None) -> None:
        """Close a step opened by :meth:`begin_step`. Safe to call twice or out of order."""
        if token is None:
            return
        frame_id, epoch = token
        with self._step_lock:
            if epoch != self._step_epoch:
                # abandon_open_steps() already closed this frame (abort/fault).
                # Must be a no-op: raising here would throw a second exception
                # out of a `with` block that is already unwinding.
                self.logger.debug("Ignoring stale step close (frame=%s epoch=%s).", frame_id, epoch)
                return

            index = next((i for i, f in enumerate(self._step_stack) if f["frame_id"] == frame_id), None)
            if index is None:
                return

            # Self-heal mis-nesting: close anything still open above this frame.
            while len(self._step_stack) > index + 1:
                self._close_frame_locked(self._step_stack.pop(), "abandoned")
            self._close_frame_locked(self._step_stack.pop(),
                                     "failed" if failed else "ok", error=error)
            self._rebuild_live_locked()

    @contextlib.contextmanager
    def step(self, name: str):
        """Report the step the cell is executing, timing it automatically.

        ``BaseException`` rather than ``Exception``: an abort unblocks a device
        SDK call and the resulting exception must still close the frame, and
        KeyboardInterrupt during a run should not leave a step open forever.
        """
        token = self.begin_step(name, managed=True)
        try:
            yield
        except BaseException as exc:
            self.end_step(token, failed=True, error=exc)
            raise
        else:
            self.end_step(token)

    def set_step(self, name: str) -> None:
        """Declare the current step without a ``with`` block.

        Closed by the next :meth:`set_step`, by :meth:`clear_step`, or when the
        enclosing task ends. Prefer :meth:`step` — it also times failures.
        """
        self.begin_step(name, managed=False)

    def clear_step(self) -> None:
        """Close the innermost unmanaged step, if any."""
        with self._step_lock:
            if self._step_stack and not self._step_stack[-1].get("managed"):
                self._close_frame_locked(self._step_stack.pop(), "ok")
                self._rebuild_live_locked()

    def abandon_open_steps(self, reason: str = "abandoned") -> None:
        """Close every open step, from any thread.

        Called on abort/fault paths so the monitor thread can clear a step the
        task thread opened — otherwise a faulted cell keeps showing
        "Pick part — 48 s and counting" while the task thread is stuck in a
        non-interruptible SDK call. Bumping the epoch makes every outstanding
        token stale so the task thread's later end_step() is a quiet no-op.
        """
        with self._step_lock:
            if not self._step_stack:
                return
            open_keys = [f["key"] for f in self._step_stack]
            while self._step_stack:
                self._close_frame_locked(self._step_stack.pop(), reason)
            self._step_epoch += 1
            self._rebuild_live_locked()
        self.logger.warning("Abandoned %d open step(s) (%s): %s",
                            len(open_keys), reason, ", ".join(reversed(open_keys)))

    def declare_sequence(self, steps: List[str], task_key: str | None = None) -> None:
        """Declare the expected step order for the active task at runtime.

        Overrides any ``sequences:`` entry in the YAML. Optional — without it
        steps are still recorded and timed, just without "step 3 of 7".
        """
        names = [str(s).strip()[: self._MAX_STEP_NAME_LEN] for s in (steps or []) if str(s).strip()]
        with self._step_lock:
            key = task_key or (self._step_task or {}).get("sequence_key")
            if key:
                self._step_sequences_runtime[key] = names
            if self._step_task is not None:
                self._step_task["declared"] = names
                self._step_task["cursor"] = 0
                self._step_task["off_sequence"] = False
            self._rebuild_live_locked()

    def begin_task(self, label: str, sequence_key: str | None = None) -> None:
        """Start step tracking for a task run, clearing the previous run's stats."""
        with self._step_lock:
            self._step_stack.clear()
            self._step_stats.clear()
            self._step_observed.clear()
            self._step_stats_capped_warned = False
            declared = (self._step_sequences_runtime.get(sequence_key)
                        or self._step_sequences.get(sequence_key)
                        or [])
            self._step_run_seq += 1
            self._step_task = {
                "run_id": self._step_run_seq,
                "label": label,
                "sequence_key": sequence_key,
                "declared": list(declared),
                "cursor": 0,
                "off_sequence": False,
                "source": "runtime" if sequence_key in self._step_sequences_runtime
                          else ("yaml" if declared else None),
            }
            self._step_epoch += 1
            self._rebuild_live_locked()

    def end_task(self) -> None:
        """Stop step tracking, keeping stats so they stay readable after STOP."""
        with self._step_lock:
            while self._step_stack:
                self._close_frame_locked(self._step_stack.pop(), "abandoned")
            self._step_epoch += 1
            self._step_task = None
            self._rebuild_live_locked()

    def reset_cycle_steps(self) -> None:
        """Reset the sequence cursor so the progress bar refills each cycle."""
        with self._step_lock:
            if self._step_task is not None:
                self._step_task["cursor"] = 0
                self._step_task["off_sequence"] = False
            self._rebuild_live_locked()

    def get_cycle_count(self) -> int:
        """Authoritative completed-cycle count (``_metadata["cycle_count"]``)."""
        with self._lock:
            return int(self._metadata.get("cycle_count", 0))

    def steps_snapshot(self) -> Dict[str, Any]:
        """Full step state for ``/api/prod/context``."""
        with self._step_lock:
            task = dict(self._step_task) if self._step_task else {}
            declared = list(task.get("declared") or [])
            cursor = int(task.get("cursor", 0))
            stats = [dict(s) for s in self._step_stats.values()]
            history = [dict(h) for h in self._step_history[-50:]]
            observed = list(self._step_observed)
            capped = len(self._step_stats) >= self._MAX_STEP_STATS

        # Precompute each depth-0 step's share of cycle time so the UI needs no math.
        depth0_total = sum(s["total_s"] for s in stats if s["depth"] == 0) or 0.0
        for stat in stats:
            stat["share"] = (stat["total_s"] / depth0_total) if (depth0_total and stat["depth"] == 0) else None
        stats.sort(key=lambda s: (s["depth"], -(s["total_s"] or 0.0)))

        sequence = None
        if declared:
            sequence = {
                "key": task.get("sequence_key"),
                "source": task.get("source"),
                "declared": declared,
                "total": len(declared),
                "current_index": cursor,
                "done": declared[:cursor],
                "upcoming": declared[cursor + 1:],
                "off_sequence": bool(task.get("off_sequence")),
            }

        return {
            "live": self.get_live_step(),
            "task": task.get("label"),
            "sequence": sequence,
            "observed": observed,
            "stats": stats,
            "history": history,
            "stats_truncated": capped,
        }

    def log_cycle_step_summary(self, cycle_number: int) -> None:
        """One INFO line per cycle breaking down where the time went.

        Filtered by run as well as cycle: _step_history deliberately survives
        across runs, and every new PROD run resets cycle_count to 0, so cycle
        numbers repeat. Without the run_id guard this reported several runs'
        steps under one cycle heading.
        """
        with self._step_lock:
            run_id = (self._step_task or {}).get("run_id")
            parts = [h for h in self._step_history
                     if h.get("cycle") == cycle_number
                     and h.get("run_id") == run_id
                     and h["depth"] == 0 and h["status"] == "ok"]
        if not parts:
            return
        total = sum(p["duration_s"] for p in parts)
        detail = " | ".join(f"{p['name']} {p['duration_s']:.2f}s" for p in parts)
        self.logger.info("Cycle #%d steps: %s (total %.2fs)", cycle_number, detail, total)

    def snapshot(self, controller_state: str) -> Dict[str, Any]:
        # Collected before taking _lock, so the two locks are never nested.
        # The step paths deliberately read _metadata["cycle_count"] without
        # _lock (a plain int read) precisely to avoid a _step_lock -> _lock
        # ordering; keeping this call outside _lock preserves that property
        # from the other direction.
        steps = self.steps_snapshot()
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
                # New top-level key. metadata/metrics stay byte-identical so
                # /api/prod/runs and renderProdMetrics are untouched.
                "steps": steps,
            }
