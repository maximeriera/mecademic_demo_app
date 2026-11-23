"""Static wiring check for an existing device driver of the Mecademic demo app.

Run from the repo root with the repo's Python:

    .venv/Scripts/python.exe .claude/skills/validate-device/scripts/check_wiring.py <target>
    python .claude/skills/validate-device/scripts/check_wiring.py <target> --json

<target> is a registry type or alias (``asyril``), a driver class name
(``AsyrilEyePlus``), a driver file (``devices/Asyril.py``) or, for a workspace
``type: "custom"`` driver, ``path/to/file.py:ClassName``.

It never imports the driver module (so a missing vendor SDK doesn't matter):
the driver and its protocol module are parsed with ``ast``. Only
``devices.registry`` is imported, which loads no SDK.

Every line is tagged:
  [FAIL]  breaks the add-device contract, almost certainly a bug
  [WARN]  a heuristic hit or a likely problem: read the code before reporting it
  [OK]    checked and fine
  [INFO]  context, nothing to judge

The heuristics (I/O in status properties, a clear_fault that only drops a flag,
...) point at lines to read. They are not verdicts: confirm each one in the
code or with a behavioral test before it goes in the report.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys

STATUS_PROPS = ("faulted", "ready", "connected", "info", "link")
ABSTRACT = ("info", "connected", "ready", "faulted", "api", "initialize", "shutdown", "clear_fault", "abort")
OPTIONAL = ("telemetry", "io_points", "write_io", "images", "get_image", "settings", "connect", "disconnect")
# self.<attr>.<method>() calls on these attributes are never hardware I/O.
HARMLESS_ATTRS = {"logger", "_lock", "lock", "_identity", "_fault", "_open"}
# Method names that are pure in-memory work even on an API object.
HARMLESS_CALLS = {"get", "copy", "items", "keys", "values", "is_set", "locked", "format", "join", "lower", "upper", "strip"}
CONNECTISH = re.compile(r"(connect|open|init|login|start|home|reference|calibrat)", re.I)
MOTIONISH = re.compile(r"(home|homing|reference|calibrat|move|levitat|activate|start_production|run)", re.I)


class Report:
    def __init__(self):
        self.lines: list[dict] = []

    def add(self, level: str, section: str, message: str, where: str | None = None):
        self.lines.append({"level": level, "section": section, "message": message, "where": where})

    def print_text(self):
        section = None
        for line in self.lines:
            if line["section"] != section:
                section = line["section"]
                print(f"\n== {section} ==")
            where = f"  ({line['where']})" if line["where"] else ""
            print(f"[{line['level']}] {line['message']}{where}")
        counts = {lvl: sum(1 for l in self.lines if l["level"] == lvl) for lvl in ("FAIL", "WARN", "OK")}
        print(f"\nSummary: {counts['FAIL']} FAIL, {counts['WARN']} WARN, {counts['OK']} OK")


# --- locating the driver -------------------------------------------------------

def load_registry(repo: str):
    sys.path.insert(0, repo)
    os.environ.setdefault("MECADEMIC_CONSOLE_LOG_LEVEL", "WARNING")
    from devices import registry  # noqa: E402  (loads no vendor SDK)
    return registry


def locate(target: str, repo: str, registry):
    """Return (spec or None, driver file path, class name)."""
    if ":" in target and target.split(":")[0].endswith(".py"):
        path, cls = target.rsplit(":", 1)
        return None, os.path.join(repo, path), cls

    spec = registry.resolve_type(target)
    if spec is None:
        norm = target.replace("\\", "/")
        for s in registry._SPECS.values():
            module, _, cls = s.target.partition(":")
            module_file = module.replace(".", "/") + ".py"
            if cls == target or norm.endswith(module_file):
                spec = s
                break
    if spec is not None:
        module, _, cls = spec.target.partition(":")
        return spec, os.path.join(repo, *module.split(".")) + ".py", cls

    if target.endswith(".py") and os.path.exists(os.path.join(repo, target)):
        path = os.path.join(repo, target)
        tree = ast.parse(open(path, encoding="utf-8").read())
        classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)
                   and any(_name(b) == "Device" for b in n.bases)]
        if len(classes) == 1:
            return None, path, classes[0]
        raise SystemExit(f"{target}: expected one Device subclass, found {classes or 'none'}; pass {target}:ClassName")

    known = ", ".join(sorted(s.type for s in registry._SPECS.values()))
    raise SystemExit(f"Unknown target {target!r}. Registered types: {known}. "
                     "For a workspace driver pass path/to/file.py:ClassName.")


# --- AST helpers -----------------------------------------------------------------

def _name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _chain(node) -> list[str]:
    """self._api.foo.bar -> ['self', '_api', 'foo', 'bar']"""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif isinstance(node, ast.Call):
        parts.append(_name(node.func) + "()")
    return list(reversed(parts))


class Unresolved(str):
    """A default the AST can't evaluate (a call, an imported constant...)."""

    def __repr__(self):
        return f"<{str(self)}>"


