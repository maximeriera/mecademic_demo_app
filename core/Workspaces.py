"""
core/Workspaces.py
------------------
Finding the workspaces the UI can switch between, and forgetting the
modules of the one being left.

A workspace is a folder holding ``config.yaml`` either directly (flat layout,
as made by ``create_project.bat`` from the template) or in ``app_logic/``.
The picker lists the direct subfolders of one root, by default the parent of
the startup workspace — the superproject folder in the usual layout.

Switching is done in-process (the Flask layer builds a new controller), so
the old workspace's modules must leave ``sys.modules`` or ``import prod``
would keep returning the old code. They are purged **by name**, never by
path: ``start_demo.bat`` creates the venv inside the app folder, and a path
rule would also unload flask or numpy (which cannot be re-imported).
"""

from __future__ import annotations

import importlib
import os
import site
import sys
from typing import Any, Dict, Iterable, List

import yaml

#: Module names a workspace provides by convention (flat, or under app_logic/).
WORKSPACE_MODULES = ("app_logic", "custom_view", "prod", "home", "shipment", "calib", "manual_actions")
#: Never purged: the framework itself, which workspace code imports from.
FRAMEWORK_MODULES = ("core", "devices", "app", "__main__", "tests", "conftest")

_SKIP_DIRS = {".venv", "venv", "env", "__pycache__", "node_modules", "logs", "backups"}


def config_dir_of(path: str) -> str | None:
    """Folder holding the workspace's config.yaml, or None if *path* is not a workspace."""
    app_logic = os.path.join(path, "app_logic")
    if os.path.isfile(os.path.join(app_logic, "config.yaml")):
        return app_logic
    if os.path.isfile(os.path.join(path, "config.yaml")):
        return path
    return None


def default_root(startup_workspace: str) -> str | None:
    """Parent of the startup workspace, unless that is a filesystem root (e.g. /webapp in Docker)."""
    parent = os.path.dirname(os.path.abspath(startup_workspace))
    return None if os.path.dirname(parent) == parent else parent


def _describe(path: str, current: str) -> Dict[str, Any]:
    config_dir = config_dir_of(path)
    item = {
        "name": os.path.basename(path),
        "path": path,
        "title": None,
        "layout": "app_logic" if config_dir and config_dir != path else "flat",
        "valid": True,
        "error": None,
        "current": os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(current)),
    }
    try:
        with open(os.path.join(config_dir, "config.yaml"), encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        if isinstance(config, dict) and config.get("title"):
            item["title"] = str(config["title"])
    except Exception as e:
        item["valid"] = False
        item["error"] = f"config.yaml: {e}"
    return item


def discover(root: str | None, current: str) -> List[Dict[str, Any]]:
    """Workspaces under *root*, plus *current* if it lives elsewhere. Sorted by name."""
    items: Dict[str, Dict[str, Any]] = {}
    if root and os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name)
            if name.startswith(".") or name in _SKIP_DIRS or not os.path.isdir(path):
                continue
            if config_dir_of(path):
                items[name] = _describe(path, current)
    if not any(item["current"] for item in items.values()) and config_dir_of(current):
        current_item = _describe(current, current)
        items.setdefault(current_item["name"], current_item)
    return sorted(items.values(), key=lambda item: item["name"].lower())


def resolve(root: str | None, current: str, name: str) -> str:
    """Path of the workspace called *name*, which must be one :func:`discover` lists.

    Only a listed folder name is accepted, never a path from the client:
    switching imports and runs that folder's code.
    """
    name = str(name or "").strip()
    if not name or name in (".", "..") or any(sep in name for sep in ("/", "\\")):
        raise ValueError(f"Invalid workspace name '{name}'.")
    for item in discover(root, current):
        if item["name"] == name:
            if not item["valid"]:
                raise ValueError(f"Workspace '{name}' has an invalid config.yaml: {item['error']}")
            return item["path"]
    raise ValueError(f"Unknown workspace '{name}'.")


def _top_level_names(folder: str) -> List[str]:
    """Importable top-level module names a folder provides (``x.py`` and packages)."""
    names = []
    try:
        entries = os.listdir(folder)
    except OSError:
        return names
    for entry in entries:
        if entry.startswith((".", "_")):
            continue
        full = os.path.join(folder, entry)
        if entry.endswith(".py"):
            names.append(entry[:-3])
        elif os.path.isdir(full) and entry not in _SKIP_DIRS:
            names.append(entry)
    return names


def _installed_dirs() -> List[str]:
    dirs = {sys.prefix, sys.base_prefix, sys.exec_prefix}
    try:
        dirs.update(site.getsitepackages())
    except AttributeError:  # pragma: no cover - some virtualenv builds
        pass
    user_site = getattr(site, "USER_SITE", None)
    if user_site:
        dirs.add(user_site)
    return [os.path.normcase(os.path.abspath(d)) for d in dirs if d]


def _is_installed(module) -> bool:
    """True for modules from the interpreter or site-packages (never purged)."""
    path = getattr(module, "__file__", None)
    if not path:
        return False
    path = os.path.normcase(os.path.abspath(path))
    return any(path == d or path.startswith(d + os.sep) for d in _installed_dirs())


def purge_modules(workspace_dirs: Iterable[str]) -> List[str]:
    """Drop the workspace modules from ``sys.modules`` so the next import reloads them.

    Purged: the conventional names (:data:`WORKSPACE_MODULES`) and every
    top-level module or package the given workspace folders provide, with
    their submodules. Never purged: the framework (``core``, ``devices``...)
    and anything installed in the interpreter or its site-packages.
    Returns the purged module names.
    """
    names = set(WORKSPACE_MODULES)
    for folder in workspace_dirs:
        if folder:
            names.update(_top_level_names(folder))
            config_dir = config_dir_of(folder)
            if config_dir and config_dir != folder:
                names.update(_top_level_names(config_dir))
    names.difference_update(FRAMEWORK_MODULES)

    purged = []
    for module_name in list(sys.modules):
        top = module_name.split(".", 1)[0]
        if top not in names:
            continue
        module = sys.modules.get(module_name)
        if module is None or _is_installed(module):
            continue
        del sys.modules[module_name]
        purged.append(module_name)
    importlib.invalidate_caches()
    return sorted(purged)
