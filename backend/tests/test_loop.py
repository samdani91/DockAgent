"""Integration test for the repair loop using FakeDockerBuilder + FakeLLM."""

import pytest

from dockerfile_generation.build import BuildResult, FakeDockerBuilder
from dockerfile_generation.context import ProjectContext
from dockerfile_generation.loop import MAX_ATTEMPTS, run_loop

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COPY_BEFORE_INSTALL = (
    "FROM python:3.11\n"
    "WORKDIR /app\n"
    "COPY requirements.txt .\n"
    "RUN pip install -r requirements.txt\n"
    "COPY . .\n"
    'CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]'
)

_BROKEN_DOCKERFILE = (
    "FROM python:3.11\n"
    "WORKDIR /app\n"
    "RUN pip install -r requirements.txt\n"   # missing COPY — will fail
    "COPY . .\n"
    'CMD ["uvicorn", "main:app"]'
)

_MISSING_REQ_ERROR = (
    "Step 3/4 : RUN pip install -r requirements.txt\n"
    "ERROR: Could not open requirements file: [Errno 2] No such file or directory: "
    "'requirements.txt'"
)


class _FakeLLM:
    """Always returns the copy-before-install Dockerfile as a repair."""

    def complete(self, system: str, user: str) -> str:
        return _COPY_BEFORE_INSTALL


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_loop_fail_once_then_succeed():
    """Main acceptance test: fail on first build, succeed on second.

    Asserts:
    - loop terminates with success=True
    - exactly 2 build attempts
    - final Dockerfile contains the COPY-before-install fix
    """
    builder = FakeDockerBuilder([
        BuildResult(success=False, exit_code=1, log=_MISSING_REQ_ERROR),
        BuildResult(success=True, exit_code=0, log="Successfully built abc123\n"),
    ])
    context = ProjectContext(text="FastAPI app, Python 3.11, requirements.txt.")

    result = run_loop(
        initial_dockerfile=_BROKEN_DOCKERFILE,
        context=context,
        context_dir="/tmp/fake_repo",
        builder=builder,
        llm=_FakeLLM(),
        max_attempts=6,
    )

    assert result.success is True
    assert result.attempts == 2
    assert "COPY requirements.txt" in result.dockerfile
    assert result.last_error is None


def test_loop_succeeds_on_first_attempt():
    """If the initial Dockerfile builds cleanly the loop exits after 1 attempt."""
    builder = FakeDockerBuilder([
        BuildResult(success=True, exit_code=0, log="Successfully built"),
    ])
    context = ProjectContext(text="")

    result = run_loop(
        initial_dockerfile="FROM python:3.11",
        context=context,
        context_dir="/tmp",
        builder=builder,
        llm=_FakeLLM(),
    )

    assert result.success is True
    assert result.attempts == 1
    assert result.dockerfile == "FROM python:3.11"


def test_loop_exhausts_max_attempts():
    """If every build fails the loop returns success=False after max_attempts."""
    always_fail = BuildResult(
        success=False, exit_code=1, log="ERROR: something is broken"
    )
    builder = FakeDockerBuilder([always_fail])
    context = ProjectContext(text="")

    result = run_loop(
        initial_dockerfile="FROM scratch",
        context=context,
        context_dir="/tmp",
        builder=builder,
        llm=_FakeLLM(),
        max_attempts=3,
    )

    # The no-progress guard (same error 3 times) fires at attempt 3.
    assert result.success is False
    assert result.last_error is not None


def test_loop_no_progress_guard_stops_early():
    """Same error three times triggers the no-progress guard before max_attempts."""
    same_error_log = "ERROR: missing dependency xyz"
    always_fail = BuildResult(success=False, exit_code=1, log=same_error_log)
    builder = FakeDockerBuilder([always_fail])
    context = ProjectContext(text="")

    result = run_loop(
        initial_dockerfile="FROM scratch",
        context=context,
        context_dir="/tmp",
        builder=builder,
        llm=_FakeLLM(),
        max_attempts=MAX_ATTEMPTS,
    )

    assert result.success is False
    # Should stop at attempt 3 (no-progress threshold), not at MAX_ATTEMPTS.
    assert result.attempts < MAX_ATTEMPTS


def test_fake_builder_records_calls():
    """FakeDockerBuilder.calls stores every (dockerfile, context_dir) pair."""
    builder = FakeDockerBuilder([
        BuildResult(success=False, exit_code=1, log="ERROR: oops"),
        BuildResult(success=True, exit_code=0, log="OK"),
    ])
    context = ProjectContext(text="")

    run_loop(
        initial_dockerfile="FROM python:3.11",
        context=context,
        context_dir="/repo",
        builder=builder,
        llm=_FakeLLM(),
    )

    assert len(builder.calls) == 2
    assert builder.calls[0][1] == "/repo"
    assert builder.calls[1][1] == "/repo"
