"""S5: Run the generated Container Structure Test spec against the built image.

Two execution paths, in order of preference:
  1. A `container-structure-test` binary on PATH.
  2. The official runner image, with the Docker socket bind-mounted so the
     containerised runner can reach the daemon that holds the image.

The runner exits non-zero when *tests* fail. That is a normal outcome, not an
execution error — the JSON on stdout is still valid and is parsed as usual.
Only a failure of the runner itself (missing image, unparseable spec, no
Docker, timeout) raises.
"""

import json
import os
import shutil
import subprocess
from typing import Any, Callable

from .data_structures import TestCaseResult, TestRunResult

RUNNER_IMAGE = "gcr.io/gcp-runtimes/container-structure-test:latest"
DOCKER_SOCKET = "/var/run/docker.sock"

# The runner prefixes each result with its test category; strip it so the UI
# shows the name we actually wrote into the spec.
_NAME_PREFIXES = (
    "Command Test: ",
    "File Existence Test: ",
    "File Content Test: ",
    "Metadata Test: ",
    "License Test: ",
)


def execute_tests(
    image_name: str,
    spec_path: str,
    timeout: int = 300,
    progress: Callable[[str, str], None] | None = None,
) -> TestRunResult:
    """Execute *spec_path* against *image_name* and return structured results."""
    emit = progress or (lambda _step, _msg: None)

    spec_path = os.path.abspath(spec_path)
    if not os.path.isfile(spec_path):
        raise RuntimeError(f"Test spec not found: {spec_path}")

    cmd = _build_command(image_name, spec_path, emit)

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Test execution timed out after {timeout} seconds."
        ) from None
    except FileNotFoundError:
        raise RuntimeError(
            "Could not run the test runner: 'docker' executable not found on PATH."
        ) from None

    stdout = proc.stdout.decode(errors="replace").strip()
    stderr = proc.stderr.decode(errors="replace").strip()

    if not stdout:
        raise RuntimeError(
            f"Test runner produced no output (exit {proc.returncode}).\n{stderr}"
        )

    payload = _extract_json(stdout)
    if payload is None:
        raise RuntimeError(
            f"Could not parse test runner output as JSON (exit {proc.returncode}).\n"
            f"stdout:\n{stdout[:1000]}\n\nstderr:\n{stderr[:1000]}"
        )

    return _to_result(payload, stdout)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _build_command(
    image_name: str,
    spec_path: str,
    emit: Callable[[str, str], None],
) -> list[str]:
    binary = shutil.which("container-structure-test")
    if binary:
        emit("S5", "S5 — Using the container-structure-test binary on PATH.")
        return [
            binary, "test",
            "--image", image_name,
            "--config", spec_path,
            "--output", "json",
        ]

    emit("S5", "S5 — No local binary found; using the official runner image.")
    if not os.path.exists(DOCKER_SOCKET):
        raise RuntimeError(
            f"Cannot run the containerised test runner: {DOCKER_SOCKET} is not "
            "available. Install container-structure-test locally, or run where "
            "the Docker socket is reachable."
        )

    spec_dir = os.path.dirname(spec_path)
    spec_name = os.path.basename(spec_path)
    return [
        "docker", "run", "--rm",
        "-v", f"{DOCKER_SOCKET}:{DOCKER_SOCKET}",
        "-v", f"{spec_dir}:/workdir",
        "-w", "/workdir",
        RUNNER_IMAGE,
        "test",
        "--image", image_name,
        "--config", f"/workdir/{spec_name}",
        "--output", "json",
    ]


def _extract_json(stdout: str) -> dict[str, Any] | None:
    """Return the JSON report, tolerating stray lines around it."""
    try:
        payload = json.loads(stdout)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass

    # Fall back to the last line that parses as a JSON object.
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
    return None


def _strip_prefix(name: str) -> str:
    for prefix in _NAME_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _to_result(payload: dict[str, Any], raw_output: str) -> TestRunResult:
    """Map the runner's PascalCase report onto our dataclasses.

    Shape (verified against the runner):
        {"Pass": 2, "Fail": 1, "Total": 3,
         "Results": [{"Name": "...", "Pass": true, "Errors": ["..."]}]}
    `Errors` is absent rather than empty on a passing case.
    """
    cases: list[TestCaseResult] = []
    for item in payload.get("Results") or []:
        if not isinstance(item, dict):
            continue
        cases.append(
            TestCaseResult(
                name=_strip_prefix(str(item.get("Name", "")).strip()),
                passed=bool(item.get("Pass")),
                errors=[str(e) for e in (item.get("Errors") or [])],
            )
        )

    # Prefer the runner's own counts; fall back to counting the cases.
    total = _as_int(payload.get("Total"), len(cases))
    passed = _as_int(payload.get("Pass"), sum(1 for c in cases if c.passed))
    failed = _as_int(payload.get("Fail"), total - passed)

    return TestRunResult(
        total=total,
        passed=passed,
        failed=failed,
        results=cases,
        raw_output=raw_output,
    )


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
