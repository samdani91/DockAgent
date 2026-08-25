"""The developer-facing half of the agent.

Routing decisions are made in routing.py without a model. This module only
explains what happened and answers questions — so a model failure degrades the
narration, never the pipeline.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .prompts import (
    ANSWER_NO_RUN,
    ANSWER_USER,
    EXPLAIN_USER,
    SUMMARY_USER,
    SYSTEM_ANSWER,
    SYSTEM_EXPLAIN,
)

if TYPE_CHECKING:
    from dockerfile_generation.llm import LLMClient

    from .state import PipelineState

log = logging.getLogger("dockagent.agent")

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class Explanation:
    explanation: str
    recommendation: str = ""
    action_required: bool = False


def explain(
    stage: str,
    outcome: str,
    state: "PipelineState",
    llm: "LLMClient | None",
) -> Explanation:
    """Explain what a stage did. Falls back to the raw outcome without an LLM."""
    if llm is None:
        return Explanation(explanation=outcome)

    user = EXPLAIN_USER.format(
        stage=stage,
        outcome=outcome,
        history="\n".join(state.summary_lines()) or "(nothing yet)",
    )
    try:
        raw = llm.complete(SYSTEM_EXPLAIN, user)
    except Exception as exc:                      # narration must not break a run
        log.warning("explanation unavailable: %s", exc)
        return Explanation(explanation=outcome)

    return _parse_explanation(raw, fallback=outcome)


def answer_query(
    question: str,
    state: "PipelineState | None",
    llm: "LLMClient | None",
) -> str:
    """Answer a developer question using the run as context."""
    if llm is None:
        return (
            "The assistant needs an LLM provider configured. Set GEMINI_API_KEY "
            "in backend/.env and restart the backend."
        )

    if state is None:
        user = ANSWER_NO_RUN.format(question=question)
    else:
        user = ANSWER_USER.format(
            state="\n".join(state.summary_lines()) or "(no results recorded)",
            question=question,
        )

    try:
        return llm.complete(SYSTEM_ANSWER, user).strip()
    except Exception as exc:
        log.error("query failed: %s", exc)
        return f"Could not reach the model to answer that: {exc}"


def summarise(state: "PipelineState", llm: "LLMClient | None") -> str:
    """The agent's closing summary for the whole run."""
    lines = state.summary_lines()
    fallback = " ".join(lines) if lines else "The pipeline finished."
    if llm is None:
        return fallback

    try:
        return llm.complete(
            SYSTEM_ANSWER, SUMMARY_USER.format(history="\n".join(lines))
        ).strip() or fallback
    except Exception as exc:
        log.warning("summary unavailable: %s", exc)
        return fallback


def _parse_explanation(raw: str, fallback: str) -> Explanation:
    """Parse the JSON reply, tolerating fences and surrounding prose."""
    match = _JSON_BLOCK.search(raw or "")
    if match:
        try:
            payload = json.loads(match.group())
            return Explanation(
                explanation=str(payload.get("explanation") or fallback).strip(),
                recommendation=str(payload.get("recommendation") or "").strip(),
                action_required=bool(payload.get("action_required")),
            )
        except (json.JSONDecodeError, AttributeError):
            pass

    # Model ignored the format — its prose is still better than nothing.
    text = (raw or "").strip()
    return Explanation(explanation=text or fallback)
