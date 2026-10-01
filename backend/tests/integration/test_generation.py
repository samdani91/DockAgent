"""Integration coverage for Module 1 — real projects, real builds, real model.

Covers report cases T2-T7.
"""

import re

import pytest

from dockerfile_generation.build import image_size_bytes
from dockerfile_generation.context import build_context
from dockerfile_generation.generate import generate_initial
from dockerfile_generation.localize import localize_error
from dockerfile_generation.loop import run_loop
from dockerfile_generation.patch import error_driven_patches
from dockerfile_generation.select import select_patch

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# T2 — Project context collection
# ---------------------------------------------------------------------------

def test_t2_context_from_a_documented_project(documented_project):
    """Build and run commands in the README reach the context."""
    context = build_context([str(documented_project / "README.md")], documented_project)

    assert "uvicorn main:app" in context.text          # documented run command
    assert "requirements.txt" in context.text          # documented build step
    assert "8000" in context.text                      # documented port
    assert "FastAPI" in context.text                   # detected framework
    assert context.scan_hint                           # facts pinned for repairs


def test_t2_context_from_an_undocumented_project(undocumented_project):
    """No README is a notice, not an error: the scan still yields facts."""
    context = build_context([], undocumented_project)

    assert "Build documentation" not in context.text   # nothing to quote
    assert "node" in context.text.lower()              # scanned anyway
    assert "server.js" in context.text                 # entry point found
    assert "Express.js" in context.text


def test_t2_context_from_the_users_own_project(user_service):
    """A real project that was not written for these tests."""
    context = build_context([], user_service)

    assert "index.js" in context.text
    assert "Express.js" in context.text


# ---------------------------------------------------------------------------
# T3 — Initial Dockerfile generation
# ---------------------------------------------------------------------------

@pytest.mark.needs_llm
@pytest.mark.slow
def test_t3_generated_dockerfile_has_the_required_instructions(
    documented_project, llm
):
    context = build_context([str(documented_project / "README.md")], documented_project)
    dockerfile = generate_initial(context, llm)

    assert re.search(r"^\s*FROM\s+\S+", dockerfile, re.M), "no base image"
    assert re.search(r"^\s*WORKDIR\s+\S+", dockerfile, re.M), "no working directory"
    assert re.search(r"pip install", dockerfile), "no dependency installation"
    assert re.search(r"^\s*(CMD|ENTRYPOINT)\s", dockerfile, re.M), "no entry point"
    assert "main:app" in dockerfile, "entry point does not match the documented one"
    assert "```" not in dockerfile, "markdown fence leaked into the file"


@pytest.mark.needs_llm
@pytest.mark.slow
def test_t3_generated_dockerfile_actually_builds(documented_project, llm, built_image):
    """The only grade that matters for a generated Dockerfile."""
    context = build_context([str(documented_project / "README.md")], documented_project)
    dockerfile = generate_initial(context, llm)

    result = built_image(dockerfile, documented_project)
    assert result.success, f"generated Dockerfile did not build:\n{result.log[-1500:]}"


# ---------------------------------------------------------------------------
# T4 — Build execution and error localization
# ---------------------------------------------------------------------------

_INSTALL_BEFORE_COPY = """\
FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
"""


@pytest.mark.slow
def test_t4_failing_build_is_detected_and_localized(
    documented_project, builder, llm
):
    """Installing before copying must fail, and the error must name the file."""
    result = builder.build(_INSTALL_BEFORE_COPY, str(documented_project))

    assert result.success is False
    assert result.exit_code != 0
    assert result.log, "build output was not captured"

    error = localize_error(result.log, llm)
    assert "requirements.txt" in error, f"error did not identify the cause: {error}"


# ---------------------------------------------------------------------------
# T5 — Iterative patch generation and selection
# ---------------------------------------------------------------------------

@pytest.mark.needs_llm
@pytest.mark.slow
def test_t5_candidates_are_generated_and_one_is_selected(
    documented_project, builder, llm, built_image
):
    context = build_context([str(documented_project / "README.md")], documented_project)
    failed = builder.build(_INSTALL_BEFORE_COPY, str(documented_project))
    error = localize_error(failed.log, llm)

    candidates = error_driven_patches(
        _INSTALL_BEFORE_COPY, error, llm, scan_hint=context.scan_hint
    )
    assert len(candidates) == 3
    # Candidates open with a comment: SYSTEM_GENERATION asks for them.
    assert all(re.search(r"^\s*FROM\s+\S+", c, re.M) for c in candidates)

    chosen = select_patch(candidates, {_INSTALL_BEFORE_COPY}, error, llm)
    assert chosen in candidates

    rebuilt = built_image(chosen, documented_project)
    assert rebuilt.success, f"selected patch still fails:\n{rebuilt.log[-1500:]}"


