"""Repair generation — one LLM call producing a candidate Dockerfile.

The paper runs its model at temperature 0 for deterministic output. The shared
LLMClient protocol does not currently accept a temperature, so support is
detected rather than assumed: a client that takes the argument gets 0, one that
does not is called unchanged.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .prompts import (
    DEMONSTRATION,
    DEMONSTRATIONS_HEADER,
    FALSE_REPAIR,
    FEEDBACK_HEADER,
    REPAIR_USER,
    SYSTEM_REPAIR,
)

if TYPE_CHECKING:
    from dockerfile_generation.llm import LLMClient

    from .knowledge import RetrievalHit

#: Paper's setting — deterministic output.
REPAIR_TEMPERATURE = 0.0

#: Keep individual prompt sections bounded; build logs can be enormous.
MAX_ERROR_CHARS = 3000
MAX_DEMO_ERROR_CHARS = 1200
MAX_DEMO_REPAIR_CHARS = 1500

# Any fenced block, whatever its language tag. Matching only "dockerfile" tags
# would let an unrecognised tag (```bash) desynchronise the scan, so that a
# match then ran from one block's closing fence to the next block's opening one.
_FENCE = re.compile(
    r"```[A-Za-z0-9_+.-]*[ \t]*\r?\n(?P<body>.*?)```",
    re.DOTALL,
)


@dataclass
class FalseRepairRecord:
    """A rejected repair and the failure it produced (the paper's R_fr, D_fr)."""
    dockerfile: str
    error: str


@dataclass
class RepairCandidate:
    dockerfile: str
    raw_response: str = ""
    demonstration_ids: list[str] = field(default_factory=list)


def generate_repair(
    dockerfile: str,
    error: str,
    llm: "LLMClient",
    demonstrations: list["RetrievalHit"] | None = None,
    feedback: list[FalseRepairRecord] | None = None,
) -> RepairCandidate:
    """Ask the model for one repaired Dockerfile."""
    hits = demonstrations or []
    user = REPAIR_USER.format(
        dockerfile=dockerfile.strip(),
        error=_clip(error, MAX_ERROR_CHARS),
        demonstrations=_render_demonstrations(hits),
        feedback=_render_feedback(feedback or []),
    )

    raw = _complete(llm, SYSTEM_REPAIR, user).strip()
    repaired = _extract_dockerfile(raw)
    if not repaired:
        raise RuntimeError(
            "The model did not return a Dockerfile. Response began:\n"
            + raw[:400]
        )

    return RepairCandidate(
        dockerfile=repaired,
        raw_response=raw,
        demonstration_ids=[h.demonstration.id for h in hits],
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _complete(llm: "LLMClient", system: str, user: str) -> str:
    """Call the client, passing temperature 0 only if it accepts one."""
    if _accepts_temperature(llm):
        return llm.complete(system, user, temperature=REPAIR_TEMPERATURE)
    return llm.complete(system, user)


def _accepts_temperature(llm: "LLMClient") -> bool:
    try:
        return "temperature" in inspect.signature(llm.complete).parameters
    except (TypeError, ValueError):
        return False


def _extract_dockerfile(response: str) -> str:
    """Pull the Dockerfile out of a chain-of-thought response.

    The prompt asks for reasoning followed by one fenced block, so the LAST
    fenced block is the answer. Falls back to treating the whole response as a
    Dockerfile when the model ignored the fencing.
    """
    blocks = [m.group("body").strip() for m in _FENCE.finditer(response)]
    blocks = [b for b in blocks if b]
    if blocks:
        # Prefer the last block that actually looks like a Dockerfile.
        for block in reversed(blocks):
            if _looks_like_dockerfile(block):
                return block
        return blocks[-1]

    stripped = response.strip()
    return stripped if _looks_like_dockerfile(stripped) else ""


def _looks_like_dockerfile(text: str) -> bool:
    return bool(re.search(r"^\s*FROM\s+\S+", text, re.IGNORECASE | re.MULTILINE))


def _render_demonstrations(hits: list["RetrievalHit"]) -> str:
    if not hits:
        return ""
    examples = []
    for i, hit in enumerate(hits, 1):
        demo = hit.demonstration
        repair = demo.repair_diffs[0] if demo.repair_diffs else ""
        examples.append(
            DEMONSTRATION.format(
                n=i,
                label=demo.label or demo.category or "similar failure",
                error=_clip(demo.error, MAX_DEMO_ERROR_CHARS),
                repair=_clip(repair, MAX_DEMO_REPAIR_CHARS),
            )
        )
    return DEMONSTRATIONS_HEADER.format(examples="\n".join(examples))


def _render_feedback(feedback: list[FalseRepairRecord]) -> str:
    if not feedback:
        return ""
    attempts = [
        FALSE_REPAIR.format(
            n=i,
            dockerfile=_clip(record.dockerfile, MAX_DEMO_REPAIR_CHARS),
            error=_clip(record.error, MAX_DEMO_ERROR_CHARS),
        )
        for i, record in enumerate(feedback, 1)
    ]
    return FEEDBACK_HEADER.format(attempts="\n".join(attempts))


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n… [truncated]"
