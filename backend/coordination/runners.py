"""Real implementations of the four operations the orchestrator invokes.

Each one calls the same functions the individual endpoints call — no HTTP, no
duplicated pipeline logic — and maps the result onto the agent's outcome types.
"""

from __future__ import annotations

import logging
import os
from dataclasses import replace
from pathlib import Path

from .state import (
    FailingTest,
    FlakinessOutcome,
    GenerationOutcome,
    PipelineState,
    TestOutcome,
)

log = logging.getLogger("dockagent.agent")


def make_llm(request):
    """The shared LLM client, or None when no key is configured."""
    from dockerfile_generation.llm import GeminiClient, OpenAIClient

    model = request.model or os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
    if request.provider.lower() == "openai":
        if not os.environ.get("OPENAI_API_KEY", "").strip():
            return None
        return OpenAIClient(model=model)
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        return None
    return GeminiClient(model=model)


def _require_llm(request):
    llm = make_llm(request)
    if llm is None:
        provider = request.provider.upper()
        raise ValueError(
            f"{provider}_API_KEY is not set. Add it to backend/.env and restart."
        )
    return llm


def _docs_for(workspace: Path) -> list[str]:
    candidates = [
        "README.md", "README.rst", "README.txt", "README",
        "INSTALL.md", "INSTALL.rst", "INSTALL", "BUILDING.md",
    ]
    found: list[str] = []
    for name in candidates:
        path = workspace / name
        if path.exists() and len(found) < 2:
            found.append(str(path))
    return found


# ---------------------------------------------------------------------------
# Module 1 — generation
# ---------------------------------------------------------------------------

def run_generation(request, state: PipelineState, emit) -> GenerationOutcome:
    from dockerfile_generation.build import RealDockerBuilder
    from dockerfile_generation.context import build_context
    from dockerfile_generation.generate import generate_initial
    from dockerfile_generation.loop import run_loop

    workspace = Path(request.workspace_path)
    dockerfile_path = Path(state.dockerfile_path)

    def progress(step: str, message: str) -> None:
        emit("generate", step, message)

    llm = _require_llm(request)
    builder = RealDockerBuilder(timeout=request.build_timeout)

    progress("context", "Reading project files…")
    context = build_context(_docs_for(workspace), workspace)

    progress("generating", "Asking the model for an initial Dockerfile…")
    initial = generate_initial(context, llm)

    progress("building", f"Building (max {request.max_attempts} attempts)…")
    result = run_loop(
        initial_dockerfile=initial,
        context=context,
        context_dir=str(workspace),
        builder=builder,
        llm=llm,
        max_attempts=request.max_attempts,
        on_attempt=lambda _n, msg: progress("building", msg),
    )

    if not result.success:
        return GenerationOutcome(
            success=False,
            attempts=result.attempts,
            message=(
                f"Could not produce a Dockerfile that builds after "
                f"{result.attempts} attempt(s)."
                + (f" Last error: {result.last_error}" if result.last_error else "")
            ),
        )

    final = result.dockerfile
    optimized = False
    reduction = None

    if request.optimize:
        from dockerfile_generation.optimize import optimize

        progress("optimizing", "Optimizing image size (multi-stage)…")
        opt = optimize(
            dockerfile=final,
            context=context,
            context_dir=str(workspace),
            builder=builder,
            llm=llm,
            on_progress=lambda m: progress("optimizing", m),
        )
        if opt.success:
            final = opt.dockerfile
            optimized = True
            reduction = opt.reduction_pct

    dockerfile_path.write_text(final, encoding="utf-8")
    message = f"Dockerfile generated in {result.attempts} attempt(s)."
    if optimized and reduction is not None:
        message += f" Optimized: {reduction:.1f}% smaller."

    return GenerationOutcome(
        success=True,
        dockerfile_path=str(dockerfile_path),
        attempts=result.attempts,
        message=message,
        optimized=optimized,
        size_reduction_pct=reduction,
    )


# ---------------------------------------------------------------------------
# Feedback A — patch the Dockerfile from failing container tests
# ---------------------------------------------------------------------------