@pytest.mark.needs_llm
@pytest.mark.slow
def test_t5_repair_loop_fixes_a_broken_dockerfile(
    documented_project, builder, llm
):
    """The whole loop, end to end, on a Dockerfile that genuinely fails."""
    context = build_context([str(documented_project / "README.md")], documented_project)

    result = run_loop(
        initial_dockerfile=_INSTALL_BEFORE_COPY,
        context=context,
        context_dir=str(documented_project),
        builder=builder,
        llm=llm,
        max_attempts=4,
    )

    assert result.success, f"loop could not repair it: {result.last_error}"
    assert result.attempts >= 2, "the first build should have failed"
    assert "COPY" in result.dockerfile


# ---------------------------------------------------------------------------
# T6 — Repair loop termination
# ---------------------------------------------------------------------------

_UNAVAILABLE_BASE = """\
FROM this-image-does-not-exist-dockagent:9.9.9
WORKDIR /app
COPY . .
CMD ["echo", "hi"]
"""


@pytest.mark.needs_llm
@pytest.mark.slow
def test_t6_loop_never_exceeds_the_attempt_limit(documented_project, builder, llm):
    """The bound holds whatever the outcome.

    Note for the report: an unavailable base image is not actually unfixable.
    The model recognises it and substitutes a valid image, so this run usually
    *succeeds*. What must hold either way is that the loop stops at the limit.
    """
    context = build_context([], documented_project)

    result = run_loop(
        initial_dockerfile=_UNAVAILABLE_BASE,
        context=context,
        context_dir=str(documented_project),
        builder=builder,
        llm=llm,
        max_attempts=3,
    )

    assert result.attempts <= 3, "ran past the configured limit"
    if not result.success:
        assert result.last_error, "failed without reporting why"


@pytest.mark.slow
def test_t6_exhausted_loop_reports_the_last_error(documented_project, builder):
    """The give-up path: a real failing build with no room to retry.

    Only the model is substituted, so that the loop is forced down the failure
    branch; the build that decides the outcome is real.
    """
    class _NoHelp:
        def complete(self, system, user):
            return _UNAVAILABLE_BASE          # never improves on the input

    context = build_context([], documented_project)
    result = run_loop(
        initial_dockerfile=_UNAVAILABLE_BASE,
        context=context,
        context_dir=str(documented_project),
        builder=builder,
        llm=_NoHelp(),
        max_attempts=2,
    )

    assert result.success is False
    assert result.attempts <= 2
    assert result.last_error, "no error was reported to the user"


# ---------------------------------------------------------------------------
# T7 — Image optimization
# ---------------------------------------------------------------------------

_FAT_BUT_WORKING = """\
FROM python:3.11
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
"""


@pytest.mark.needs_llm
@pytest.mark.slow
def test_t7_optimized_image_builds_and_is_smaller(documented_project, builder, llm):
    from dockerfile_generation.optimize import optimize

    context = build_context([str(documented_project / "README.md")], documented_project)
    result = optimize(
        dockerfile=_FAT_BUT_WORKING,
        context=context,
        context_dir=str(documented_project),
        builder=builder,
        llm=llm,
    )

    assert result.success, f"optimization rejected: {result.note}"
    assert result.optimized_size < result.original_size
    assert result.reduction_pct > 0

    # And the kept file must still build.
    rebuilt = builder.build(result.dockerfile, str(documented_project))
    assert rebuilt.success


@pytest.mark.slow
def test_t7_a_broken_optimization_falls_back(documented_project, builder):
    """A rewrite that cannot build must leave the working Dockerfile in place."""
    from dockerfile_generation.optimize import optimize

    class _BreaksIt:
        """Stands in for a model that returns something unbuildable.

        Only the model is substituted here — the builds are real, which is what
        the fallback decision depends on.
        """
        def complete(self, system, user):
            return "FROM this-image-does-not-exist-dockagent:9.9.9\nCMD [\"x\"]"

    context = build_context([], documented_project)
    result = optimize(
        dockerfile=_FAT_BUT_WORKING,
        context=context,
        context_dir=str(documented_project),
        builder=builder,
        llm=_BreaksIt(),
        max_repair_attempts=1,
    )

    assert result.success is False
    assert result.dockerfile == _FAT_BUT_WORKING, "did not fall back to the original"
