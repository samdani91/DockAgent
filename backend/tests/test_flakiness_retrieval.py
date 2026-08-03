"""Tests for embedding backends and demonstration retrieval.

No network: the lexical embedder is deterministic and needs no API key.
"""

import json

import pytest

from flakiness_repair.embed import (
    TASK_DOCUMENT,
    TASK_QUERY,
    LexicalEmbedder,
    cosine,
)
from flakiness_repair.knowledge import (
    Demonstration,
    KnowledgeBase,
    build_query,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_RECORDS = [
    {
        "id": "acme@web#1", "project": "acme@web", "label": "Broken Link",
        "category": "Dependency-Related Errors -> Retrieval/Import Issues",
        "rounds": 1,
        "dockerfile": "FROM debian:bullseye\nRUN wget -q https://zlib.net/zlib.tar.gz",
        "error": "** STDERR **: \nwget: server returned error: 404 Not Found for zlib.net",
        "repair_diffs": ["-RUN wget https://zlib.net/zlib.tar.gz\n"
                         "+RUN wget https://zlib.net/fossils/zlib.tar.gz"],
    },
    {
        "id": "acme@api#1", "project": "acme@api", "label": "Base Image Unavailable",
        "category": "Dependency-Related Errors -> Base Image Availability Issues",
        "rounds": 2,
        "dockerfile": "FROM golang:1.9.1 AS build\nRUN go build -o proxy",
        "error": "** STDERR **: \nfailed to solve: golang:1.9.1: not found manifest unknown",
        "repair_diffs": ["-FROM golang:1.9.1 AS build\n+FROM golang:1.22 AS build"],
    },
    {
        "id": "acme@ui#1", "project": "acme@ui", "label": "Timeout Faults",
        "category": "Server Connectivity Errors -> Timeout Issues",
        "rounds": 3,
        "dockerfile": "FROM node:16.14.0\nRUN yarn install --production",
        "error": "** STDERR **: \nESOCKETTIMEDOUT registry.yarnpkg.com yarn install",
        "repair_diffs": ["+RUN yarn config set network-timeout 600000"],
    },
]


@pytest.fixture
def corpus(tmp_path):
    path = tmp_path / "corpus.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in _RECORDS), encoding="utf-8")
    return path


@pytest.fixture
def kb(corpus):
    base = KnowledgeBase.load(corpus)
    base.index(LexicalEmbedder(), cache_path=None)
    return base


# ---------------------------------------------------------------------------
# cosine
# ---------------------------------------------------------------------------

def test_cosine_identical_is_one():
    assert cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_is_zero():
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_handles_degenerate_input():
    assert cosine([], [1.0]) == 0.0
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert cosine([1.0, 2.0], [1.0]) == 0.0     # length mismatch


# ---------------------------------------------------------------------------
# LexicalEmbedder
# ---------------------------------------------------------------------------

def test_lexical_returns_one_vector_per_text():
    vectors = LexicalEmbedder().embed(["alpha beta", "gamma", "delta"])
    assert len(vectors) == 3


def test_lexical_is_deterministic_across_instances():
    """Uses hashlib, not hash() — otherwise a cached index breaks next run."""
    a = LexicalEmbedder().embed(["RUN apt-get install curl"])[0]
    b = LexicalEmbedder().embed(["RUN apt-get install curl"])[0]
    assert a == b


def test_lexical_vectors_are_normalised():
    v = LexicalEmbedder().embed(["some docker build error text"])[0]
    assert sum(x * x for x in v) == pytest.approx(1.0)


def test_lexical_similar_text_scores_higher_than_unrelated():
    e = LexicalEmbedder()
    q, near, far = e.embed([
        "wget 404 not found zlib.net",
        "wget server returned 404 for zlib.net archive",
        "yarn install ESOCKETTIMEDOUT registry",
    ])
    assert cosine(q, near) > cosine(q, far)


def test_lexical_handles_empty_text():
    v = LexicalEmbedder().embed([""])[0]
    assert len(v) == 1024
    assert all(x == 0.0 for x in v)


# ---------------------------------------------------------------------------
# build_query
# ---------------------------------------------------------------------------

def test_query_layout_matches_record_document():
    """A record's document and a query built from the same inputs must agree."""
    demo = Demonstration.from_dict(_RECORDS[0])
    assert demo.as_document() == build_query(demo.dockerfile, demo.error)


def test_query_puts_the_error_first():
    q = build_query("FROM alpine", "** STDERR **: \nboom")
    assert q.index("boom") < q.index("FROM alpine")


def test_query_tolerates_missing_parts():
    assert build_query("", "just an error").strip() == "just an error"
    assert "FROM alpine" in build_query("FROM alpine", "")


# ---------------------------------------------------------------------------
# KnowledgeBase
# ---------------------------------------------------------------------------

def test_loads_all_records(kb):
    assert len(kb) == 3
    assert kb.is_indexed


def test_load_rejects_missing_corpus(tmp_path):
    with pytest.raises(FileNotFoundError):
        KnowledgeBase.load(tmp_path / "nope.jsonl")


def test_load_reports_malformed_json(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"id": "ok"}\nnot json at all\n', encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        KnowledgeBase.load(bad)


def test_retrieve_before_index_raises(corpus):
    base = KnowledgeBase.load(corpus)
    with pytest.raises(RuntimeError, match="index"):
        base.retrieve("anything", LexicalEmbedder())


def test_retrieve_ranks_the_relevant_record_first(kb):
    hits = kb.retrieve(
        build_query("FROM node:16.14.0\nRUN yarn install",
                    "ESOCKETTIMEDOUT registry.yarnpkg.com"),
        LexicalEmbedder(),
        k=3,
    )
    assert hits[0].demonstration.id == "acme@ui#1"


def test_retrieve_finds_base_image_record(kb):
    hits = kb.retrieve(
        build_query("FROM golang:1.9.1", "golang:1.9.1: not found manifest unknown"),
        LexicalEmbedder(),
        k=1,
    )
    assert hits[0].demonstration.label == "Base Image Unavailable"


def test_retrieve_respects_k(kb):
    assert len(kb.retrieve("wget 404", LexicalEmbedder(), k=2)) == 2
    assert len(kb.retrieve("wget 404", LexicalEmbedder(), k=1)) == 1


def test_retrieve_returns_scores_in_descending_order(kb):
    hits = kb.retrieve("wget 404 zlib", LexicalEmbedder(), k=3)
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


def test_retrieve_can_exclude_ids(kb):
    hits = kb.retrieve("ESOCKETTIMEDOUT yarn", LexicalEmbedder(), k=3,
                       exclude_ids={"acme@ui#1"})
    assert all(h.demonstration.id != "acme@ui#1" for h in hits)


def test_retrieve_empty_query_returns_nothing(kb):
    assert kb.retrieve("   ", LexicalEmbedder(), k=3) == []


def test_repair_diffs_survive_the_round_trip(kb):
    hits = kb.retrieve("golang not found manifest unknown", LexicalEmbedder(), k=1)
    assert "FROM golang:1.22" in hits[0].demonstration.repair_diffs[0]


# ---------------------------------------------------------------------------
# Embedding cache
# ---------------------------------------------------------------------------

class _CountingEmbedder(LexicalEmbedder):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def embed(self, texts, task_type=TASK_DOCUMENT):
        self.calls += 1
        return super().embed(texts, task_type)


def test_cache_avoids_re_embedding(corpus, tmp_path):
    cache = tmp_path / "cache.json"

    first = KnowledgeBase.load(corpus)
    e1 = _CountingEmbedder()
    first.index(e1, cache_path=cache)
    assert e1.calls == 1
    assert cache.is_file()

    second = KnowledgeBase.load(corpus)
    e2 = _CountingEmbedder()
    second.index(e2, cache_path=cache)
    assert e2.calls == 0            # served from cache
    assert second.is_indexed


def test_cache_is_invalidated_by_a_different_embedder(corpus, tmp_path):
    cache = tmp_path / "cache.json"
    KnowledgeBase.load(corpus).index(LexicalEmbedder(dimensions=1024), cache_path=cache)

    base = KnowledgeBase.load(corpus)
    e = _CountingEmbedder()
    e._dim = 512                     # different backend identity
    base.index(LexicalEmbedder(dimensions=512), cache_path=cache)
    assert base.is_indexed


def test_cache_is_invalidated_by_a_changed_corpus(corpus, tmp_path):
    cache = tmp_path / "cache.json"
    KnowledgeBase.load(corpus).index(LexicalEmbedder(), cache_path=cache)

    extended = tmp_path / "more.jsonl"
    extended.write_text(
        corpus.read_text(encoding="utf-8") + "\n" + json.dumps(
            {"id": "new@one#1", "dockerfile": "FROM alpine", "error": "boom"}
        ),
        encoding="utf-8",
    )

    base = KnowledgeBase.load(extended)
    e = _CountingEmbedder()
    base.index(e, cache_path=cache)
    assert e.calls == 1             # fingerprint changed → re-embedded
    assert len(base) == 4


def test_corrupt_cache_falls_back_to_embedding(corpus, tmp_path):
    cache = tmp_path / "cache.json"
    cache.write_text("{ not json", encoding="utf-8")

    base = KnowledgeBase.load(corpus)
    e = _CountingEmbedder()
    base.index(e, cache_path=cache)
    assert e.calls == 1
    assert base.is_indexed


def test_index_rejects_a_wrong_length_result(corpus):
    class _Broken(LexicalEmbedder):
        def embed(self, texts, task_type=TASK_DOCUMENT):
            return super().embed(texts[:1], task_type)   # too few

    with pytest.raises(RuntimeError, match="vectors"):
        KnowledgeBase.load(corpus).index(_Broken(), cache_path=None)
