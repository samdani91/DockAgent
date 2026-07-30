"""Phase B — image optimization (DRAFT, Lyu et al., ICSE 2026).

The paper distils five optimization methods from Stack Overflow; this module
currently implements **multi-stage builds**, the most effective single method
for size (46.19% average reduction in the paper).

The contract from the paper is what makes this safe:

    apply → re-build → re-verify
      ├─ builds & smaller  → keep the optimized file
      ├─ does not build    → feed it through the Phase A repair loop
      └─ still broken      → fall back to the last known-good Dockerfile

Optimization is therefore never able to leave the project worse off than the
Dockerfile Phase A produced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

# Reuses the fence-stripper from the generation step rather than adding a third
# copy of it (generate.py and patch.py already each define one — worth
# consolidating into a shared helper when one of those is next touched).
from .generate import _strip_fences

if TYPE_CHECKING:
    from .build import DockerBuilder
    from .context import ProjectContext
    from .llm import LLMClient

MULTI_STAGE: str = "multi-stage"

#: Repair budget for an optimized-but-broken Dockerfile. Deliberately smaller
#: than the Phase A cap: if a rewrite needs many repairs it is not worth the
#: API spend when a working Dockerfile already exists.
DEFAULT_REPAIR_ATTEMPTS: int = 3


@dataclass
class OptimizationResult:
    success: bool           # True only if an optimized file was kept
    dockerfile: str         # optimized text on success, the original on fallback
    method: str
    original_size: int | None
    optimized_size: int | None
    repair_attempts: int
    note: str               # human-readable outcome, safe to show in the UI

    @property
    def reduction_pct(self) -> float | None:
        """Percentage size reduction, or None if either size is unknown."""
        if not self.original_size or self.optimized_size is None:
            return None
        return (self.original_size - self.optimized_size) / self.original_size * 100


def optimize(
    dockerfile: str,
    context: "ProjectContext",
    context_dir: str,
    builder: "DockerBuilder",
    llm: "LLMClient",
    method: str = MULTI_STAGE,
    max_repair_attempts: int = DEFAULT_REPAIR_ATTEMPTS,
    sizer: Callable[[str], int | None] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> OptimizationResult:
    """Optimize a *known-good* Dockerfile, falling back to it if anything fails.

    Parameters
    ----------
    dockerfile:
        A Dockerfile that already builds — the output of Phase A.
    context:
        Project context; its ``scan_hint`` pins the entry point and port so the
        rewrite cannot regress them.
    context_dir:
        Repository root, used as the Docker build context.
    sizer:
        Maps an image ID to its size in bytes. Defaults to the real
        ``docker image inspect`` call; injectable so tests need no Docker.
    """
    from .build import image_size_bytes

    measure = sizer or image_size_bytes
    emit = on_progress or (lambda _msg: None)

    if method != MULTI_STAGE:
        raise ValueError(
            f"Unsupported optimization method: {method!r}. Supported: {MULTI_STAGE!r}."
        )

    # ── Baseline ───────────────────────────────────────────────────────────
    # Rebuild the known-good file to obtain its image ID (and therefore size).
    # Layer cache makes this cheap; it also guards against optimizing a file
    # that has stopped building since Phase A ran.
    emit("Measuring baseline image size…")
    baseline = builder.build(dockerfile, context_dir)
    if not baseline.success:
        return OptimizationResult(
            success=False,
            dockerfile=dockerfile,
            method=method,
            original_size=None,
            optimized_size=None,
            repair_attempts=0,
            note="Baseline Dockerfile no longer builds — optimization skipped.",
        )

    original_size = measure(baseline.image_id) if baseline.image_id else None

    # ── Rewrite ────────────────────────────────────────────────────────────
    emit(f"Generating {method} rewrite…")
    candidate = _rewrite(dockerfile, context, llm, method)

    # ── Re-build ───────────────────────────────────────────────────────────
    emit("Building optimized Dockerfile…")
    result = builder.build(candidate, context_dir)
    repair_attempts = 0

    # ── Broken? Hand it to the Phase A repair loop ─────────────────────────
    if not result.success:
        emit("Optimized build failed — entering repair loop…")
        from .loop import run_loop

        loop_result = run_loop(
            initial_dockerfile=candidate,
            context=context,
            context_dir=context_dir,
            builder=builder,
            llm=llm,
            max_attempts=max_repair_attempts,
        )
        repair_attempts = loop_result.attempts

        if not loop_result.success:
            return OptimizationResult(
                success=False,
                dockerfile=dockerfile,
                method=method,
                original_size=original_size,
                optimized_size=None,
                repair_attempts=repair_attempts,
                note=(
                    f"Optimized Dockerfile could not be repaired in "
                    f"{repair_attempts} attempt(s) — kept the original."
                ),
            )

        candidate = loop_result.dockerfile
        # run_loop does not surface the image ID, so re-verify to obtain one.
        # This is cache-warm and therefore near-instant.
        emit("Re-verifying repaired Dockerfile…")
        result = builder.build(candidate, context_dir)
        if not result.success:
            return OptimizationResult(
                success=False,
                dockerfile=dockerfile,
                method=method,
                original_size=original_size,
                optimized_size=None,
                repair_attempts=repair_attempts,
                note="Repaired Dockerfile failed re-verification — kept the original.",
            )

    # ── Compare ────────────────────────────────────────────────────────────
    optimized_size = measure(result.image_id) if result.image_id else None

    # Deviation from the paper, which keeps any optimized file that still
    # builds: we additionally reject a rewrite that did not actually shrink
    # the image, since keeping it would be a pure regression.
    if (
        original_size is not None
        and optimized_size is not None
        and optimized_size >= original_size
    ):
        return OptimizationResult(
            success=False,
            dockerfile=dockerfile,
            method=method,
            original_size=original_size,
            optimized_size=optimized_size,
            repair_attempts=repair_attempts,
            note="Optimized image is not smaller — kept the original.",
        )

    return OptimizationResult(
        success=True,
        dockerfile=candidate,
        method=method,
        original_size=original_size,
        optimized_size=optimized_size,
        repair_attempts=repair_attempts,
        note="Optimization applied.",
    )


def _rewrite(
    dockerfile: str,
    context: "ProjectContext",
    llm: "LLMClient",
    method: str,
) -> str:
    """Ask the LLM to apply *method* to *dockerfile*."""
    from .prompts import MULTI_STAGE_USER, SYSTEM_GENERATION

    user = MULTI_STAGE_USER.format(
        scan_hint=context.scan_hint,
        dockerfile=dockerfile,
    )
    raw = llm.complete(SYSTEM_GENERATION, user).strip()
    return _strip_fences(raw)
