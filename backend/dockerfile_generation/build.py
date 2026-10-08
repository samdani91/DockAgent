"""DockerBuilder protocol, real subprocess implementation, and fake for tests."""

import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

log = logging.getLogger("dockagent.build")


@dataclass
class BuildResult:
    success: bool
    exit_code: int
    log: str
    image_id: str | None = None  # set on success; needed to measure image size


@runtime_checkable
class DockerBuilder(Protocol):
    def build(self, dockerfile_text: str, context_dir: str) -> BuildResult:
        """Build the image described by *dockerfile_text* with *context_dir* as
        the Docker build context. Returns a BuildResult regardless of outcome."""
        ...


class RealDockerBuilder:
    """Shells out to `docker build`.

    The Dockerfile is written to a system temp file so it does not pollute or
    accidentally appear inside the build context.
    """

    def __init__(self, timeout: int = 1800, no_cache: bool = False) -> None:
        self._timeout = timeout
        self._no_cache = no_cache

    def build(self, dockerfile_text: str, context_dir: str) -> BuildResult:
        fd, dockerfile_path = tempfile.mkstemp(suffix=".Dockerfile")
        iid_fd, iid_path = tempfile.mkstemp(suffix=".iid")
        os.close(iid_fd)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(dockerfile_text)

            cmd = [
                "docker", "build", "--progress=plain",
                "--iidfile", iid_path,
                "-f", dockerfile_path, ".",
            ]
            if self._no_cache:
                cmd.insert(2, "--no-cache")

            log.info(
                "docker build%s (context %s)",
                " --no-cache" if self._no_cache else "",
                context_dir,
            )
            log.debug("%s", " ".join(cmd))
            started = time.monotonic()

            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                cwd=context_dir,
            )
            elapsed = time.monotonic() - started
            # docker build writes everything to stderr with --progress=plain
            build_log = (proc.stdout + proc.stderr).strip()
            image_id = _read_iid(iid_path) if proc.returncode == 0 else None

            if proc.returncode == 0:
                log.info("build succeeded in %s (%s)",
                         _duration(elapsed), (image_id or "no image id")[:19])
            else:
                log.warning("build failed in %s (exit %d)", _duration(elapsed), proc.returncode)
                log.debug("build log tail:\n%s", "\n".join(build_log.splitlines()[-20:]))

            return BuildResult(
                success=proc.returncode == 0,
                exit_code=proc.returncode,
                log=build_log,
                image_id=image_id,
            )
        except subprocess.TimeoutExpired:
            log.error("build timed out after %ds", self._timeout)
            return BuildResult(
                success=False,
                exit_code=-1,
                log=f"Build timed out after {self._timeout} seconds.",
            )
        except FileNotFoundError:
            log.error("docker executable not found on PATH")
            return BuildResult(
                success=False,
                exit_code=-1,
                log="docker executable not found. Is Docker installed and on PATH?",
            )
        finally:
            for path in (dockerfile_path, iid_path):
                try:
                    os.unlink(path)
                except OSError:
                    pass


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s"


def _read_iid(path: str) -> str | None:
    """Read the image ID docker wrote via --iidfile."""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def image_size_bytes(image_id: str, timeout: int = 30) -> int | None:
    """Return the on-disk size of *image_id* in bytes, or None if unavailable."""
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", "-f", "{{.Size}}", image_id],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


class FakeDockerBuilder:
    """Returns preset BuildResult objects in sequence — for unit tests only.

    Once the preset list is exhausted the last result is repeated.
    """

    def __init__(self, responses: list[BuildResult]) -> None:
        if not responses:
            raise ValueError("FakeDockerBuilder needs at least one response.")
        self._responses = list(responses)
        self.call_count = 0
        self.calls: list[tuple[str, str]] = []  # (dockerfile_text, context_dir)

    def build(self, dockerfile_text: str, context_dir: str) -> BuildResult:
        self.calls.append((dockerfile_text, context_dir))
        result = self._responses[min(self.call_count, len(self._responses) - 1)]
        self.call_count += 1
        return result
