"""Repair validation loop — the paper's Algorithm 1.

    build the repair n times
      ├─ all succeed                → accept it
      ├─ same error seen T times    → "Unable to resolve!"
      └─ otherwise                  → record the false repair as feedback and retry

The threshold exists because, past three attempts against the same error, the
model tends to keep proposing variations of a fix that cannot work; the paper
sets T = 3.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from .detector import DEFAULT_ITERATIONS, detect
from .embed import LexicalEmbedder, cosine
from .knowledge import DEFAULT_TOP_K, build_query
from .preprocess import ErrorFeatures
from .repair import FalseRepairRecord, generate_repair

log = logging.getLogger("dockagent.flakiness")

if TYPE_CHECKING:
    from dockerfile_generation.build import DockerBuilder
    from dockerfile_generation.llm import LLMClient

    from .detector import FlakinessReport
    from .embed import Embedder
    from .knowledge import KnowledgeBase

#: Paper's T — how many times the same error may recur before giving up.
FAILURE_THRESHOLD: int = 3

#: Two build outputs above this cosine similarity count as "the same error".
#: The paper compares build outputs with a sentence-transformer; the lexical
#: embedder is used here so the comparison costs nothing and stays offline.
SIMILARITY_THRESHOLD: float = 0.9

UNABLE_TO_RESOLVE = "Unable to resolve!"


@dataclass
class RepairAttempt:
    index: int
    dockerfile: str
    succeeded: bool
    error: ErrorFeatures | None = None
    demonstration_ids: list[str] = field(default_factory=list)


@dataclass
class RepairOutcome:
    success: bool
    dockerfile: str | None          # the accepted repair, when successful
    attempts: list[RepairAttempt] = field(default_factory=list)
    message: str = ""
    validated_builds: int = 0       # successful builds behind the accepted repair

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)


def repair_flakiness(
    dockerfile: str,
    context_dir: str,
    report: "FlakinessReport",
    builder: "DockerBuilder",
    llm: "LLMClient",
    knowledge: "KnowledgeBase | None" = None,
    embedder: "Embedder | None" = None,
    iterations: int = DEFAULT_ITERATIONS,
    max_attempts: int = FAILURE_THRESHOLD,
    top_k: int = DEFAULT_TOP_K,
    progress: Callable[[str, str], None] | None = None,
) -> RepairOutcome:
    """Repair the failure described by *report*, validating each candidate.

    *builder* must have caching disabled — a cached layer would let a repair
    appear to work when it has not been exercised.
    """
    emit = progress or (lambda _s, _m: None)

    error = report.primary_error
    if error is None:
        return RepairOutcome(
            success=False,
            dockerfile=None,
            message="Nothing to repair: no build failure was captured.",
        )

    query = build_query(dockerfile, error.as_query())
    hits = _retrieve(knowledge, embedder, query, top_k, emit)

    similarity = LexicalEmbedder()
    feedback: list[FalseRepairRecord] = []
    attempts: list[RepairAttempt] = []
    current = dockerfile

    for attempt_no in range(1, max_attempts + 1):
        log.info("── repair attempt %d of %d ──", attempt_no, max_attempts)
        emit("repair", f"Generating repair {attempt_no} of {max_attempts}…")
        try:
            candidate = generate_repair(
                dockerfile=current,
                error=error.as_query(),
                llm=llm,
                demonstrations=hits,
                feedback=feedback,
            )
        except RuntimeError as exc:
            return RepairOutcome(
                success=False,
                dockerfile=None,
                attempts=attempts,
                message=f"Repair generation failed: {exc}",
            )

        # -- validation: build the candidate n times ------------------------
        emit("repair", f"Validating repair {attempt_no} ({iterations} builds)…")
        validation = detect(
            candidate.dockerfile, context_dir, builder,
            iterations=iterations, progress=progress,
        )

        if validation.failures == 0:
            attempts.append(RepairAttempt(
                index=attempt_no,
                dockerfile=candidate.dockerfile,
                succeeded=True,
                demonstration_ids=candidate.demonstration_ids,
            ))
            log.info("repair accepted after %d attempt(s), validated by %d clean builds",
                     attempt_no, validation.successes)
            emit("repair", f"Repair validated across {iterations} builds.")
            return RepairOutcome(
                success=True,
                dockerfile=candidate.dockerfile,
                attempts=attempts,
                message=f"Repaired and validated across {iterations} clean builds.",
                validated_builds=validation.successes,
            )

        new_error = validation.primary_error or ErrorFeatures()
        attempts.append(RepairAttempt(
            index=attempt_no,
            dockerfile=candidate.dockerfile,
            succeeded=False,
            error=new_error,
            demonstration_ids=candidate.demonstration_ids,
        ))

        # -- Algorithm 1: has this same error already defeated us T times? ---
        similar = _count_similar_failures(new_error, feedback, similarity)
        log.info("attempt %d failed; %d prior attempt(s) failed the same way",
                 attempt_no, similar)
        if similar + 1 >= max_attempts:
            log.warning("%s giving up after %d similar failures",
                        UNABLE_TO_RESOLVE, similar + 1)
            emit("repair", f"{UNABLE_TO_RESOLVE} The same error keeps recurring.")
            return RepairOutcome(
                success=False,
                dockerfile=None,
                attempts=attempts,
                message=(
                    f"{UNABLE_TO_RESOLVE} The same failure recurred across "
                    f"{similar + 1} repair attempts."
                ),
            )

        feedback.append(FalseRepairRecord(
            dockerfile=candidate.dockerfile,
            error=new_error.as_query(),
        ))
        emit("repair", f"Repair {attempt_no} still fails — feeding the result back.")

    return RepairOutcome(
        success=False,
        dockerfile=None,
        attempts=attempts,
        message=f"No working repair after {max_attempts} attempts.",
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _retrieve(
    knowledge: "KnowledgeBase | None",
    embedder: "Embedder | None",
    query: str,
    top_k: int,
    emit: Callable[[str, str], None],
):
    """Fetch demonstrations, degrading to none if retrieval is unavailable."""
    if knowledge is None or embedder is None:
        emit("retrieve", "No demonstration corpus — repairing without examples.")
        return []
    try:
        hits = knowledge.retrieve(query, embedder, k=top_k)
    except (RuntimeError, ValueError) as exc:
        emit("retrieve", f"Retrieval unavailable ({exc}); continuing without examples.")
        return []

    if hits:
        labels = ", ".join(h.demonstration.label or "?" for h in hits)
        emit("retrieve", f"Retrieved {len(hits)} similar repairs: {labels}")
    return hits


def _count_similar_failures(
    error: ErrorFeatures,
    feedback: list[FalseRepairRecord],
    embedder: "Embedder",
) -> int:
    """How many recorded false repairs failed the same way as *error*."""
    if not feedback:
        return 0

    signature = error.signature()
    texts = [error.as_query()] + [f.error for f in feedback]
    vectors = embedder.embed(texts)
    current, previous = vectors[0], vectors[1:]

    count = 0
    for record, vector in zip(feedback, previous):
        if signature and signature in record.error.lower():
            count += 1
        elif cosine(current, vector) >= SIMILARITY_THRESHOLD:
            count += 1
    return count
