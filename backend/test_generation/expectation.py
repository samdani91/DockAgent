"""S4: Acquire expectations by running version commands in a container."""

import posixpath
import re
import logging
import subprocess
from typing import Callable, Optional

import docker
import docker.errors

from .viewpoint import _create_container, _stop_remove

log = logging.getLogger("dockagent.test")


_VERSION_OPTIONS = ("--version", "-version", "-V")
_VERSION_PATTERN = re.compile(r"\bv?\d+\.\d+(\.\d+)?(\(\d+\)?-\w+)?\b")
_COMMAND_TIMEOUT = 5   # seconds per exec — subprocess kills it hard on timeout

# Commands whose --version either loops forever (busybox yes/cat/echo)
# or is pointless to capture.
_SKIP_VERSION_CHECK: set[str] = {
    "yes", "true", "false", "echo", "printf", "[", "test",
    "sh", "bash", "dash", "ash", "nologin", "sync", "sleep",
    "cat", "ls", "cp", "mv", "rm", "rmdir", "mkdir", "touch",
    "ln", "chmod", "chown", "chgrp", "kill", "env", "pwd",
    "cd", "read", "expr", "head", "tail", "tee", "wc",
}


class ExpectationAcquirer:
    def get_versions(
        self,
        commands: list[str],
        image_name: str,
        progress: Callable[[str, str], None],
    ) -> dict[str, tuple[str, tuple]]:
        versioned: dict[str, tuple[str, tuple]] = {}
        total = len(commands)

        client = docker.from_env()
        container = _create_container(client, image_name)
        container_id = container.id
        try:
            # One line per command is detail, not news: a real image produces
            # hundreds. They go to the log file at debug level, while the
            # console gets periodic progress instead.
            # Only worth reporting progress through when there is enough of it:
            # below this the opening and closing lines already say everything.
            step = total // 10 if total >= 20 else 0

            for i, command in enumerate(commands):
                if step and i and i % step == 0:
                    progress("S4", f"S4 — {i}/{total} commands checked…")

                if command in _SKIP_VERSION_CHECK:
                    log.debug("skipping %s (%d/%d) — known infinite-output command",
                              command, i + 1, total)
                    continue

                log.debug("checking %s version (%d/%d)", command, i + 1, total)

                for option in _VERSION_OPTIONS:
                    result = _run_subprocess(container_id, [command, option])
                    if result is None:
                        continue   # timed out
                    exit_code, stdout, stderr = result
                    version = _version_search(stdout, stderr, option)
                    if exit_code == 0 and (version[0] or version[1]):
                        versioned[command] = (option, version)
                        break
        finally:
            _stop_remove(container)

        return versioned


def _run_subprocess(container_id: str, cmd: list[str]) -> Optional[tuple]:
    """Run a command inside a container via subprocess docker exec (hard-kills on timeout)."""
    try:
        proc = subprocess.run(
            ["docker", "exec", container_id] + cmd,
            capture_output=True,
            timeout=_COMMAND_TIMEOUT,
            text=True,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        log.debug("version probe timed out: %s", " ".join(cmd))
        return None
    except OSError as exc:
        # A failed probe is not a timeout; the caller logged it as one.
        log.warning("version probe could not run (%s): %s", type(exc).__name__, exc)
        return None


def _version_search(stdout: str, stderr: str, option: str) -> tuple:
    if stdout.rstrip("\r\n") == option:
        stdout_version = None
    else:
        m = _VERSION_PATTERN.search(stdout)
        if m:
            stdout_version = m.group(0)
        else:
            lines = stdout.splitlines()
            stdout_version = next(
                (ln for ln in lines if re.search(r"[Vv]ersion", ln)), None
            )

    m2 = _VERSION_PATTERN.search(stderr)
    stderr_version = m2.group(0) if m2 else None
    return (stdout_version, stderr_version)


def mask_patch_version(version: str) -> str:
    m = _VERSION_PATTERN.search(version)
    if m:
        dots = [i for i, c in enumerate(version) if c == "."]
        masked = re.escape(version[: dots[1] + 1]) + ".*" if len(dots) >= 2 else re.escape(version)
    else:
        masked = re.escape(version)
    return masked.replace("'", "''")
