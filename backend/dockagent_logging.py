"""Console logging setup for the DockAgent backend.

Only the application configures handlers. The pipeline packages just call
`logging.getLogger("dockagent.<module>")` and log — no imports from here — so
they stay independently installable.

Output is one line per step, tagged with the module it came from:

    14:23:01 INFO  generate   Build attempt 1/6
    14:23:01 DEBUG build      docker build --no-cache -f /tmp/x.Dockerfile .
    14:25:14 INFO  build      build failed in 2m13s (exit 1)
    14:25:14 INFO  llm        gemini-flash-latest replied 1,240 chars in 8.3s

Set DOCKAGENT_LOG_LEVEL=DEBUG for command lines, prompt sizes and raw output.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = "dockagent"

#: Where the rolling log lives unless DOCKAGENT_LOG_FILE says otherwise.
DEFAULT_LOG_FILE = Path(__file__).parent / "logs" / "dockagent.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5

_RESET = "\033[0m"
_DIM = "\033[2m"
_LEVEL_STYLE = {
    "DEBUG": "\033[36m",      # cyan
    "INFO": "\033[32m",       # green
    "WARNING": "\033[33m",    # yellow
    "ERROR": "\033[31m",      # red
    "CRITICAL": "\033[1;31m",
}


class ConsoleFormatter(logging.Formatter):
    """time · level · module · message, with the module column aligned."""

    def __init__(self, colour: bool = True) -> None:
        super().__init__(datefmt="%H:%M:%S")
        self._colour = colour

    def format(self, record: logging.LogRecord) -> str:
        timestamp = self.formatTime(record, self.datefmt)
        module = record.name[len(ROOT) + 1:] if record.name.startswith(ROOT + ".") else record.name
        message = record.getMessage()

        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)

        if not self._colour:
            return f"{timestamp} {record.levelname:<7} {module:<10} {message}"

        style = _LEVEL_STYLE.get(record.levelname, "")
        return (
            f"{_DIM}{timestamp}{_RESET} "
            f"{style}{record.levelname:<7}{_RESET} "
            f"{_DIM}{module:<10}{_RESET} "
            f"{message}"
        )


def configure(
    level: str | int | None = None,
    stream=None,
    log_file: str | os.PathLike | None = None,
    file_level: str | int | None = None,
) -> logging.Logger:
    """Install the console and rolling-file handlers. Safe to call repeatedly.

    The console stays readable at INFO while the file keeps DEBUG detail — a
    build that failed an hour ago is exactly when you want the command line and
    log tail that the terminal omitted.

    Set DOCKAGENT_LOG_FILE=off to disable file logging.
    """
    stream = stream or sys.stderr
    console_level = _resolve_level(level or os.environ.get("DOCKAGENT_LOG_LEVEL"), logging.INFO)
    disk_level = _resolve_level(
        file_level or os.environ.get("DOCKAGENT_FILE_LOG_LEVEL"), logging.DEBUG
    )

    root = logging.getLogger(ROOT)
    root.propagate = False          # don't double-print through the root logger

    for handler in list(root.handlers):
        handler.close()
        root.removeHandler(handler)

    console = logging.StreamHandler(stream)
    console.setLevel(console_level)
    console.setFormatter(ConsoleFormatter(colour=_supports_colour(stream)))
    root.addHandler(console)

    path = _resolve_log_path(log_file)
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            rolling = logging.handlers.RotatingFileHandler(
                path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
            )
            rolling.setLevel(disk_level)
            rolling.setFormatter(FileFormatter())
            root.addHandler(rolling)
        except OSError as exc:
            # Logging must never take the application down with it.
            console.handle(logging.LogRecord(
                f"{ROOT}.logging", logging.WARNING, __file__, 0,
                "file logging disabled (%s): %s", (path, exc), None,
            ))
            disk_level = console_level

    # The root logger must admit whatever the most verbose handler wants.
    root.setLevel(min(console_level, disk_level))

    # uvicorn is noisy at INFO and duplicates request lines we already log.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    return root


def log_file_path() -> Path | None:
    """The active log file, or None when file logging is off."""
    for handler in logging.getLogger(ROOT).handlers:
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            return Path(handler.baseFilename)
    return None


class FileFormatter(logging.Formatter):
    """Plain, dated, never coloured — the file outlives the terminal session."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )


def _resolve_level(value: str | int | None, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    return getattr(logging, str(value).upper(), default)


def _resolve_log_path(explicit: str | os.PathLike | None) -> Path | None:
    if explicit is not None:
        return Path(explicit)
    configured = os.environ.get("DOCKAGENT_LOG_FILE")
    if configured is None:
        return DEFAULT_LOG_FILE
    if configured.strip().lower() in ("", "off", "none", "0", "false"):
        return None
    return Path(configured).expanduser()


def get_logger(module: str) -> logging.Logger:
    """A logger for one pipeline module, e.g. get_logger("generate")."""
    return logging.getLogger(f"{ROOT}.{module}")


@contextmanager
def timed(logger: logging.Logger, message: str, level: int = logging.INFO):
    """Log *message* with how long the block took.

    Failures are logged with their elapsed time too, then re-raised — the
    duration of a step that timed out is usually the interesting part.
    """
    started = time.monotonic()
    try:
        yield
    except BaseException:
        logger.log(level, "%s failed after %s", message, format_duration(time.monotonic() - started))
        raise
    logger.log(level, "%s in %s", message, format_duration(time.monotonic() - started))


def format_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _supports_colour(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return bool(getattr(stream, "isatty", lambda: False)())
