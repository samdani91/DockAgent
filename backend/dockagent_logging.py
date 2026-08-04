"""Console logging setup for the DockAgent backend.

Only the application configures handlers. The pipeline packages just call
`logging.getLogger("dockagent.<module>")` and log — no imports from here — so
they stay independently installable.

Output is one line per step, tagged with the module it came from:

    14:23:01 INFO  generate   Build attempt 1/6
    14:23:01 DEBUG build      docker build --no-cache -f /tmp/x.Dockerfile .
    14:25:14 INFO  build      build failed in 2m13s (exit 1)
    14:25:14 INFO  llm        gemini-2.5-pro replied 1,240 chars in 8.3s

Set DOCKAGENT_LOG_LEVEL=DEBUG for command lines, prompt sizes and raw output.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import contextmanager

ROOT = "dockagent"

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


def configure(level: str | int | None = None, stream=None) -> logging.Logger:
    """Install the console handler. Safe to call more than once."""
    stream = stream or sys.stderr
    resolved = level or os.environ.get("DOCKAGENT_LOG_LEVEL", "INFO")
    if isinstance(resolved, str):
        resolved = getattr(logging, resolved.upper(), logging.INFO)

    root = logging.getLogger(ROOT)
    root.setLevel(resolved)
    root.propagate = False          # don't double-print through the root logger

    for handler in list(root.handlers):
        root.removeHandler(handler)

    colour = _supports_colour(stream)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(ConsoleFormatter(colour=colour))
    root.addHandler(handler)

    # uvicorn is noisy at INFO and duplicates request lines we already log.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    return root


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
