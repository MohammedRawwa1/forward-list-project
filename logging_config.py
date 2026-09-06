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

load_dotenv()

LOG_FORMAT = os.getenv("LOG_FORMAT", "text").lower()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_QUEUE_MAXSIZE = int(os.getenv("LOG_QUEUE_MAXSIZE", "10000"))

TEXT_LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s - %(message)s"


class ColoredConsoleFormatter(logging.Formatter):

    _COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[1;31m",
    }
    _RESET = "\033[0m"
    _DIM = "\033[2m"

    def format(self, record):
        orig_levelname = record.levelname
        orig_name = record.name
        orig_msg = record.msg

        try:
            color = self._COLORS.get(orig_levelname, self._RESET)
            record.levelname = f"{color}{orig_levelname:<8}{self._RESET}"

            record.name = f"{self._DIM}\033[35m{orig_name}{self._RESET}"

            return super().format(record)
        finally:
            record.levelname = orig_levelname
            record.name = orig_name
            record.msg = orig_msg


class JsonFormatter(logging.Formatter):

    def format(self, record: logging.LogRecord) -> str:
        timestamp = self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S")
        payload: dict[str, Any] = {
            "timestamp": timestamp,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            payload["exception"] = "".join(format_exception(*record.exc_info)).rstrip()
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

    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except Exception:
            try:
                msg = self.format(record)
                sys.stderr.write(msg + "\n")
                sys.stderr.flush()
            except OSError:
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
        maxBytes=10 * 1024 * 1024,
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
    _logger.handlers.clear()
    _logger.addHandler(_queue_handler)

    # ----------  Start listener  ----------

    _listener.start()
    _logger.getChild(__name__).info("QueueListener started (maxsize=%d, level=%s)", LOG_QUEUE_MAXSIZE, LOG_LEVEL)

    # ----------  Graceful shutdown on exit  ----------

    def _shutdown_listener():
        try:
            _listener.stop()
        except Exception:
            logger = logging.getLogger(__name__)
            logger.warning("QueueListener shutdown encountered an error", exc_info=True)

    atexit.register(_shutdown_listener)

# ----------  Uvicorn loggers  ----------


def configure_uvicorn_loggers():
    # httpx logs full request URLs at INFO; for the Telegram Bot API these
    # contain the bot token (https://api.telegram.org/bot<token>/...), which
    # has already leaked into logs twice. Silence the logger and rely on
    # application-level logs instead.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uvi_logger = logging.getLogger(name)
        uvi_logger.handlers.clear()
        uvi_logger.propagate = True