def _literal(node, constants: dict):
    try:
        return ast.literal_eval(node)
    except Exception:
        if isinstance(node, ast.Name) and node.id in constants:
            return constants[node.id]
        return Unresolved(ast.unparse(node))


def _is_property(fn) -> bool:
    return any(_name(d) == "property" for d in fn.decorator_list)


def _calls(fn):
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            yield node


def _body_is_trivial(fn) -> bool:
    body = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
    if not body:
        return True
    if len(body) == 1:
        s = body[0]
        if isinstance(s, ast.Pass):
            return True
        if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and s.value.value is Ellipsis:
            return True
        if isinstance(s, ast.Return) and isinstance(s.value, ast.Call):
            chain = _chain(s.value.func)
            if chain and chain[0] == "super()":
                return True
    return False


def module_constants(tree, repo: str, module_dir: str) -> dict:
    """Simple module-level constants, plus those imported from a sibling module."""
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                consts[node.targets[0].id] = ast.literal_eval(node.value)
            except Exception:
                pass
        elif isinstance(node, ast.ImportFrom) and node.level >= 1 and node.module:
            path = os.path.join(module_dir, *node.module.split(".")) + ".py"
            if os.path.exists(path):
                sub = ast.parse(open(path, encoding="utf-8").read())
                sub_consts = module_constants(sub, repo, os.path.dirname(path))
                for alias in node.names:
                    if alias.name in sub_consts:
                        consts[alias.asname or alias.name] = sub_consts[alias.name]
    return consts


IOISH = re.compile(r"(send|recv|query|write|read|request|execute|get_\w*status|Get\w+|peek|MSG_PEEK|_lock|acquire|"
                   r"__send|__receive|is_socket_open)", re.I)


def api_module_paths(tree, repo: str) -> list[str]:
    paths = []
    for n in tree.body:
        if isinstance(n, ast.ImportFrom) and n.module and (n.module.startswith("api.") or n.module.startswith("devices.api.")):
            mod = n.module.split("api.", 1)[1]
            path = os.path.join(repo, "devices", "api", *mod.split(".")) + ".py"
            if os.path.exists(path):
                paths.append(path)
    return paths


def index_api_members(tree, repo: str) -> dict:
    """Name -> {kind: property|method|attribute, calls, where} for every class in
    the driver's devices/api modules (one hop: their bodies are not followed)."""
    index = {}
    for path in api_module_paths(tree, repo):
        rel = os.path.relpath(path, repo).replace("\\", "/")
        mtree = ast.parse(open(path, encoding="utf-8").read())
        for cls in (n for n in mtree.body if isinstance(n, ast.ClassDef)):
            for node in cls.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name == "__init__":
                        for t in ast.walk(node):
                            if isinstance(t, ast.Attribute) and isinstance(t.ctx, ast.Store) and _chain(t)[:1] == ["self"]:
                                index.setdefault(t.attr, {"kind": "attribute", "calls": [], "where": f"{rel}:{t.lineno}"})
                        continue
                    if any(_name(d) == "setter" or (isinstance(d, ast.Attribute) and d.attr == "setter")
                           for d in node.decorator_list):
                        continue
                    calls = sorted({".".join(_chain(c.func)) for c in _calls(node)} - {""})
                    withs = [".".join(_chain(i.context_expr)) for w in ast.walk(node) if isinstance(w, ast.With)
                             for i in w.items]
                    index[node.name] = {"kind": "property" if _is_property(node) else "method",
                                        "calls": calls + [f"with {w}" for w in withs if w],
                                        "where": f"{rel}:{node.lineno}"}
    return index


