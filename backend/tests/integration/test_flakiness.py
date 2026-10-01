"""Integration coverage for Module 3 — real no-cache builds and real retrieval.

Covers report cases T10-T12.
"""

import pytest

from dockerfile_generation.build import RealDockerBuilder
from flakiness_repair.detector import (
    DETERMINISTIC_FAILURE,
    NON_DETERMINISTIC,
    STABLE,
    detect,
)
from flakiness_repair.embed import GeminiEmbedder, LexicalEmbedder
from flakiness_repair.knowledge import KnowledgeBase, build_query
from flakiness_repair.loop import UNABLE_TO_RESOLVE, repair_flakiness

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_STABLE = """\
FROM alpine:3.19
RUN echo stable > /marker.txt
CMD ["cat", "/marker.txt"]
"""

# zlib.net moves superseded releases to /fossils/, so this worked when written
# and fails today — the same root cause as cdecker@lightning in the corpus.
_TEMPORALLY_BROKEN = """\
FROM alpine:3.19
RUN apk add --no-cache wget
WORKDIR /build
RUN wget -q https://zlib.net/zlib-1.2.11.tar.gz
CMD ["echo", "ok"]
"""

# Fails every time for a reason no Dockerfile edit can fix.
_UNFIXABLE = """\
FROM alpine:3.19
RUN echo 'deliberate failure for the stopping-condition test' && exit 7
CMD ["echo", "never"]
"""


# ---------------------------------------------------------------------------
# T10 — Flakiness detection
# ---------------------------------------------------------------------------

def test_t10_a_stable_dockerfile_is_not_classified_as_flaky(
    undocumented_project, nocache_builder
):
    report = detect(_STABLE, str(undocumented_project), nocache_builder, iterations=2)

    assert report.verdict == STABLE
    assert report.is_flaky is False
    assert report.needs_repair is False
    assert report.successes == 2
    # The report must not overclaim: a clean run is not proof of stability.
    assert "not proof" in report.summary()


def test_t10_a_broken_dockerfile_is_classified_and_the_cause_located(
    undocumented_project, nocache_builder
):
    report = detect(
        _TEMPORALLY_BROKEN, str(undocumented_project), nocache_builder, iterations=2
    )

    assert report.verdict == DETERMINISTIC_FAILURE
    assert report.needs_repair is True
    assert report.primary_error is not None
    assert "wget" in report.primary_error.dockerfile_error_line
    assert report.distinct_errors, "no error signature captured"


def test_t10_builds_run_without_cache(undocumented_project):
    """Caching is what hides flakiness, so the builder must disable it."""
    builder = RealDockerBuilder(timeout=600, no_cache=True)
    report = detect(_STABLE, str(undocumented_project), builder, iterations=2)
    assert report.iterations == 2
    assert report.successes == 2


# ---------------------------------------------------------------------------
# T11 — Similar case retrieval and repair generation
# ---------------------------------------------------------------------------

@pytest.mark.needs_llm
def test_t11_retrieval_finds_cases_in_the_same_category(undocumented_project, nocache_builder):
    """A real query against the real Flake4Dock corpus."""
    report = detect(
        _TEMPORALLY_BROKEN, str(undocumented_project), nocache_builder, iterations=1
    )
    assert report.primary_error is not None

    embedder = GeminiEmbedder()
    kb = KnowledgeBase.load()
    kb.index(embedder)

    query = build_query(_TEMPORALLY_BROKEN, report.primary_error.as_query())
    hits = kb.retrieve(query, embedder, k=3)

    assert len(hits) == 3
    assert all(h.demonstration.repair_diffs for h in hits), "a hit carried no repair"
    assert hits[0].score > hits[-1].score, "results are not ranked"

    # A dead download URL is a dependency-retrieval fault; at least one of the
    # three should come from that family rather than something unrelated.
    labels = " ".join((h.demonstration.label or "") for h in hits).lower()
    categories = " ".join((h.demonstration.category or "") for h in hits).lower()
    assert any(word in labels + categories
               for word in ("link", "dependency", "retrieval", "url", "deprecat")), \
        f"retrieved nothing in the right family: {labels}"


