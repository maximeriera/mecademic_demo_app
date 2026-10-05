"""
core/LogSetup.py
----------------
Central logging configuration for the whole application.

Replaces three near-identical copy-pasted ``_setup_logger()`` blocks (Flask
layer, ``ApplicationController``, ``Device``) that each emitted
``asctime | levelname | message`` — a format with no thread name, no logger
name and no source location, in an application where a monitor thread, one or
more task threads and several Flask request threads all write to the same
files concurrently. Working out *who* logged a line meant relying on
hand-written ``[MonitorThread]`` prefixes that only some messages carried.

Logger hierarchy
----------------
Everything hangs off a single ``mecademic`` parent::

    mecademic                     -> combined.log + stdout
      mecademic.app               -> logs/app/app.log
      mecademic.controller        -> logs/app/ApplicationController.log
      mecademic.device.<id>       -> logs/devices/<id>.log

Per-component handlers sit on the children, so the existing per-file layout
(and the Logs tab that reads it) is unchanged. The combined and console
handlers sit on the parent, so records reach them by propagation.

``mecademic.propagate`` is set to ``False``, which also makes the application
immune to anything a third-party module does to the root logger — notably
``devices/api/gigE_vision.py``, which calls ``logging.basicConfig()`` at import
and used to duplicate every record onto stderr in a different format.

Why combined.log
----------------
Diagnosing a fault previously meant opening ``app.log``,
``ApplicationController.log`` and one file per device, then aligning
timestamps by eye. ``combined.log`` is the same records in one chronological
stream, so an HTTP request, the state transition it caused and the device error
underneath it are adjacent lines.

Environment
-----------
``MECADEMIC_LOG_LEVEL``          file level, default ``DEBUG``
``MECADEMIC_CONSOLE_LOG_LEVEL``  stdout level, default ``INFO``
``MECADEMIC_DEMO_ROOT``          root under which ``logs/`` is created
"""

from __future__ import annotations

import logging
import os
import re
import sys
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from typing import Any

ROOT_LOGGER_NAME = "mecademic"

#: Correlation id for the HTTP request being served on this thread, if any.
#: Set by the Flask ``before_request`` hook in app.py. Thread-local by nature,
#: so a request id does not leak into the monitor or task threads — those are
#: identified by their thread name instead.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_LOG_FORMAT = (
    "%(asctime)s | %(levelname)-7s | %(thread_label)-16s | %(short_name)-22s | "
    "%(module)s:%(lineno)-4d |%(ctx)s %(message)s"
)
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_FILE_MAX_BYTES = 5 * 1024 * 1024
_FILE_BACKUPS = 2
# combined.log is the primary debugging artefact, so give it a larger budget.
_COMBINED_MAX_BYTES = 10 * 1024 * 1024
_COMBINED_BACKUPS = 3

_configured = False


class _ContextFilter(logging.Filter):
    """Adds the derived fields the formatter needs.

    ``short_name`` strips the ``mecademic.`` prefix so lines stay narrow, and
    ``ctx`` renders the request id only when one is set — contributing zero
    width otherwise, so non-request logs are not padded with empty columns.
    """

    #: Werkzeug names each request thread "Thread-58 (process_request_thread)",
    #: which overflows the thread column and pushes every other field out of
    #: alignment. Render those as "http-58" instead.
    _WERKZEUG_THREAD_RE = re.compile(r"^Thread-(\d+) \(process_request_thread\)$")

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name
        record.short_name = name[len(ROOT_LOGGER_NAME) + 1:] if name.startswith(ROOT_LOGGER_NAME + ".") else name

        match = self._WERKZEUG_THREAD_RE.match(record.threadName or "")
        record.thread_label = f"http-{match.group(1)}" if match else (record.threadName or "-")

        request_id = request_id_var.get()
        record.ctx = f" [req={request_id}]" if request_id else ""
        return True


def _level_from_env(var_name: str, default: str) -> int:
    raw = str(os.environ.get(var_name, default)).strip().upper()
    level = logging.getLevelName(raw)
    if not isinstance(level, int):
        print(f"Invalid {var_name}={raw!r}; falling back to {default}.", file=sys.stderr)
        level = logging.getLevelName(default)
    return level


def log_root_dir() -> str:
    """Directory that ``logs/`` lives under."""
    return os.environ.get(
        "MECADEMIC_DEMO_ROOT",
        os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")),
    )


def _make_formatter() -> logging.Formatter:
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
    formatter.default_msec_format = "%s.%03d"
    return formatter