def apply_test_feedback(
    request, state: PipelineState, instruction: str, emit
) -> GenerationOutcome:
    """Repair a *building* Dockerfile that failed its container tests.

    run_loop cannot be reused here: it only calls the model when a build fails,
    and this Dockerfile builds. So the repair machinery is invoked one level
    down, with the failing tests as the error and the instruction carried on the
    existing scan_hint channel.
    """
    from dockerfile_generation.build import RealDockerBuilder
    from dockerfile_generation.context import build_context
    from dockerfile_generation.patch import error_driven_patches
    from dockerfile_generation.select import select_patch

    workspace = Path(request.workspace_path)
    dockerfile_path = Path(state.dockerfile_path)
    current = dockerfile_path.read_text(encoding="utf-8")

    def progress(step: str, message: str) -> None:
        emit("generate", step, message)

    llm = _require_llm(request)
    builder = RealDockerBuilder(timeout=request.build_timeout)

    context = build_context(_docs_for(workspace), workspace)
    # Prepend the instruction to the project facts so both reach the prompt.
    context = replace(context, scan_hint=instruction + context.scan_hint)

    failing = state.test.failing if state.test else []
    names = ", ".join(t.name for t in failing[:4]) or "container structure tests"
    error = (
        f"{len(failing)} container structure test(s) failed: {names}"
        if failing else "container structure tests failed"
    )

    progress("generating", "Writing a fix for the failing tests…")
    candidates = error_driven_patches(current, error, llm, scan_hint=context.scan_hint)
    patched = select_patch(candidates, {current}, error, llm)

    progress("building", "Rebuilding to confirm the fix still builds…")
    result = builder.build(patched, str(workspace))
    if not result.success:
        # A patch that breaks the build is worse than failing tests.
        return GenerationOutcome(
            success=False,
            dockerfile_path=str(dockerfile_path),
            message="The proposed fix no longer builds; the Dockerfile was left unchanged.",
        )

    dockerfile_path.write_text(patched, encoding="utf-8")
    return GenerationOutcome(
        success=True,
        dockerfile_path=str(dockerfile_path),
        attempts=1,
        message="Applied a fix for the failing container tests.",
    )


# ---------------------------------------------------------------------------
# Module 2 — container tests
# ---------------------------------------------------------------------------

def run_tests(request, state: PipelineState, emit) -> TestOutcome:
    from test_generation.pipeline import TestPipeline, _image_name_for

    workspace = Path(request.workspace_path)
    output_path = workspace / ".dockagent" / "tests" / "container-structure-test.yaml"
    state.image_name = _image_name_for(str(workspace))

    def progress(step: str, message: str) -> None:
        emit("test", step, message)

    result = TestPipeline().run(
        dockerfile_path=state.dockerfile_path,
        workspace_path=str(workspace),
        output_path=str(output_path),
        threshold=request.threshold,
        progress=progress,
        execute=True,
        execute_timeout=request.execute_timeout,
    )

    if result.test_run is None:
        return TestOutcome(
            executed=False,
            spec_path=result.output_path,
            message=f"Tests written to {result.output_path} but not executed.",
            warning=result.execution_error,
        )

    run = result.test_run
    return TestOutcome(
        executed=True,
        total=run.total,
        passed=run.passed,
        failed=run.failed,
        failing=[
            FailingTest(name=c.name, errors=list(c.errors))
            for c in run.results if not c.passed
        ],
        spec_path=result.output_path,
        message=f"{run.passed} of {run.total} container tests passed.",
    )


# ---------------------------------------------------------------------------
# Module 3 — flakiness
# ---------------------------------------------------------------------------

def run_flakiness(request, state: PipelineState, emit) -> FlakinessOutcome:
    from dockerfile_generation.build import RealDockerBuilder
    from flakiness_repair.detector import detect
    from flakiness_repair.embed import default_embedder
    from flakiness_repair.knowledge import KnowledgeBase
    from flakiness_repair.loop import repair_flakiness

    workspace = Path(request.workspace_path)
    dockerfile_path = Path(state.dockerfile_path)
    dockerfile = dockerfile_path.read_text(encoding="utf-8")

    def progress(step: str, message: str) -> None:
        emit("flakiness", step, message)

    # Caching is what hides flakiness.
    builder = RealDockerBuilder(timeout=request.build_timeout, no_cache=True)

    progress("detect", f"Building {request.iterations}× without cache…")
    report = detect(
        dockerfile, str(workspace), builder,
        iterations=request.iterations, progress=progress,
    )

    outcome = FlakinessOutcome(
        verdict=report.verdict,
        is_flaky=report.is_flaky,
        needs_repair=report.needs_repair,
        failing_instruction=(
            report.primary_error.dockerfile_error_line if report.primary_error else ""
        ),
        message=report.summary(),
    )
    if not report.needs_repair:
        return outcome

    llm = _require_llm(request)
    knowledge = embedder = None
    try:
        embedder = default_embedder()
        knowledge = KnowledgeBase.load()
        knowledge.index(embedder, progress=progress)
    except Exception as exc:                      # retrieval is optional
        log.warning("retrieval unavailable: %s", exc)
        progress("retrieve", f"Retrieval unavailable ({exc}); continuing without examples.")
        knowledge = embedder = None

    repair = repair_flakiness(
        dockerfile=dockerfile,
        context_dir=str(workspace),
        report=report,
        builder=builder,
        llm=llm,
        knowledge=knowledge,
        embedder=embedder,
        iterations=request.iterations,
        progress=progress,
    )

    outcome.attempts = repair.attempt_count
    outcome.message = repair.message
    if not (repair.success and repair.dockerfile):
        return outcome

    outcome.repaired = True
    if request.apply:
        dockerfile_path.write_text(repair.dockerfile, encoding="utf-8")
        outcome.applied = True
        outcome.repaired_path = str(dockerfile_path)
    else:
        out_dir = workspace / ".dockagent"
        out_dir.mkdir(parents=True, exist_ok=True)
        repaired = out_dir / "Dockerfile.repaired"
        repaired.write_text(repair.dockerfile, encoding="utf-8")
        outcome.repaired_path = str(repaired)
    return outcome
