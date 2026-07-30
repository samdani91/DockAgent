"""Initial Dockerfile generation from project context."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .context import ProjectContext
    from .llm import LLMClient


def generate_initial(context: "ProjectContext", llm: "LLMClient") -> str:
    """Send project context to the LLM and return the first candidate Dockerfile."""
    from .prompts import INITIAL_GENERATION_USER, SYSTEM_GENERATION

    user = INITIAL_GENERATION_USER.format(context=context.text)
    raw = llm.complete(SYSTEM_GENERATION, user).strip()
    return _strip_fences(raw)


def _strip_fences(text: str) -> str:
    """Remove markdown code fences if the model added them despite instructions."""
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()
