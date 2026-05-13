import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple


class BackupRestoreService:
    """Backup/restore helper for Mecademic robots already managed by ApplicationController."""

    ARCHIVE_EXTENSION = ".zip"

    def __init__(self, backup_dir: str, logger):
        self.backup_dir = Path(backup_dir)
        self.logger = logger
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def list_mecademic_robots(self, application) -> List[Dict[str, Any]]:
        configured = application.config.get("devices", {}) if hasattr(application, "config") else {}
        robots: List[Dict[str, Any]] = []

        for device_id, device in getattr(application, "devices", {}).items():
            cfg = configured.get(device_id, {})
            if str(cfg.get("type", "")).lower() != "mecademic":
                continue

            info = {}
            try:
                info = device.info or {}
            except Exception:
                info = {}

            robots.append(
                {
                    "device_id": device_id,
                    "ip_address": cfg.get("ip_address") or info.get("ip_address"),
                    "connected": bool(getattr(device, "connected", False)),
                    "ready": bool(getattr(device, "ready", False)),
                    "faulted": bool(getattr(device, "faulted", False)),
                    "model": info.get("model"),
                    "serial_number": info.get("serial_number"),
                    "firmware_version": info.get("firmware_version"),
                }
            )

        robots.sort(key=lambda item: item["device_id"])
        return robots

    def backup_all(self, application, stop_on_error: bool = False) -> Dict[str, Any]:
        robots = [r for r in self.list_mecademic_robots(application) if r["connected"]]
        self.logger.info(f"[backup] Starting backup-all for {len(robots)} connected robot(s)")
        results: List[Dict[str, Any]] = []

        for robot in robots:
            device = application.devices[robot["device_id"]]
            self.logger.info(f"[backup] Backing up robot '{robot['device_id']}'")
            result = self.backup_single_robot(device_id=robot["device_id"], device=device)
            results.append(result)
            if result["success"]:
                self.logger.info(f"[backup] '{robot['device_id']}' -> OK: {result.get('archive_filename')}")
            else:
                self.logger.warning(f"[backup] '{robot['device_id']}' -> completed with {len(result.get('errors', []))} error(s)")
            if stop_on_error and not result["success"]:
                self.logger.warning(f"[backup] Stopping early due to error on '{robot['device_id']}'")
                break

        return self._summarize_results(mode="all", target_count=len(robots), results=results)

    def backup_one(self, application, robot_id: str) -> Dict[str, Any]:
        self.logger.info(f"[backup] Starting single backup for robot '{robot_id}'")
        device = self._get_connected_robot_device(application, robot_id)
        result = self.backup_single_robot(device_id=robot_id, device=device)
        return self._summarize_results(mode="single", target_count=1, results=[result])

    def backup_single_robot(self, device_id: str, device) -> Dict[str, Any]:
        api = self._get_robot_api(device)
        now_utc = datetime.now(timezone.utc)
        errors: List[Dict[str, str]] = []

        self.logger.info(f"[backup:{device_id}] Collecting robot info")
        robot_info = self._safe_collect_robot_info(api)
        archive_name = self._build_archive_name(device_id=device_id, robot_info=robot_info, timestamp=now_utc)
        archive_path = self.backup_dir / archive_name
        self.logger.info(f"[backup:{device_id}] Archive will be saved to: {archive_path}")

        self.logger.info(f"[backup:{device_id}] Collecting variables")
        variables_payload = self._collect_variables(api, errors)
        self.logger.info(f"[backup:{device_id}] Collected {len(variables_payload.get('variables', []))} variable(s)")

        self.logger.info(f"[backup:{device_id}] Collecting files")
        files_payload = self._collect_files(api, errors)
        self.logger.info(f"[backup:{device_id}] Collected {len(files_payload.get('files', []))} file(s)")

        self.logger.info(f"[backup:{device_id}] Collecting robot config")
        config_payload = self._collect_robot_config(api, errors)

        manifest = {
            "schema_version": 1,
            "created_at": now_utc.isoformat(),
            "device_id": device_id,
            "robot_info": robot_info,
            "errors": errors,
            "counts": {
                "variables": len(variables_payload.get("variables", [])),
                "files": len(files_payload.get("files", [])),
            },
        }

        self.logger.info(f"[backup:{device_id}] Writing archive")
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
            archive.writestr("variables.json", json.dumps(variables_payload, indent=2))
            archive.writestr("robot_config.json", json.dumps(config_payload, indent=2))
            for entry in files_payload.get("files", []):
                archive_name_in_zip = self._normalize_archive_file_name(entry.get("name", ""))
                if not archive_name_in_zip:
                    continue
                content = entry.get("content", b"")
                # Ensure content is bytes for zipfile.writestr
                if isinstance(content, str):
                    content = content.encode('utf-8')
                elif not isinstance(content, bytes):
                    content = b""
                archive.writestr(archive_name_in_zip, content)
                self.logger.debug(f"[backup:{device_id}] Wrote file: {archive_name_in_zip} ({len(content)} bytes)")

        if errors:
            self.logger.warning(f"[backup:{device_id}] Completed with {len(errors)} error(s):")
            for err in errors:
                self.logger.warning(f"[backup:{device_id}]   [{err.get('kind')}] {err.get('name')}: {err.get('error')}")
        else:
            self.logger.info(f"[backup:{device_id}] Completed successfully -> {archive_name}")

        return {
            "device_id": device_id,
            "success": len(errors) == 0,
            "archive_filename": archive_name,
            "archive_path": str(archive_path),
            "counts": manifest["counts"],
            "errors": errors,
            "message": "Backup completed." if len(errors) == 0 else "Backup completed with item errors.",
        }

    def restore_single_from_archive(self, application, robot_id: str, archive_path: str, dry_run: bool = False, stop_on_error: bool = False) -> Dict[str, Any]:
        self.logger.info(f"[restore:{robot_id}] Starting restore from '{archive_path}' (dry_run={dry_run})")
        device = self._get_connected_robot_device(application, robot_id)
        api = self._get_robot_api(device)
        payload = self._read_archive_payload(archive_path)

        variables_payload = payload.get("variables", {})
        variables = self._load_variables_payload(variables_payload)
        files = payload.get("files", [])
        config = payload.get("robot_config", {})
        self.logger.info(f"[restore:{robot_id}] Archive contains {len(variables)} variable(s), {len(files)} file(s)")

        if dry_run:
            self.logger.info(f"[restore:{robot_id}] Dry-run complete — no changes written")
            return {
                "device_id": robot_id,
                "success": True,
                "dry_run": True,
                "counts": {
                    "variables": len(variables),
                    "files": len(files),
                },
                "errors": [],
                "message": "Dry-run complete. No changes were written.",
            }

        errors: List[Dict[str, str]] = []
        restored_variables = 0
        restored_files = 0

        self.logger.info(f"[restore:{robot_id}] Restoring {len(variables)} variable(s)")
        for var_name, var_payload in variables.items():
            try:
                value = self._decode_variable_value(var_name, var_payload)
                cyclic_id = var_payload.get("cyclic_id")
                if cyclic_id in (0, "0"):
                    cyclic_id = None

                kwargs = {"override": True}
                # Note: 'volatile' parameter is not supported by Robot.CreateVariable() API
                if cyclic_id is not None:
                    kwargs["cyclic_id"] = cyclic_id
                if "description" in var_payload and var_payload.get("description"):
                    kwargs["description"] = var_payload.get("description")

                api.CreateVariable(var_name, value, **kwargs)
                restored_variables += 1
                self.logger.debug(f"[restore:{robot_id}] Variable restored: {var_name}")
            except Exception as exc:
                self.logger.warning(f"[restore:{robot_id}] Variable failed: {var_name} — {exc}")
                errors.append({"kind": "variable", "name": str(var_name), "error": str(exc)})
                if stop_on_error:
                    break

        if not stop_on_error or len(errors) == 0:
            self.logger.info(f"[restore:{robot_id}] Restoring {len(files)} file(s)")
            for item in files:
                try:
                    safe_name = self._denormalize_archive_file_name(item["archive_name"])
                    raw_bytes = item.get("content", b"")

                    # Decode bytes to string
                    try:
                        content = raw_bytes.decode("utf-8")
                    except UnicodeDecodeError:
                        content = raw_bytes.decode("latin-1")

                    # Handle legacy archives where LoadFile() object was JSON-serialized.
                    # Detect: content is a JSON object with a "content" key.
                    try:
                        parsed = json.loads(content)
                        if isinstance(parsed, dict) and "content" in parsed:
                            self.logger.debug(f"[restore:{robot_id}] Unwrapping legacy JSON payload for '{safe_name}'")
                            content = parsed["content"]
                    except (json.JSONDecodeError, ValueError):
                        pass  # Content is raw program text, use as-is

                    # Delete existing file first before saving (robot doesn't allow overwrite in some versions)
                    try:
                        api.DeleteFile(safe_name)
                        self.logger.debug(f"[restore:{robot_id}] Deleted existing file: {safe_name}")
                    except Exception:
                        pass  # File might not exist, that's OK

                    api.SaveFile(safe_name, content)
                    restored_files += 1
                    self.logger.debug(f"[restore:{robot_id}] File restored: {safe_name}")
                except Exception as exc:
                    self.logger.warning(f"[restore:{robot_id}] File failed: {item.get('archive_name')} — {exc}")
                    errors.append({"kind": "file", "name": str(item.get("archive_name", "unknown")), "error": str(exc)})
                    if stop_on_error:
                        break

        if (not stop_on_error or len(errors) == 0) and isinstance(config, dict):
            self.logger.info(f"[restore:{robot_id}] Restoring robot config")
            config_errors = self._restore_robot_config(api, config, stop_on_error=stop_on_error)
            for err in config_errors:
                self.logger.warning(f"[restore:{robot_id}] Config failed: {err.get('name')} — {err.get('error')}")
            errors.extend(config_errors)

        if errors:
            self.logger.warning(f"[restore:{robot_id}] Completed with {len(errors)} error(s) | vars={restored_variables} files={restored_files}")
        else:
            self.logger.info(f"[restore:{robot_id}] Completed successfully | vars={restored_variables} files={restored_files}")

        return {
            "device_id": robot_id,
            "success": len(errors) == 0,
            "dry_run": False,
            "counts": {
                "variables": restored_variables,
                "files": restored_files,
            },
            "errors": errors,
            "message": "Restore completed." if len(errors) == 0 else "Restore completed with item errors.",
        }

    def _summarize_results(self, mode: str, target_count: int, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        success_count = sum(1 for item in results if item.get("success"))
        failure_count = target_count - success_count
        return {
            "mode": mode,
            "target_count": target_count,
            "success_count": success_count,
            "failure_count": max(failure_count, 0),
            "results": results,
        }

    def _get_connected_robot_device(self, application, robot_id: str):
        robots = {item["device_id"]: item for item in self.list_mecademic_robots(application)}
        if robot_id not in robots:
            raise ValueError(f"Unknown Mecademic robot '{robot_id}'.")
        if not robots[robot_id].get("connected"):
            raise ValueError(f"Robot '{robot_id}' is not connected.")
        return application.devices[robot_id]

    def _get_robot_api(self, device):
        api = getattr(device, "api", None)
        if api is None:
            raise ValueError("Device has no Mecademic API handle.")
        return api

    def _safe_collect_robot_info(self, api) -> Dict[str, Any]:
        try:
            info = api.GetRobotInfo()
            return {
                "model": getattr(info, "model", None),
                "serial_number": getattr(info, "serial", None),
                "firmware_version": getattr(getattr(info, "version", None), "get_str", lambda: None)(),
            }
        except Exception:
            return {"model": None, "serial_number": None, "firmware_version": None}

    def _build_archive_name(self, device_id: str, robot_info: Dict[str, Any], timestamp: datetime) -> str:
        # Format: device_name_YYYYMMDD_HH.zip
        stamp = timestamp.strftime("%Y%m%d_%H")
        return f"{self._sanitize_slug(device_id)}_{stamp}{self.ARCHIVE_EXTENSION}"

    def _sanitize_slug(self, value: str) -> str:
        cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value)
        return cleaned.strip("_") or "item"

    def _collect_variables(self, api, errors: List[Dict[str, str]]) -> Dict[str, Any]:
        payload = {"schema_version": 1, "variables": []}
        try:
            names = api.ListVariables()
            self.logger.debug(f"[backup] ListVariables returned {len(names) if names else 0} variable(s)")
        except Exception as exc:
            errors.append({"kind": "variable", "name": "ListVariables", "error": str(exc)})
            self.logger.error(f"[backup] ListVariables failed: {exc}")
            return payload

        for name in names or []:
            try:
                data = api.GetVariable(name)
                payload["variables"].append(
                    {
                        "name": name,
                        "value": getattr(data, "value", None),
                        "volatile": getattr(data, "volatile", False),
                        "cyclic_id": getattr(data, "cyclic_id", None),
                        "description": getattr(data, "description", None),
                    }
                )
                self.logger.debug(f"[backup] Variable collected: {name}")
            except Exception as exc:
                errors.append({"kind": "variable", "name": str(name), "error": str(exc)})
                self.logger.warning(f"[backup] Failed to collect variable '{name}': {exc}")
        return payload

    def _collect_files(self, api, errors: List[Dict[str, str]]) -> Dict[str, Any]:
        payload = {"schema_version": 1, "files": []}
        try:
            # Use as_dict=True for direct dict and timeout to trigger a live fetch from the robot
            file_dict = api.ListFiles(timeout=10.0, as_dict=True)
            if isinstance(file_dict, dict):
                names = sorted(file_dict.keys())
            else:
                # Fallback: ListFiles returned a RobotFileList object
                file_list = file_dict
                if hasattr(file_list, "files") and isinstance(file_list.files, dict):
                    names = sorted(file_list.files.keys())
                elif isinstance(file_list, (list, tuple)):
                    names = sorted(file_list)
                else:
                    raise TypeError(f"Unexpected ListFiles() return type: {type(file_list).__name__}")
            self.logger.debug(f"[backup] ListFiles returned {len(names)} file(s): {names}")
        except Exception as exc:
            errors.append({"kind": "file", "name": "ListFiles", "error": str(exc)})
            self.logger.error(f"[backup] ListFiles failed: {exc}")
            return payload

        for name in names:
            try:
                loaded_file = api.LoadFile(name)
                # LoadFile() returns a file object; extract .content attribute
                if hasattr(loaded_file, "content"):
                    raw = loaded_file.content
                else:
                    raw = loaded_file

                # Ensure content is properly serialized: bytes for zipfile.writestr
                if isinstance(raw, bytes):
                    serialized_content = raw
                elif isinstance(raw, str):
                    serialized_content = raw.encode('utf-8')
                else:
                    raise TypeError(f"Unsupported file content type: {type(raw).__name__}")
                payload["files"].append({"name": name, "content": serialized_content})
                self.logger.debug(f"[backup] Loaded file '{name}' ({len(serialized_content)} bytes)")
            except Exception as exc:
                errors.append({"kind": "file", "name": str(name), "error": str(exc)})
                self.logger.warning(f"[backup] Failed to load file '{name}': {exc}")
        return payload

    def _collect_robot_config(self, api, errors: List[Dict[str, str]]) -> Dict[str, Any]:
        config: Dict[str, Any] = {"schema_version": 1}
        getters: List[Tuple[str, str]] = [
            ("robot_info", "GetRobotInfo"),
            ("network_config", "GetNetworkCfg"),
            ("network_options", "GetNetworkOptions"),
            ("joint_limits_cfg", "GetJointLimitsCfg"),
            ("work_zone_cfg", "GetWorkZoneCfg"),
            ("collision_cfg", "GetCollisionCfg"),
        ]

        for key, getter_name in getters:
            getter = getattr(api, getter_name, None)
            if getter is None:
                self.logger.debug(f"[backup] Config getter '{getter_name}' not available, skipping")
                continue
            try:
                raw = getter()
                config[key] = self._to_jsonable(raw)
                self.logger.debug(f"[backup] Config collected: {getter_name}")
            except Exception as exc:
                errors.append({"kind": "config", "name": getter_name, "error": str(exc)})
                self.logger.warning(f"[backup] Config getter '{getter_name}' failed: {exc}")

        return config

    def _restore_robot_config(self, api, config_payload: Dict[str, Any], stop_on_error: bool = False) -> List[Dict[str, str]]:
        errors: List[Dict[str, str]] = []

        def record_error(name: str, exc: Exception):
            errors.append({"kind": "config", "name": name, "error": str(exc)})
            self.logger.warning(f"[restore:config] {name} failed: {exc}")

        # Deactivate the robot before applying config (required for these commands)
        was_activated = False
        try:
            self.logger.info("[restore:config] Deactivating robot for config restore")
            api.DeactivateRobot()
            api.WaitDeactivated(timeout=15.0)
            was_activated = True
            self.logger.info("[restore:config] Robot deactivated")
        except Exception as exc:
            self.logger.warning(f"[restore:config] Deactivate failed (continuing anyway): {exc}")

        try:
            # network_options: SetNetworkOptions(n: int)
            network_options = config_payload.get("network_options")
            if isinstance(network_options, dict) and "keep_alive_timeout" in network_options:
                try:
                    api.SetNetworkOptions(int(network_options["keep_alive_timeout"]))
                    self.logger.debug("[restore:config] SetNetworkOptions OK")
                except Exception as exc:
                    record_error("SetNetworkOptions", exc)
                    if stop_on_error:
                        return errors

            # joint_limits_cfg: SetJointLimitsCfg(e: bool)
            joint_limits_cfg = config_payload.get("joint_limits_cfg")
            if isinstance(joint_limits_cfg, dict) and "enabled" in joint_limits_cfg:
                try:
                    api.SetJointLimitsCfg(bool(joint_limits_cfg["enabled"]))
                    self.logger.debug("[restore:config] SetJointLimitsCfg OK")
                except Exception as exc:
                    record_error("SetJointLimitsCfg", exc)
                    if stop_on_error:
                        return errors

            # work_zone_cfg: SetWorkZoneCfg(severity, mode)
            work_zone_cfg = config_payload.get("work_zone_cfg")
            if isinstance(work_zone_cfg, dict):
                try:
                    import mecademicpy.robot as mdr
                    kwargs: Dict[str, Any] = {}
                    if "severity" in work_zone_cfg:
                        kwargs["severity"] = mdr.MxEventSeverity(int(work_zone_cfg["severity"]))
                    if "mode" in work_zone_cfg:
                        kwargs["mode"] = mdr.MxWorkZoneMode(int(work_zone_cfg["mode"]))
                    if kwargs:
                        api.SetWorkZoneCfg(**kwargs)
                        self.logger.debug("[restore:config] SetWorkZoneCfg OK")
                except Exception as exc:
                    record_error("SetWorkZoneCfg", exc)
                    if stop_on_error:
                        return errors

            # collision_cfg: SetCollisionCfg(severity)
            collision_cfg = config_payload.get("collision_cfg")
            if isinstance(collision_cfg, dict) and "severity" in collision_cfg:
                try:
                    import mecademicpy.robot as mdr
                    api.SetCollisionCfg(mdr.MxEventSeverity(int(collision_cfg["severity"])))
                    self.logger.debug("[restore:config] SetCollisionCfg OK")
                except Exception as exc:
                    record_error("SetCollisionCfg", exc)
                    if stop_on_error:
                        return errors
        finally:
            # Re-activate the robot if we deactivated it
            if was_activated:
                try:
                    self.logger.info("[restore:config] Re-activating robot after config restore")
                    api.ActivateRobot()
                    api.WaitActivated(timeout=30.0)
                    self.logger.info("[restore:config] Robot re-activated")
                except Exception as exc:
                    self.logger.warning(f"[restore:config] Re-activation failed: {exc}")
                    errors.append({"kind": "config", "name": "ReactivateRobot", "error": str(exc)})

        return errors

    def _normalize_archive_file_name(self, raw_name: str) -> str:
        cleaned = str(raw_name).replace("\\", "/").lstrip("/")
        cleaned = cleaned.strip()
        if not cleaned:
            return ""
        return f"files/{cleaned}"

    def _denormalize_archive_file_name(self, archive_name: str) -> str:
        normalized = str(archive_name).replace("\\", "/")
        if normalized.startswith("/") or ".." in normalized.split("/"):
            raise ValueError("Unsafe archive path.")
        if not normalized.startswith("files/"):
            raise ValueError("Unexpected archive file prefix.")
        return normalized[len("files/") :]

    def _read_archive_payload(self, archive_path: str) -> Dict[str, Any]:
        with zipfile.ZipFile(archive_path, "r") as archive:
            variables = self._read_json_member(archive, "variables.json", default={"variables": []})
            robot_config = self._read_json_member(archive, "robot_config.json", default={})
            manifest = self._read_json_member(archive, "manifest.json", default={})

            files: List[Dict[str, Any]] = []
            for name in archive.namelist():
                if not name.startswith("files/"):
                    continue
                safe_name = self._denormalize_archive_file_name(name)
                content = archive.read(name)
                # Always store as bytes; restore endpoint will handle encoding
                files.append({"archive_name": name, "safe_name": safe_name, "content": content})

        return {
            "manifest": manifest,
            "variables": variables,
            "robot_config": robot_config,
            "files": files,
        }

    def _read_json_member(self, archive: zipfile.ZipFile, member_name: str, default: Any) -> Any:
        try:
            with archive.open(member_name) as stream:
                return json.loads(stream.read().decode("utf-8"))
        except KeyError:
            return default

    def _to_jsonable(self, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(k): self._to_jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._to_jsonable(item) for item in value]
        if hasattr(value, "__dict__"):
            return self._to_jsonable(vars(value))
        return str(value)

    def _load_variables_payload(self, variables_payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """Load variables from archive with backward compatibility for multiple formats.
        
        Supports:
        - New format: {"variables": {<var_dict>}, "schema_version": ...}
        - Legacy format: {<var_dict>} (variables dict directly)
        - Very old format: [<var_list>] (list of variable objects with 'name' field)
        """
        if isinstance(variables_payload, dict) and "variables" in variables_payload:
            variables = variables_payload["variables"]
        else:
            # Legacy compatibility: file content is directly the variables dictionary
            variables = variables_payload

        # Backward compatibility: older backups store variables as a list of objects with 'name' field
        if isinstance(variables, list):
            converted: Dict[str, Dict[str, Any]] = {}
            for idx, item in enumerate(variables):
                if not isinstance(item, dict):
                    raise ValueError(f"Invalid variables payload: expected object at index {idx}")
                name = item.get("name")
                if not isinstance(name, str) or not name:
                    raise ValueError(f"Invalid variables payload: missing variable name at index {idx}")
                converted[name] = {
                    "value": item.get("value"),
                    "volatile": bool(item.get("volatile", False)),
                    "cyclic_id": item.get("cyclic_id"),
                    "description": item.get("description", ""),
                }
            variables = converted

        if not isinstance(variables, dict):
            raise ValueError("Invalid variables payload: expected dictionary or list")

        return variables

    def _decode_variable_value(self, var_name: str, var_payload: Dict[str, Any]) -> Any:
        """Decode variable value with support for multiple encoding formats.
        
        Supports:
        - New format: value_encoding field (json, repr, etc.)
        - Legacy format: value may be JSON string or already decoded value
        """
        if "value_encoding" in var_payload:
            encoding = var_payload.get("value_encoding")
            if encoding == "json":
                return var_payload.get("value")
            if encoding == "repr":
                # repr fallback cannot be safely deserialized, keep textual value
                return var_payload.get("value")
            raise ValueError(f"Unsupported value_encoding '{encoding}' for variable '{var_name}'")

        # Legacy compatibility (existing variable_list.json style):
        # value may be a JSON string payload or already decoded value
        raw_value = var_payload.get("value")
        if isinstance(raw_value, str):
            try:
                return json.loads(raw_value)
            except json.JSONDecodeError:
                return raw_value
        return raw_value