def test_t11_retrieval_works_without_an_api_key():
    """The lexical backend keeps retrieval usable offline."""
    embedder = LexicalEmbedder()
    kb = KnowledgeBase.load()
    kb.index(embedder, cache_path=None)

    hits = kb.retrieve("wget 404 not found tar.gz download", embedder, k=3)
    assert len(hits) == 3
    assert hits[0].score > 0


@pytest.mark.needs_llm
def test_t11_repair_addresses_the_detected_failure(
    undocumented_project, nocache_builder, llm
):
    """End to end: detect a real failure, retrieve, repair, re-verify."""
    report = detect(
        _TEMPORALLY_BROKEN, str(undocumented_project), nocache_builder, iterations=1
    )
    embedder = GeminiEmbedder()
    kb = KnowledgeBase.load()
    kb.index(embedder)

    outcome = repair_flakiness(
        dockerfile=_TEMPORALLY_BROKEN,
        context_dir=str(undocumented_project),
        report=report,
        builder=nocache_builder,
        llm=llm,
        knowledge=kb,
        embedder=embedder,
        iterations=1,
    )

    assert outcome.success, f"no repair produced: {outcome.message}"
    assert outcome.dockerfile is not None
    assert outcome.attempts[0].demonstration_ids, "repaired without using examples"
    # The fix must address the dead URL rather than deleting the step.
    assert "wget" in outcome.dockerfile, "the failing step was removed, not repaired"


# ---------------------------------------------------------------------------
# T12 — Repair verification and stopping condition
# ---------------------------------------------------------------------------

@pytest.mark.needs_llm
def test_t12_a_repair_is_confirmed_only_by_repeated_clean_builds(
    undocumented_project, nocache_builder, llm
):
    report = detect(
        _TEMPORALLY_BROKEN, str(undocumented_project), nocache_builder, iterations=1
    )
    outcome = repair_flakiness(
        dockerfile=_TEMPORALLY_BROKEN,
        context_dir=str(undocumented_project),
        report=report,
        builder=nocache_builder,
        llm=llm,
        iterations=2,                 # two clean builds required
    )

    assert outcome.success
    assert outcome.validated_builds == 2, "accepted on fewer builds than required"


@pytest.mark.needs_llm
def test_t12_attempts_never_exceed_the_threshold(
    undocumented_project, nocache_builder, llm
):
    """The bound holds whatever the model does.

    Note for the report: a deliberately failing RUN is not unfixable — the
    model simply deletes the failing line and the build passes. A capable model
    can repair almost any self-inflicted failure, so this run usually succeeds.
    What must hold either way is that it stops at the threshold.
    """
    report = detect(
        _UNFIXABLE, str(undocumented_project), nocache_builder, iterations=1
    )
    assert report.needs_repair

    outcome = repair_flakiness(
        dockerfile=_UNFIXABLE,
        context_dir=str(undocumented_project),
        report=report,
        builder=nocache_builder,
        llm=llm,
        iterations=1,
        max_attempts=3,
    )

    assert outcome.attempt_count <= 3, "ran past the configured threshold"


def test_t12_give_up_path_reports_unable_to_resolve(
    undocumented_project, nocache_builder
):
    """The stopping condition itself, with real builds deciding it.

    Only the model is substituted — by one that keeps proposing the same
    broken file — because a capable model repairs the failure instead of
    reaching the limit, so the branch is otherwise unreachable.
    """
    class _NeverImproves:
        def complete(self, system, user):
            return f"```dockerfile\n{_UNFIXABLE}```"

    report = detect(
        _UNFIXABLE, str(undocumented_project), nocache_builder, iterations=1
    )
    outcome = repair_flakiness(
        dockerfile=_UNFIXABLE,
        context_dir=str(undocumented_project),
        report=report,
        builder=nocache_builder,
        llm=_NeverImproves(),
        iterations=1,
        max_attempts=3,
    )

    assert outcome.success is False
    assert outcome.attempt_count <= 3
    assert UNABLE_TO_RESOLVE in outcome.message or "No working repair" in outcome.message
