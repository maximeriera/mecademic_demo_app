from __future__ import annotations

from importlib import import_module
from typing import Any, Callable, Dict, List


MANUAL_ACTION_MODULE_CANDIDATES = ("manual_actions", "app_logic.manual_actions")


def load_manual_actions_registry(
    module_candidates: tuple[str, ...] = MANUAL_ACTION_MODULE_CANDIDATES,
    logger=None,
) -> Dict[str, Callable[..., Any]]:
    """Load manual action callables from workspace modules.

    Supported module conventions:
    - ``get_manual_actions() -> dict[str, callable]``
    - ``MANUAL_ACTIONS`` dict constant
    """
    last_error = None
    for index, module_name in enumerate(module_candidates):
        try:
            if logger:
                logger.debug(f"ManualActions: trying module '{module_name}'")
            module = import_module(module_name)
            if hasattr(module, "get_manual_actions"):
                registry = module.get_manual_actions()
            else:
                registry = getattr(module, "MANUAL_ACTIONS")

            if not isinstance(registry, dict):
                raise ValueError(
                    f"Manual action registry in '{module_name}' must be a dict, got {type(registry).__name__}."
                )

            normalized: Dict[str, Callable[..., Any]] = {}
            for key, fn in registry.items():
                key_str = str(key).strip()
                if not key_str:
                    raise ValueError("Manual action key cannot be empty.")
                if not callable(fn):
                    raise ValueError(f"Manual action '{key_str}' is not callable.")
                normalized[key_str] = fn
            if logger:
                logger.info(
                    "ManualActions: loaded %d action(s) from '%s': %s",
                    len(normalized),
                    module_name,
                    sorted(normalized.keys()),
                )
            return normalized
        except Exception as error:
            if logger:
                remaining = len(module_candidates) - (index + 1)
                if remaining > 0:
                    logger.debug(
                        "ManualActions: module '%s' unavailable (%s). Trying next candidate.",
                        module_name,
                        error,
                    )
                else:
                    logger.debug(
                        "ManualActions: final candidate '%s' failed (%s).",
                        module_name,
                        error,
                    )
            last_error = error

    raise ImportError(
        "Unable to import manual action registry from any workspace module: "
        f"{module_candidates}"
    ) from last_error


def normalize_manual_actions_config(raw_actions: Any) -> List[Dict[str, str]]:
    """Validate and normalize manual action metadata from config."""
    if raw_actions is None:
        return []
    if not isinstance(raw_actions, list):
        raise ValueError("'manual_actions' must be a list of action definitions.")

    normalized: List[Dict[str, str]] = []
    seen_keys: set[str] = set()

    for index, item in enumerate(raw_actions):
        if not isinstance(item, dict):
            raise ValueError(f"manual_actions[{index}] must be a mapping.")

        key = str(item.get("key", "")).strip()
        if not key:
            raise ValueError(f"manual_actions[{index}] is missing required field 'key'.")
        if key in seen_keys:
            raise ValueError(f"Duplicate manual action key '{key}' in configuration.")

        label = str(item.get("label", key)).strip() or key
        description = str(item.get("description", "")).strip()
        confirm_title = str(item.get("confirm_title", f"Confirm {label}")).strip() or f"Confirm {label}"
        confirm_message = str(
            item.get(
                "confirm_message",
                f"Run manual action '{label}'? Ensure the cell is clear before proceeding.",
            )
        ).strip() or f"Run manual action '{label}'? Ensure the cell is clear before proceeding."

        normalized.append(
            {
                "key": key,
                "label": label,
                "description": description,
                "confirm_title": confirm_title,
                "confirm_message": confirm_message,
            }
        )
        seen_keys.add(key)

    return normalized
