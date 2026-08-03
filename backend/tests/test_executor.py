"""Unit tests for executor.py — subprocess is mocked, no real Docker is invoked.

The JSON fixtures below match the runner's real output, which uses PascalCase
keys and omits `Errors` entirely on a passing case.
"""

import json
import subprocess
from unittest import mock

import pytest

from test_generation.executor import RUNNER_IMAGE, execute_tests

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ALL_PASS = {
    "Pass": 2, "Fail": 0, "Total": 2,
    "Results": [
        {"Name": "Command Test: check node", "Pass": True, "Stdout": "/usr/bin/node\n"},
        {"Name": "File Existence Test: /app/index.js exists", "Pass": True},
    ],
}

_SOME_FAIL = {
    "Pass": 1, "Fail": 1, "Total": 2,
    "Results": [
        {"Name": "Command Test: check node", "Pass": True, "Stdout": "/usr/bin/node\n"},
        {
            "Name": "Command Test: check missing",
            "Pass": False,
            "Errors": [
                "Expected string '/nope' not found in output ''",
                "exited with incorrect error code. Expected: 0, Actual: 1",
            ],
        },
    ],
}


@pytest.fixture
def spec(tmp_path):
    path = tmp_path / "container-structure-test.yaml"
    path.write_text("schemaVersion: '2.0.0'\n")
    return str(path)


def _completed(payload, returncode=0, stderr=b""):
    out = json.dumps(payload).encode() if isinstance(payload, dict) else payload
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=out, stderr=stderr)


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------

def test_all_tests_pass(spec):
    with mock.patch("shutil.which", return_value="/usr/bin/container-structure-test"), \
         mock.patch("subprocess.run", return_value=_completed(_ALL_PASS)):
        result = execute_tests("img", spec)

    assert result.total == 2
    assert result.passed == 2
    assert result.failed == 0
    assert all(c.passed for c in result.results)
    assert all(c.errors == [] for c in result.results)


def test_some_tests_fail_is_not_an_execution_error(spec):
    """The runner exits 1 when tests fail — that must still parse normally."""
    with mock.patch("shutil.which", return_value="/usr/bin/container-structure-test"), \
         mock.patch("subprocess.run",
                    return_value=_completed(_SOME_FAIL, returncode=1,
                                            stderr=b'level=fatal msg=FAIL')):
        result = execute_tests("img", spec)

    assert result.total == 2
    assert result.passed == 1
    assert result.failed == 1

    failing = [c for c in result.results if not c.passed]
    assert len(failing) == 1
    assert len(failing[0].errors) == 2


def test_category_prefix_is_stripped_from_names(spec):
    with mock.patch("shutil.which", return_value="/usr/bin/cst"), \
         mock.patch("subprocess.run", return_value=_completed(_ALL_PASS)):
        result = execute_tests("img", spec)

    names = [c.name for c in result.results]
    assert names == ["check node", "/app/index.js exists"]


def test_raw_output_is_preserved(spec):
    with mock.patch("shutil.which", return_value="/usr/bin/cst"), \
         mock.patch("subprocess.run", return_value=_completed(_ALL_PASS)):
        result = execute_tests("img", spec)
    assert '"Total":2' in result.raw_output.replace(" ", "")


def test_counts_fall_back_to_case_tally_when_absent(spec):
    payload = {"Results": [{"Name": "a", "Pass": True}, {"Name": "b", "Pass": False}]}
    with mock.patch("shutil.which", return_value="/usr/bin/cst"), \
         mock.patch("subprocess.run", return_value=_completed(payload, returncode=1)):
        result = execute_tests("img", spec)

    assert result.total == 2
    assert result.passed == 1
    assert result.failed == 1


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------

def test_malformed_output_raises(spec):
    with mock.patch("shutil.which", return_value="/usr/bin/cst"), \
         mock.patch("subprocess.run",
                    return_value=_completed(b"not json at all", returncode=1)):
        with pytest.raises(RuntimeError, match="Could not parse"):
            execute_tests("img", spec)


def test_empty_output_raises(spec):
    with mock.patch("shutil.which", return_value="/usr/bin/cst"), \
         mock.patch("subprocess.run", return_value=_completed(b"", returncode=1)):
        with pytest.raises(RuntimeError, match="no output"):
            execute_tests("img", spec)


def test_timeout_raises(spec):
    with mock.patch("shutil.which", return_value="/usr/bin/cst"), \
         mock.patch("subprocess.run",
                    side_effect=subprocess.TimeoutExpired(cmd="cst", timeout=300)):
        with pytest.raises(RuntimeError, match="timed out after 300"):
            execute_tests("img", spec, timeout=300)


def test_missing_docker_binary_raises(spec):
    with mock.patch("shutil.which", return_value=None), \
         mock.patch("os.path.exists", return_value=True), \
         mock.patch("subprocess.run", side_effect=FileNotFoundError()):
        with pytest.raises(RuntimeError, match="docker.*not found"):
            execute_tests("img", spec)


def test_missing_spec_raises(tmp_path):
    with pytest.raises(RuntimeError, match="Test spec not found"):
        execute_tests("img", str(tmp_path / "nope.yaml"))


def test_missing_docker_socket_raises(spec):
    """No local binary and no socket to mount — cannot run at all."""
    with mock.patch("shutil.which", return_value=None), \
         mock.patch("os.path.exists", return_value=False):
        with pytest.raises(RuntimeError, match="not\\s+available"):
            execute_tests("img", spec)


# ---------------------------------------------------------------------------
# Execution path selection
# ---------------------------------------------------------------------------

def test_uses_local_binary_when_on_path(spec):
    with mock.patch("shutil.which", return_value="/usr/local/bin/container-structure-test"), \
         mock.patch("subprocess.run", return_value=_completed(_ALL_PASS)) as run:
        execute_tests("my-image", spec)

    cmd = run.call_args[0][0]
    assert cmd[0] == "/usr/local/bin/container-structure-test"
    assert "docker" not in cmd
    assert cmd[cmd.index("--image") + 1] == "my-image"
    assert cmd[cmd.index("--config") + 1] == spec
    assert cmd[cmd.index("--output") + 1] == "json"


def test_falls_back_to_runner_image(spec):
    with mock.patch("shutil.which", return_value=None), \
         mock.patch("os.path.exists", return_value=True), \
         mock.patch("subprocess.run", return_value=_completed(_ALL_PASS)) as run:
        execute_tests("my-image", spec)

    cmd = run.call_args[0][0]
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert RUNNER_IMAGE in cmd
    # The spec directory is mounted and referenced from inside the container.
    assert any(arg.endswith(":/workdir") for arg in cmd)
    assert cmd[cmd.index("--config") + 1] == "/workdir/container-structure-test.yaml"


def test_progress_reports_which_path_was_taken(spec):
    seen: list[tuple[str, str]] = []
    with mock.patch("shutil.which", return_value="/usr/bin/cst"), \
         mock.patch("subprocess.run", return_value=_completed(_ALL_PASS)):
        execute_tests("img", spec, progress=lambda s, m: seen.append((s, m)))

    assert any("binary on PATH" in m for _, m in seen)


def test_timeout_is_passed_to_subprocess(spec):
    with mock.patch("shutil.which", return_value="/usr/bin/cst"), \
         mock.patch("subprocess.run", return_value=_completed(_ALL_PASS)) as run:
        execute_tests("img", spec, timeout=42)
    assert run.call_args.kwargs["timeout"] == 42