# --- checks ----------------------------------------------------------------------

def check_class(rep: Report, path: str, cls_name: str, repo: str):
    rel = os.path.relpath(path, repo).replace("\\", "/")
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    consts = module_constants(tree, repo, os.path.dirname(path))
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls_name), None)
    if cls is None:
        rep.add("FAIL", "Driver class", f"class {cls_name} not found in {rel}")
        return None
    S = "Driver class"
    rep.add("INFO", S, f"{cls_name} in {rel}, bases: {', '.join(_name(b) for b in cls.bases)}", f"{rel}:{cls.lineno}")
    api_index = index_api_members(tree, repo)

    members = {}
    class_attrs = {}
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            members[node.name] = node
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    class_attrs[t.id] = _literal(node.value, consts)

    inherits_other = any(_name(b) not in ("Device",) for b in cls.bases)
    for m in ABSTRACT:
        if m in members:
            fn = members[m]
            is_prop = _is_property(fn)
            want_prop = m in ("info", "connected", "ready", "faulted", "api")
            if want_prop and not is_prop:
                rep.add("FAIL", S, f"{m} must be a @property", f"{rel}:{fn.lineno}")
        elif not inherits_other:
            rep.add("FAIL", S, f"abstract member {m} not implemented")
        else:
            rep.add("INFO", S, f"{m} not defined here (maybe inherited from a mixin/parent)")
    present_optional = [m for m in OPTIONAL if m in members]
    rep.add("INFO", S, "optional members: " + (", ".join(present_optional) or "none"))
    reconnect = class_attrs.get("supports_reconnect", False)
    rep.add("INFO", S, f"supports_reconnect = {reconnect}")

    # Status properties: any call that could reach the hardware.
    S2 = "Status properties (fault monitor polls these every 200 ms)"
    for m in STATUS_PROPS:
        fn = members.get(m)
        if fn is None:
            continue
        suspicious = []
        for call in _calls(fn):
            chain = _chain(call.func)
            if len(chain) >= 3 and chain[0] == "self" and chain[1] not in HARMLESS_ATTRS and chain[-1] not in HARMLESS_CALLS:
                suspicious.append(".".join(chain) + "()")
            elif len(chain) == 2 and chain[0] == "self" and chain[1] not in STATUS_PROPS and not chain[1].startswith("_fmt"):
                suspicious.append(".".join(chain) + "()")
        attr_reads = sorted({".".join(_chain(n)) for n in ast.walk(fn)
                             if isinstance(n, ast.Attribute) and len(_chain(n)) >= 3 and _chain(n)[0] == "self"
                             and _chain(n)[1] not in HARMLESS_ATTRS and not isinstance(n.ctx, ast.Store)})
        assigns = []
        for n in ast.walk(fn):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target] if isinstance(n, ast.AugAssign) else []
            assigns += [t for t in targets if isinstance(t, ast.Attribute) and _chain(t)[:1] == ["self"]]
        if suspicious:
            rep.add("WARN", S2, f"{m} calls {', '.join(sorted(set(suspicious)))}: check whether that does I/O "
                    "or takes the session lock", f"{rel}:{fn.lineno}")
        elif not attr_reads:
            rep.add("OK", S2, f"{m} makes no calls on other objects", f"{rel}:{fn.lineno}")
        # One hop into the API module: is self._api.X a plain attribute, or a
        # property/method whose body talks to the device?
        for read in attr_reads:
            parts = read.split(".")
            hop = api_index.get(parts[2]) if len(parts) >= 3 else None
            if hop is None:
                if not suspicious:
                    rep.add("WARN", S2, f"{m} reads {read}: not found in the API module, check what it is",
                            f"{rel}:{fn.lineno}")
            elif hop["kind"] == "attribute":
                rep.add("OK", S2, f"{m} reads {read}: a plain attribute of the API object", f"{rel}:{fn.lineno}")
            elif not hop["calls"]:
                rep.add("OK", S2, f"{m} reads {read}: a {hop['kind']} at {hop['where']} that makes no calls",
                        f"{rel}:{fn.lineno}")
            else:
                io = [c for c in hop["calls"] if IOISH.search(c)]
                rep.add("FAIL" if io else "WARN", S2,
                        f"{m} reads {read}, a {hop['kind']} at {hop['where']} that calls {', '.join(hop['calls'])}"
                        + (f": likely device I/O or a session lock on every poll ({', '.join(io)}); follow it to confirm"
                           if io else ": read its body"),
                        f"{rel}:{fn.lineno}")
        if assigns:
            rep.add("WARN", S2, f"{m} assigns state while being read (a getter with side effects)", f"{rel}:{fn.lineno}")

    # Constructor
    S3 = "Constructor"
    init = members.get("__init__")
    ctor = {}
    if init is None:
        rep.add("INFO", S3, "no __init__ in this class")
    else:
        args = init.args
        positional = args.posonlyargs + args.args
        defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
        for a, d in zip(positional, defaults):
            if a.arg != "self":
                ctor[a.arg] = ("<required>" if d is None else _literal(d, consts))
        for a, d in zip(args.kwonlyargs, args.kw_defaults):
            ctor[a.arg] = "<required>" if d is None else _literal(d, consts)
        rep.add("INFO", S3, "arguments: " + ", ".join(f"{k}={v!r}" if v != "<required>" else k for k, v in ctor.items()),
                f"{rel}:{init.lineno}")
        names = list(ctor)
        if "name" not in ctor:
            rep.add("FAIL", S3, "no `name` argument: create_device() passes name=<entry name>", f"{rel}:{init.lineno}")
        elif names[-1] != "name":
            rep.add("WARN", S3, "`name` is not the last argument", f"{rel}:{init.lineno}")
        else:
            rep.add("OK", S3, "`name` argument present and last")
        connects = []
        for call in _calls(init):
            chain = _chain(call.func)
            if chain and chain[0] == "self" and len(chain) >= 3 and CONNECTISH.search(chain[-1]):
                connects.append(".".join(chain) + "()")
        if connects:
            rep.add("WARN", S3, f"__init__ calls {', '.join(connects)}: the constructor must not connect "
                    "(a dead device would block app startup); check what these do", f"{rel}:{init.lineno}")

    # Lifecycle heuristics
    S4 = "Lifecycle"
    abort = members.get("abort")
    if abort is not None and _body_is_trivial(abort):
        rep.add("WARN", S4, "abort() does nothing (pass / super().abort()): fine only if the device never blocks "
                "or moves, and it should at least log a warning", f"{rel}:{abort.lineno}")
    cf = members.get("clear_fault")
    if cf is not None:
        calls = [".".join(_chain(c.func)) for c in _calls(cf)]
        reopen = [c for c in calls if CONNECTISH.search(c.split(".")[-1]) or c.endswith("initialize")]
        moving = [c for c in calls if MOTIONISH.search(c.split(".")[-1])]
        if not calls or all(c.startswith("self.logger") for c in calls):
            rep.add("WARN", S4, "clear_fault() makes no calls: it probably only drops a flag (stale session)",
                    f"{rel}:{cf.lineno}")
        elif not reopen:
            rep.add("WARN", S4, f"clear_fault() calls {', '.join(calls)} but nothing that reopens the session",
                    f"{rel}:{cf.lineno}")
        if any(c.endswith(".initialize") or c == "self.initialize" for c in calls):
            rep.add("WARN", S4, "clear_fault() calls initialize(): check initialize() doesn't home/move hardware",
                    f"{rel}:{cf.lineno}")
        if moving:
            rep.add("WARN", S4, f"clear_fault() calls {', '.join(moving)}: Clear faults must never move hardware",
                    f"{rel}:{cf.lineno}")
    sd = members.get("shutdown")
    if sd is not None:
        guarded = any(isinstance(n, (ast.Try, ast.If)) for n in ast.walk(sd))
        if not guarded:
            rep.add("WARN", S4, "shutdown() has no guard (if/try): check it is safe when never initialized and twice",
                    f"{rel}:{sd.lineno}")
    ini = members.get("initialize")
    if ini is not None:
        moving = sorted({".".join(_chain(c.func)) for c in _calls(ini) if MOTIONISH.search(_name(c.func))})
        if moving:
            rep.add("INFO", S4, f"initialize() calls {', '.join(moving)}: it may move hardware. Ask before a live run.",
                    f"{rel}:{ini.lineno}")

    # Logging / prints
    S5 = "Logging"
    prints = [n.lineno for n in ast.walk(cls) if isinstance(n, ast.Call) and _name(n.func) == "print"]
    if prints:
        rep.add("WARN", S5, f"print() in the driver class (use self.logger): lines {prints}", rel)
    unprefixed = []
    for call in _calls(cls):
        chain = _chain(call.func)
        if len(chain) == 3 and chain[:2] == ["self", "logger"] and call.args:
            arg = call.args[0]
            text = ast.unparse(arg)
            if "device_id" not in text:
                unprefixed.append(call.lineno)
    if unprefixed:
        rep.add("WARN", S5, f"logger messages without the [{{self.device_id}}] prefix: lines {unprefixed}", rel)
    elif not prints:
        rep.add("OK", S5, "logger messages are prefixed, no print()")

    return {"members": members, "ctor": ctor, "reconnect": reconnect, "tree": tree, "rel": rel}


