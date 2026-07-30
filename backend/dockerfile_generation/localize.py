"""Error localization: keyword scan (primary) + LLM on last 50 lines (fallback)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .llm import LLMClient

# The 10 error-indicator phrases from the paper (matched case-insensitively).
ERROR_KEYWORDS: tuple[str, ...] = (
    "no matching distribution found for",
    "unable to locate package",
    "no such file or directory",
    "failed to execute",
    "execution failed",
    "file not found",
    "not specified",
    "unknown",
    "missing",
    "error",
)

_LOG_TAIL_LINES = 50


def keyword_localize(log: str) -> str | None:
    """Scan log lines in reverse; return the first line matching a keyword."""
    lines = log.splitlines()
    for line in reversed(lines):
        lower = line.lower()
        for kw in ERROR_KEYWORDS:
            if kw in lower:
                return line.strip()
    return None


def llm_localize(log: str, llm: LLMClient) -> str:
    """Ask the LLM to identify the key error from the last 50 lines."""
    from .prompts import LLM_LOCALIZE_SYSTEM, LLM_LOCALIZE_USER

    tail = "\n".join(log.splitlines()[-_LOG_TAIL_LINES:])
    return llm.complete(LLM_LOCALIZE_SYSTEM, LLM_LOCALIZE_USER.format(log_tail=tail)).strip()


def localize_error(log: str, llm: LLMClient) -> str:
    """Return the best error description: keyword scan first, LLM as fallback."""
    result = keyword_localize(log)
    if result:
        return result
    return llm_localize(log, llm)
