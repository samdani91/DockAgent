"""Generate 3 repair candidates per iteration using two complementary strategies."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .context import ProjectContext
    from .llm import LLMClient

_N_CANDIDATES = 3


def _strip_fences(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def error_driven_patches(
    dockerfile: str,
    error: str,
    llm: "LLMClient",
    scan_hint: str = "",
) -> list[str]:
    """Strategy 1 — error-driven: current Dockerfile + error + optional scan hint → 3 candidates.

    *scan_hint* is a compact block of critical project facts (entry point, port) that
    prevents the LLM from regressing on values it got right in previous iterations.
    """
    from .prompts import REPAIR_USER, SYSTEM_GENERATION

    candidates: list[str] = []
    for _ in range(_N_CANDIDATES):
        user = REPAIR_USER.format(
            scan_hint=scan_hint,
            dockerfile=dockerfile,
            error=error,
        )
        raw = llm.complete(SYSTEM_GENERATION, user).strip()
        candidates.append(_strip_fences(raw))
    return candidates


def context_driven_patches(
    dockerfile: str,
    error: str,
    context: "ProjectContext",
    llm: "LLMClient",
) -> list[str]:
    """Strategy 2 — context-driven: same as error-driven but prefixed with all project context."""
    from .prompts import CONTEXT_REPAIR_USER, SYSTEM_GENERATION

    candidates: list[str] = []
    for _ in range(_N_CANDIDATES):
        user = CONTEXT_REPAIR_USER.format(
            context=context.text,
            dockerfile=dockerfile,
            error=error,
        )
        raw = llm.complete(SYSTEM_GENERATION, user).strip()
        candidates.append(_strip_fences(raw))
    return candidates