def check_registry(rep: Report, spec, ctor: dict, registry):
    S = "Registry (devices/registry.py)"
    if spec is None:
        rep.add("INFO", S, "not a registered type (workspace custom driver): constructor must coerce its own args")
        return
    rep.add("INFO", S, f"type '{spec.type}', aliases {list(spec.aliases)}, target {spec.target}")
    if not spec.doc:
        rep.add("WARN", S, "DeviceSpec has no doc string")
    for alias in spec.aliases:
        owner = registry._ALIASES.get(alias.lower())
        if owner != spec.type.lower():
            rep.add("FAIL", S, f"alias '{alias}' is claimed by type '{owner}'")
    if not ctor:
        return
    params = spec.params
    for key, p in params.items():
        if key not in ctor:
            rep.add("FAIL", S, f"Param '{key}' has no constructor argument of that name (TypeError at create_device)")
            continue
        c_default = ctor[key]
        if p.required and c_default != "<required>":
            rep.add("WARN", S, f"'{key}' is required in the registry but defaults to {c_default!r} in the constructor")
        elif not p.required and c_default == "<required>":
            rep.add("WARN", S, f"'{key}' defaults to {p.default!r} in the registry but is required in the constructor "
                    "(task code constructing it directly must pass it)")
        elif not p.required and isinstance(c_default, Unresolved):
            rep.add("INFO", S, f"'{key}': registry default {p.default!r}, constructor default {c_default!r} "
                    "(not a literal: compare by hand)")
        elif not p.required and c_default != p.default:
            rep.add("WARN", S, f"'{key}' default differs: registry {p.default!r}, constructor {c_default!r}")
        else:
            rep.add("OK", S, f"'{key}' matches ({p.type}, default {'required' if p.required else repr(p.default)})")
    for key in ctor:
        if key != "name" and key not in params:
            rep.add("WARN", S, f"constructor argument '{key}' has no Param: a config can't set it")