def _add_file_handler(logger: logging.Logger, path: str, level: int,
                      max_bytes: int, backups: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups,
                                  encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(_make_formatter())
    handler.addFilter(_ContextFilter())
    logger.addHandler(handler)


def configure_logging() -> logging.Logger:
    """Install the shared handlers on the ``mecademic`` parent logger.

    Idempotent — safe to call from both the Flask layer and the controller,
    whichever imports first.
    """
    global _configured
    root = logging.getLogger(ROOT_LOGGER_NAME)
    if _configured:
        return root

    file_level = _level_from_env("MECADEMIC_LOG_LEVEL", "DEBUG")
    console_level = _level_from_env("MECADEMIC_CONSOLE_LOG_LEVEL", "INFO")

    # The parent must pass everything its handlers might want.
    root.setLevel(min(file_level, console_level))
    # Do not propagate to the real root logger: keeps third-party
    # basicConfig() calls from duplicating or reformatting our records.
    root.propagate = False

    # Everything, in one chronological stream.
    _add_file_handler(
        root,
        os.path.join(log_root_dir(), "logs", "app", "combined.log"),
        file_level,
        _COMBINED_MAX_BYTES,
        _COMBINED_BACKUPS,
    )

    # stdout, so `docker compose logs -f` is actually useful for the service.
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(console_level)
    console.setFormatter(_make_formatter())
    console.addFilter(_ContextFilter())
    root.addHandler(console)

    _quiet_noisy_third_party_loggers(console)

    _configured = True
    root.info(
        "Logging configured | file_level=%s console_level=%s combined=%s",
        logging.getLevelName(file_level),
        logging.getLevelName(console_level),
        os.path.join(log_root_dir(), "logs", "app", "combined.log"),
    )
    return root


def _quiet_noisy_third_party_loggers(console_handler: logging.Handler) -> None:
    """Stop third-party loggers from drowning the output.

    Werkzeug writes one INFO access line per request to a ``StreamHandler`` it
    installs on the ``werkzeug`` logger itself. With the UI polling /api/status
    at 10 Hz that buried every application record in ``docker compose logs``.
    The app's own ``after_request`` hook already logs requests — with duration,
    status and a correlation id, and without the polling endpoints — so the
    access log is redundant.

    Setting the level to WARNING keeps genuine Werkzeug problems visible.
    Attaching our own handler first also stops Werkzeug installing its
    unformatted one (it only does so when the logger has no suitable handler).

    Set ``MECADEMIC_ACCESS_LOG=1`` to get the raw access lines back.
    """
    want_access_log = str(os.environ.get("MECADEMIC_ACCESS_LOG", "")).strip().lower() in {
        "1", "true", "yes", "on",
    }

    werkzeug_logger = logging.getLogger("werkzeug")
    werkzeug_logger.handlers.clear()
    werkzeug_logger.setLevel(logging.INFO if want_access_log else logging.WARNING)
    werkzeug_logger.addHandler(console_handler)
    werkzeug_logger.propagate = False

    # Harvester's GenTL layer logs driver stack traces at INFO during normal
    # camera discovery retries.
    for noisy in ("harvesters", "harvesters.core"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(child_name: str, filename: str, subdir: str = "app") -> logging.Logger:
    """Return a ``mecademic.<child_name>`` logger with its own rotating file.

    The file keeps its historical name and location so existing logs and the
    Logs tab are unaffected; records also reach ``combined.log`` and stdout by
    propagating to the parent.
    """
    configure_logging()

    logger = logging.getLogger(f"{ROOT_LOGGER_NAME}.{child_name}")
    logger.setLevel(_level_from_env("MECADEMIC_LOG_LEVEL", "DEBUG"))

    # Only this logger's own file handler, not the inherited ones.
    if not logger.handlers:
        _add_file_handler(
            logger,
            os.path.join(log_root_dir(), "logs", subdir, filename),
            _level_from_env("MECADEMIC_LOG_LEVEL", "DEBUG"),
            _FILE_MAX_BYTES,
            _FILE_BACKUPS,
        )
    return logger


def get_app_logger() -> logging.Logger:
    """Logger for the Flask layer -> ``logs/app/app.log``."""
    return get_logger("app", "app.log")


def get_controller_logger() -> logging.Logger:
    """Logger for the controller and its task threads -> ``logs/app/ApplicationController.log``."""
    return get_logger("controller", "ApplicationController.log")


def get_device_logger(device_id: str) -> logging.Logger:
    """Logger for one device -> ``logs/devices/<device_id>.log``."""
    return get_logger(f"device.{device_id}", f"{device_id}.log", subdir="devices")


def get_module_logger(suffix: str) -> logging.Logger:
    """Logger for a driver module that has no device instance to borrow from.

    Has no file of its own; records surface in ``combined.log`` and on stdout.
    Used instead of bare ``logging.info()`` calls, which went to the real root
    logger and therefore to none of the application's own files.
    """
    configure_logging()
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{suffix}")


def describe_logging() -> dict[str, Any]:
    """Snapshot of the active logging configuration, for ``/api/logs/config``."""
    configure_logging()
    return {
        "file_level": logging.getLevelName(_level_from_env("MECADEMIC_LOG_LEVEL", "DEBUG")),
        "console_level": logging.getLevelName(
            _level_from_env("MECADEMIC_CONSOLE_LOG_LEVEL", "INFO")
        ),
        "log_root": os.path.join(log_root_dir(), "logs"),
        "combined_log": "app/combined.log",
        "format": _LOG_FORMAT,
    }
