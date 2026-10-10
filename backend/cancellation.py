"""Cooperative cancellation for long-running pipeline work."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Callable


class RunCancelled(Exception):
    """The client stopped the current run."""


def check_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise RunCancelled("Run stopped by the user.")


def run_process(
    command: list[str],
    *,
    cancelled: Callable[[], bool] | None = None,
    **kwargs,
) -> subprocess.CompletedProcess:
    """Run a command, terminating its process group when cancellation arrives."""
    check_cancelled(cancelled)
    if cancelled is None:
        return subprocess.run(command, **kwargs)

    timeout = kwargs.pop("timeout", None)
    deadline = time.monotonic() + timeout if timeout is not None else None
    capture_output = kwargs.pop("capture_output", False)
    if capture_output:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE

    process = subprocess.Popen(
        command, start_new_session=os.name != "nt", **kwargs
    )
    try:
        while True:
            check_cancelled(cancelled)
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                check_cancelled(cancelled)
                return subprocess.CompletedProcess(
                    command, process.returncode, stdout, stderr
                )
            except subprocess.TimeoutExpired:
                if deadline is not None and time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, timeout)
    except BaseException:
        if process.poll() is None:
            if os.name == "nt":
                process.terminate()
            else:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    process.kill()
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.communicate()
        raise
