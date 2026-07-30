"""Main repair loop: orchestrates build → locate → patch → select → repeat."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .build import DockerBuilder
    from .context import ProjectContext
    from .llm import LLMClient

MAX_ATTEMPTS: int = 6
NO_PROGRESS_THRESHOLD: int = 3  # stop if same error signature repeats this many times
_ERROR_SIG_LEN: int = 120        # chars used as the error signature key


@dataclass
class LoopResult:
    success: bool
    dockerfile: str
    attempts: int
    last_error: str | None
    last_log: str


def run_loop(
    initial_dockerfile: str,
    context: "ProjectContext",
    context_dir: str,
    builder: "DockerBuilder",
    llm: "LLMClient",
    max_attempts: int = MAX_ATTEMPTS,
    on_attempt: Callable[[int, str], None] | None = None,
) -> LoopResult:
    """Run the build-repair loop.

    Alternates error-driven and context-driven patch strategies across iterations.
    Stops early if the same error signature repeats NO_PROGRESS_THRESHOLD times.

    Parameters
    ----------
    initial_dockerfile:
        The starting Dockerfile text (from the generation step).
    context:
        Project context read from build docs.
    context_dir:
        Repository root — passed to the builder as the Docker build context.
    builder:
        A DockerBuilder implementation (real or fake).
    llm:
        A LLMClient implementation.
    max_attempts:
        Hard cap on build attempts.
    """
    from .localize import localize_error
    from .patch import context_driven_patches, error_driven_patches
    from .select import select_patch

    current = initial_dockerfile
    tried: set[str] = set()
    error_sig_counts: dict[str, int] = {}
    last_error: str | None = None
    last_log: str = ""
    use_error_driven: bool = True
    attempt: int = 0

    for attempt in range(1, max_attempts + 1):
        if on_attempt:
            on_attempt(attempt, f"Build attempt {attempt}/{max_attempts}…")

        result = builder.build(current, context_dir)
        last_log = result.log

        if result.success:
            if on_attempt:
                on_attempt(attempt, f"Build succeeded on attempt {attempt}.")
            return LoopResult(
                success=True,
                dockerfile=current,
                attempts=attempt,
                last_error=None,
                last_log=last_log,
            )

        error = localize_error(result.log, llm)
        last_error = error

        if on_attempt:
            on_attempt(attempt, f"Error located: {error[:120]}")

        # No-progress guard: abort if the same error keeps recurring.
        sig = error[:_ERROR_SIG_LEN]
        error_sig_counts[sig] = error_sig_counts.get(sig, 0) + 1
        if error_sig_counts[sig] >= NO_PROGRESS_THRESHOLD:
            if on_attempt:
                on_attempt(attempt, "No progress — same error repeated. Stopping.")
            break

        tried.add(current)

        strategy = "error-driven" if use_error_driven else "context-driven"
        if on_attempt:
            on_attempt(attempt, f"Generating repair candidates ({strategy})…")

        # Generate 3 candidate patches with the current strategy.
        if use_error_driven:
            candidates = error_driven_patches(current, error, llm, scan_hint=context.scan_hint)
        else:
            candidates = context_driven_patches(current, error, context, llm)
        use_error_driven = not use_error_driven  # alternate next iteration

        current = select_patch(candidates, tried, error, llm)

    return LoopResult(
        success=False,
        dockerfile=current,
        attempts=attempt,
        last_error=last_error,
        last_log=last_log,
    )
