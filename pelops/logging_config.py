"""Structured logging for Pelops.

Two output sinks:
  * stderr in plain text -- readable when you tail logs in a terminal
  * a JSON-lines rotating file (data/logs/pelops.log) -- machine readable,
    rotated at 5 MB, keeps the last 5 generations

Call `configure()` once at process startup (telegram bot, standalone
scheduler, smoke scripts). It is idempotent -- repeated calls do not
duplicate handlers.

The JSON formatter emits one object per line with `timestamp`, `level`,
`logger`, `message`, and any extra fields passed via `logger.info(...,
extra={...})`. Tracebacks land in an `exc_info` string field.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import UTC, datetime
from pathlib import Path

_DEFAULT_LOG_DIR = Path("data/logs")
_DEFAULT_LOG_FILE = "pelops.log"
_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_BACKUP_COUNT = 5

# Standard LogRecord attributes -- everything else on the record is custom
# and worth surfacing in the JSON output.
_STANDARD_LOGRECORD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
    }
)


class JsonFormatter(logging.Formatter):
    """Emit each log record as a single JSON line.

    Custom fields passed via `extra={...}` are merged into the top level
    so dashboards and grep-style tooling can use them directly.
    """

    def format(self, record: logging.LogRecord) -> str:
        out: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            out["exc_info"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in _STANDARD_LOGRECORD_ATTRS or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                out[key] = value
            except (TypeError, ValueError):
                out[key] = repr(value)
        return json.dumps(out, ensure_ascii=False)


def configure(
    level: int | str = logging.INFO,
    log_dir: Path | str | None = None,
    json_to_file: bool = True,
) -> None:
    """Wire up logging. Idempotent.

    Args:
        level: minimum level for both sinks.
        log_dir: where to write the rotating JSON file. Defaults to
            `data/logs/` relative to the cwd. Pass `None` and
            `json_to_file=False` to disable the file sink entirely.
        json_to_file: when False, only the stderr handler is installed.
            Use for CI, tests, and short-lived smoke scripts.
    """
    root = logging.getLogger()
    if getattr(root, "_pelops_configured", False):
        return
    if isinstance(level, str):
        level = logging.getLevelName(level.upper())
    root.setLevel(level)

    # Console (plain) -- the one you read live.
    plain = logging.StreamHandler(stream=sys.stderr)
    plain.setLevel(level)
    plain.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s -- %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(plain)

    # Rotating JSONL file -- the one you grep later.
    if json_to_file:
        directory = Path(log_dir) if log_dir else _DEFAULT_LOG_DIR
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / _DEFAULT_LOG_FILE
        rotating = logging.handlers.RotatingFileHandler(
            target,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        rotating.setLevel(level)
        rotating.setFormatter(JsonFormatter())
        root.addHandler(rotating)

    # Tame the third-party loggers that spam us at INFO.
    for noisy in ("httpx", "apscheduler.executors.default", "aiogram.dispatcher"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    root._pelops_configured = True  # type: ignore[attr-defined]
