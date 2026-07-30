"""Patch selection: 4 rules applied in order (DRAFT paper, Section 3.5)."""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .llm import LLMClient


def select_patch(
    candidates: list[str],
    tried: set[str],
    error: str,
    llm: "LLMClient",
) -> str:
    """Pick the best candidate from *candidates* using the 4-rule cascade.

    Rules (applied in order):
      1. Prefer a patch not previously tried.
      2. If 2+ candidates are identical (consensus), pick that one.
      3. Keyword-match core error words against candidate content.
      4. Ask the LLM to choose.
    """
    if not candidates:
        raise ValueError("candidates list must not be empty")

    # Rule 1 — prefer untried
    for c in candidates:
        if c not in tried:
            return c

    # All candidates were already tried. Fall through to rules 2-4.

    # Rule 2 — consensus: 2+ identical candidates
    counts = Counter(candidates)
    most_common, count = counts.most_common(1)[0]
    if count > 1:
        return most_common

    # Rule 3 — keyword match core error words against candidate text
    error_words = set(re.findall(r"\w+", error.lower())) - _STOPWORDS
    if error_words:
        best = max(candidates, key=lambda c: sum(1 for w in error_words if w in c.lower()))
        if any(w in best.lower() for w in error_words):
            return best

    # Rule 4 — LLM chooses
    return _llm_select(candidates, error, llm)


def _llm_select(candidates: list[str], error: str, llm: "LLMClient") -> str:
    from .prompts import LLM_SELECT_SYSTEM, LLM_SELECT_USER

    numbered = "\n\n".join(
        f"Candidate {i + 1}:\n{c}" for i, c in enumerate(candidates)
    )
    user = LLM_SELECT_USER.format(n=len(candidates), error=error, candidates=numbered)
    response = llm.complete(LLM_SELECT_SYSTEM, user).strip()
    try:
        idx = int(re.search(r"\d", response).group()) - 1  # type: ignore[union-attr]
        return candidates[max(0, min(idx, len(candidates) - 1))]
    except (AttributeError, ValueError, IndexError):
        return candidates[0]


# Common words that carry no signal for matching errors to fixes.
_STOPWORDS: frozenset[str] = frozenset(
    {"the", "a", "an", "is", "in", "on", "at", "to", "for", "of", "and", "or", "not"}
)