def check_export(rep: Report, cls_name: str, repo: str, is_builtin: bool):
    S = "Export (devices/__init__.py)"
    if not is_builtin:
        return
    src = open(os.path.join(repo, "devices", "__init__.py"), encoding="utf-8").read()
    in_all = re.search(r"__all__\s*=\s*\[[^\]]*['\"]" + re.escape(cls_name) + r"['\"]", src, re.S)
    lazy = re.search(r"name\s*==\s*['\"]" + re.escape(cls_name) + r"['\"]", src)
    rep.add("OK" if in_all else "FAIL", S, f"{cls_name} {'in' if in_all else 'missing from'} __all__")
    rep.add("OK" if lazy else "FAIL", S, f"lazy __getattr__ branch for {cls_name} {'present' if lazy else 'missing'}")


def check_api_module(rep: Report, info: dict, repo: str):
    S = "Protocol module (devices/api/)"
    tree = info["tree"]
    api_mods = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module
                and (n.module.startswith("api.") or n.module.startswith("devices.api."))]
    if not api_mods:
        rep.add("INFO", S, "driver imports no devices/api module (vendor SDK or inline protocol)")
        return []
    paths = []
    for imp in api_mods:
        mod = imp.module.split("api.", 1)[1]
        path = os.path.join(repo, "devices", "api", *mod.split(".")) + ".py"
        rel = os.path.relpath(path, repo).replace("\\", "/")
        if not os.path.exists(path):
            rep.add("FAIL", S, f"{rel} imported but not found")
            continue
        paths.append(path)
        src = open(path, encoding="utf-8").read()
        mtree = ast.parse(src)
        bad = [ast.unparse(n) for n in ast.walk(mtree) if isinstance(n, ast.ImportFrom)
               and ((n.module or "").startswith(("devices", "core")) or n.level >= 2)]
        bad += [ast.unparse(n) for n in ast.walk(mtree) if isinstance(n, ast.Import)
                and any(a.name.startswith(("devices", "core")) for a in n.names)]
        rep.add("WARN" if bad else "OK", S, f"{rel}: " + (f"imports app code: {bad}" if bad else "standalone (no devices/ or core/ imports)"))
        has_lock = "RLock" in src or "Lock(" in src
        rep.add("OK" if has_lock else "WARN", S, f"{rel}: {'has a threading lock' if has_lock else 'no threading lock: concurrent task + Live panel exchanges can interleave'}")
        has_main = "__main__" in src
        rep.add("OK" if has_main else "WARN", S, f"{rel}: {'has a __main__ CLI' if has_main else 'no __main__ CLI for testing at the machine'}")
        has_all = "__all__" in src
        if not has_all:
            rep.add("WARN", S, f"{rel}: no __all__")
        logs_from_driver = re.search(r"def __init__\([^)]*logger", src)
        if logs_from_driver:
            rep.add("INFO", S, f"{rel}: takes a logger from the driver (older pattern)")
    return paths


