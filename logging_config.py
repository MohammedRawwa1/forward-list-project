"""Centralised logging configuration for the bot.

This module is the single source of truth for logging setup. Import it at
application startup to configure both console and file handlers with a
consistent format and log level. The console handler uses a loguru-style
colored formatter for a better developer experience.

All log records are enqueued via a QueueHandler and processed by a background
QueueListener thread, keeping the async event loop free from blocking I/O.
The listener is gracefully shut down on interpreter exit via atexit.

Usage:
    import logging_config  # configures root logger as a side effect

    # In any module:
    import logging
    logger = logging.getLogger(__name__)
"""

import atexit
import json as _json
import logging
import logging.handlers
import os
import sys
from queue import Queue
from traceback import format_exception
from typing import Any

from dotenv import load_dotenv

load_dotenv()  # ensure env vars are loaded before reading LOG_LEVEL

LOG_FORMAT = os.getenv("LOG_FORMAT", "text").lower()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
# Maximum number of queued log records before put() starts blocking.
# Under sustained high load this limits memory growth; the default of 10000
# records (~a few MB) is a safe upper bound for a single-process bot.
LOG_QUEUE_MAXSIZE = int(os.getenv("LOG_QUEUE_MAXSIZE", "10000"))

TEXT_LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s - %(message)s"

# Guard: prevent duplicate setup when the module is re-executed (e.g.
# via importlib.reload).  We inspect the root logger for an existing
# QueueHandler rather than using a module-level flag, which would be
# reset on reload anyway.


class ColoredConsoleFormatter(logging.Formatter):
    """Loguru-style colored formatter for console output.

    Applies ANSI color codes to log level, module name, and message so the
    developer terminal output is easier to scan at a glance.  Colours are
    stripped from the record *after* formatting so the file handler (which
    uses a separate plain-text formatter) is unaffected.
    """

    _COLORS = {
        "DEBUG": "\033[36m",  # Cyan
        "INFO": "\033[32m",  # Green
        "WARNING": "\033[33m",  # Yellow
        "ERROR": "\033[31m",  # Red
        "CRITICAL": "\033[1;31m",  # Bold red
    }
    _RESET = "\033[0m"
    _DIM = "\033[2m"

    def format(self, record):
        orig_levelname = record.levelname
        orig_name = record.name
        orig_msg = record.msg

        try:
            # Colourise the level name and left-pad to 8 chars (loguru style)
            color = self._COLORS.get(orig_levelname, self._RESET)
            record.levelname = f"{color}{orig_levelname:<8}{self._RESET}"

            # Dim-magenta module name
            record.name = f"{self._DIM}\033[35m{orig_name}{self._RESET}"

            return super().format(record)
        finally:
            # Restore originals so the plain-text file handler is not polluted
            record.levelname = orig_levelname
            record.name = orig_name
            record.msg = orig_msg


class JsonFormatter(logging.Formatter):
    """JSON log formatter for machine-parsable output.

    Each log record is serialised as a single JSON line with keys:
    ``timestamp``, ``level``, ``logger``, ``message``, and optionally
    ``exception`` and ``extra``.
    """

    def format(self, record: logging.LogRecord) -> str:
        timestamp = self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S")
        payload: dict[str, Any] = {
            "timestamp": timestamp,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Include exception traceback if present
        if record.exc_info and record.exc_info[0]:
            payload["exception"] = "".join(format_exception(*record.exc_info)).rstrip()
        # Include any extra fields attached via logger.info(..., extra={...})
        extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED_ATTRS}
        if extras:
            payload["extra"] = extras
        return _json.dumps(payload, default=str, ensure_ascii=False)


_RESERVED_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
    },
)


# ----------  QueueHandler / QueueListener (async-friendly)  ----------


class _NonBlockingQueueHandler(logging.handlers.QueueHandler):
    """QueueHandler that never blocks the calling thread.

    When the queue is full, the record is serialised and written directly
    to stderr as a last-resort fallback instead of blocking the event loop.
    """

    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except Exception:
            # Queue is full — fall back to stderr rather than blocking.
            try:
                msg = self.format(record)
                sys.stderr.write(msg + "\n")
                sys.stderr.flush()
            except OSError:
                # stderr is closed — nothing we can do safely.  Using the
                # regular logger here would recurse through the same full
                # queue, so we silently discard the record.
                pass


# ----------  Guard against duplicate setup on re-import  ----------

_logger = logging.getLogger()
if not any(isinstance(h, logging.handlers.QueueHandler) for h in _logger.handlers):
    _use_json = LOG_FORMAT == "json"

    _console_formatter: logging.Formatter = JsonFormatter() if _use_json else ColoredConsoleFormatter(TEXT_LOG_FORMAT)
    _console_handler = logging.StreamHandler()
    _console_handler.setLevel(LOG_LEVEL)
    _console_handler.setFormatter(_console_formatter)

    _file_formatter: logging.Formatter = JsonFormatter() if _use_json else logging.Formatter(TEXT_LOG_FORMAT)
    _file_handler = logging.handlers.RotatingFileHandler(
        "bot.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
    )
    _file_handler.setLevel(LOG_LEVEL)
    _file_handler.setFormatter(_file_formatter)

    _real_handlers = [_console_handler, _file_handler]

    _log_queue: Queue = Queue(maxsize=LOG_QUEUE_MAXSIZE)

    _queue_handler = _NonBlockingQueueHandler(_log_queue)
    _queue_handler.setLevel(LOG_LEVEL)

    _listener = logging.handlers.QueueListener(
        _log_queue,
        *_real_handlers,
        respect_handler_level=True,
    )

    # ----------  Wire up root logger  ----------

    _logger.setLevel(LOG_LEVEL)
    # Remove any pre-existing handlers (e.g. if basicConfig was called earlier)
    _logger.handlers.clear()
    _logger.addHandler(_queue_handler)

    # ----------  Start listener  ----------

    _listener.start()
    _logger.getChild(__name__).info("QueueListener started (maxsize=%d, level=%s)", LOG_QUEUE_MAXSIZE, LOG_LEVEL)

    # ----------  Graceful shutdown on exit  ----------

    def _shutdown_listener():
        """Stop the QueueListener so log records are not lost on exit."""
        try:
            _listener.stop()
        except Exception:
            logger = logging.getLogger(__name__)
            logger.warning("QueueListener shutdown encountered an error", exc_info=True)

    atexit.register(_shutdown_listener)

# ----------  Uvicorn loggers  ----------


def configure_uvicorn_loggers():
    """Make uvicorn loggers propagate to the root logger.

    By default uvicorn configures its own loggers (``uvicorn``,
    ``uvicorn.access``, ``uvicorn.error``) with separate handlers and
    format.  This function removes those handlers and sets
    ``propagate = True`` so all uvicorn output flows through the bot's
    QueueHandler and uses the same format, colourisation, and file output.

    Call this before ``uvicorn.run()``.  It is safe to call even if
    uvicorn has not been imported yet.
    """
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uvi_logger = logging.getLogger(name)
        uvi_logger.handlers.clear()
        uvi_logger.propagate = True


# Uvicorn loggers are reconfigured from main.py's startup event, which
# runs after uvicorn.run() has applied its internal logging setup.
# The module-level call is omitted to avoid dead code.