def third_party_imports(paths: list[str]) -> set[str]:
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    found = set()
    for path in paths:
        tree = ast.parse(open(path, encoding="utf-8").read())
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                names = [a.name.split(".")[0] for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                names = [n.module.split(".")[0]]
            else:
                continue
            for name in names:
                if name not in stdlib and name not in ("devices", "core", "app_logic", "__future__"):
                    found.add(name)
    return found


def check_docs(rep: Report, spec, cls_name: str, info: dict, repo: str, module_paths: list[str]):
    S = "Docs, config reference, dependencies"
    if spec is None:
        rep.add("INFO", S, "workspace driver: document its keys in a comment above the entry in that workspace's config.yaml")
        return
    t = spec.type
    cfg_path = os.path.join(repo, "app_logic", "config.yaml")
    if os.path.exists(cfg_path):
        cfg = open(cfg_path, encoding="utf-8").read()
        ok = re.search(r"#\s*type:\s*['\"]" + re.escape(t) + r"['\"]", cfg)
        rep.add("OK" if ok else "WARN", S, f"app_logic/config.yaml reference block for '{t}' {'present' if ok else 'missing'}")
        if ok:
            block = cfg[ok.start():].split("# ---", 1)[0]
            missing = [k for k in spec.params if not re.search(r"#\s*" + re.escape(k) + r"\s*:", block)]
            if missing:
                rep.add("WARN", S, f"config.yaml reference block doesn't list: {', '.join(missing)}")
    readme_path = os.path.join(repo, "README.md")
    if os.path.exists(readme_path):
        readme = open(readme_path, encoding="utf-8").read()
        row = re.search(r"\|\s*`" + re.escape(t) + r"`\s*\|\s*`" + re.escape(cls_name) + r"`", readme)
        rep.add("OK" if row else "WARN", S, f"README 'Supported device types' row {'present' if row else 'missing or names another class'}")
        file_name = os.path.basename(info["rel"])
        rep.add("OK" if file_name in readme else "WARN", S, f"README mentions {file_name} (Project Structure) "
                f"{'yes' if file_name in readme else 'no'}")
        if info["reconnect"] is True:
            rel_sec = readme.split("## Releasing a device", 1)[-1].split("\n## ", 1)[0]
            ok = f"`{t}`" in rel_sec
            rep.add("OK" if ok else "WARN", S, f"README 'Releasing a device' list {'includes' if ok else 'omits'} `{t}` (supports_reconnect)")
        has_live = any(m in info["members"] for m in ("telemetry", "io_points", "images"))
        if has_live:
            live = re.search(r"\|\s*Driver\s*\|\s*Live data\s*\|(.*?)\n\n", readme, re.S)
            ok = live and (cls_name in live.group(1) or t in live.group(1))
            rep.add("OK" if ok else "WARN", S, f"README Live data table {'lists' if ok else 'omits'} this driver")
    req_path = os.path.join(repo, "requirements.txt")
    deps = third_party_imports([os.path.join(repo, info["rel"])] + module_paths)
    if deps:
        if os.path.exists(req_path):
            req = open(req_path, encoding="utf-8").read().lower()
            for dep in sorted(deps):
                variants = {dep.lower(), dep.lower().replace("_", "-"), dep.lower().replace("_", "")}
                ok = any(v in req for v in variants)
                rep.add("OK" if ok else "WARN", S, f"third-party import '{dep}' {'found' if ok else 'not found'} in requirements.txt"
                        + ("" if ok else " (import name may differ from the PyPI name: check by hand)"))
        else:
            rep.add("INFO", S, f"requirements.txt not in the working tree; third-party imports: {', '.join(sorted(deps))}")


def check_tests(rep: Report, spec, cls_name: str, info: dict, repo: str):
    S = "Tests (tests/)"
    tests_dir = os.path.join(repo, "tests")
    module = os.path.splitext(os.path.basename(info["rel"]))[0]
    driver_tests, type_only = [], []
    for fn in sorted(os.listdir(tests_dir)):
        if fn.startswith("test_") and fn.endswith(".py"):
            src = open(os.path.join(tests_dir, fn), encoding="utf-8").read()
            if re.search(r"\b" + re.escape(cls_name) + r"\b", src) or f"devices.{module}" in src:
                driver_tests.append(fn)
            elif spec and re.search(r"['\"]" + re.escape(spec.type) + r"['\"]", src):
                type_only.append(fn)
    if type_only:
        rep.add("INFO", S, f"only the type string appears in: {', '.join(type_only)} (registry-level, not driver behavior)")
    if driver_tests:
        rep.add("OK", S, f"tests exercising the driver: {', '.join(driver_tests)} "
                "(compare what they cover with add-device/references/testing.md)")
    else:
        rep.add("FAIL", S, "no test in tests/ exercises the driver class: write tests/test_<snake_name>.py")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target")
    ap.add_argument("--repo", default=os.getcwd())
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    repo = os.path.abspath(args.repo)

    registry = load_registry(repo)
    spec, path, cls_name = locate(args.target, repo, registry)
    rep = Report()
    info = check_class(rep, path, cls_name, repo)
    if info is not None:
        check_registry(rep, spec, info["ctor"], registry)
        check_export(rep, cls_name, repo, spec is not None)
        module_paths = check_api_module(rep, info, repo)
        check_docs(rep, spec, cls_name, info, repo, module_paths)
        check_tests(rep, spec, cls_name, info, repo)

    if args.json:
        print(json.dumps({"target": args.target, "class": cls_name, "file": os.path.relpath(path, repo),
                          "type": spec.type if spec else None, "lines": rep.lines}, indent=2, default=str))
    else:
        print(f"Wiring check: {cls_name} ({os.path.relpath(path, repo)})" + (f", type '{spec.type}'" if spec else ""))
        rep.print_text()


if __name__ == "__main__":
    main()
