"""The demonstration corpus and top-k retrieval over it.

FLAKIDOCK forms the query from both static and dynamic features — the
Dockerfile and its pre-processed build output — and retrieves the top-3 most
similar records by cosine similarity, passing their repairs to the LLM as
few-shot demonstrations.

Records come from Flake4Dock via data/extract_flake4dock.py.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable

from .embed import TASK_DOCUMENT, TASK_QUERY, cosine

if TYPE_CHECKING:
    from .embed import Embedder

DEFAULT_CORPUS = Path(__file__).parent / "data" / "flake4dock_repairs.jsonl"
DEFAULT_CACHE = Path(__file__).parent / "data" / ".embedding_cache.json"
DEFAULT_TOP_K = 3   # paper's k


@dataclass
class Demonstration:
    """One (S_d, D_d, C_d, R_d, I_d) record."""
    id: str
    project: str = ""
    label: str = ""
    description: str = ""
    category: str = ""
    summary: str = ""
    rounds: int = 0
    dockerfile: str = ""
    error: str = ""
    repair_diffs: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict) -> "Demonstration":
        return cls(
            id=str(raw.get("id", "")),
            project=str(raw.get("project", "")),
            label=str(raw.get("label", "")),
            description=str(raw.get("description", "")),
            category=str(raw.get("category", "")),
            summary=str(raw.get("summary", "")),
            rounds=int(raw.get("rounds") or 0),
            dockerfile=str(raw.get("dockerfile", "")),
            error=str(raw.get("error", "")),
            repair_diffs=[str(d) for d in (raw.get("repair_diffs") or [])],
        )

    def as_document(self) -> str:
        """The text that gets embedded — must match build_query()'s layout."""
        return build_query(self.dockerfile, self.error)


@dataclass
class RetrievalHit:
    demonstration: Demonstration
    score: float


def build_query(dockerfile: str, error: str) -> str:
    """Compose the retrieval document from static and dynamic features.

    The error leads because it is the more discriminative signal; the
    Dockerfile follows for context. Records and queries must use this same
    layout or cosine similarity compares different things.
    """
    parts = []
    if error.strip():
        parts.append(error.strip())
    if dockerfile.strip():
        parts.append("** DOCKERFILE **: \n" + dockerfile.strip())
    return "\n\n".join(parts)


class KnowledgeBase:
    """Loads the corpus, embeds it once, and answers similarity queries."""

    def __init__(self, demonstrations: list[Demonstration]) -> None:
        self.demonstrations = demonstrations
        self._vectors: list[list[float]] = []
        self._embedder_name: str = ""

    # -- construction ------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path | None = None) -> "KnowledgeBase":
        corpus = Path(path) if path else DEFAULT_CORPUS
        if not corpus.is_file():
            raise FileNotFoundError(f"Demonstration corpus not found: {corpus}")

        records: list[Demonstration] = []
        with corpus.open(encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(Demonstration.from_dict(json.loads(line)))
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"{corpus}:{line_no} is not valid JSON: {exc}"
                    ) from None
        return cls(records)

    def __len__(self) -> int:
        return len(self.demonstrations)

    @property
    def is_indexed(self) -> bool:
        return len(self._vectors) == len(self.demonstrations) and bool(self._vectors)

    # -- indexing ----------------------------------------------------------

    def index(
        self,
        embedder: "Embedder",
        cache_path: str | Path | None = DEFAULT_CACHE,
        progress: Callable[[str, str], None] | None = None,
    ) -> None:
        """Embed every record, reusing a cached index when it is still valid."""
        emit = progress or (lambda _s, _m: None)
        cache = Path(cache_path) if cache_path else None
        fingerprint = self._fingerprint(embedder.name)

        if cache and self._load_cache(cache, fingerprint):
            emit("retrieve", f"Loaded {len(self._vectors)} cached embeddings.")
            return

        documents = [d.as_document() for d in self.demonstrations]
        emit("retrieve", f"Embedding {len(documents)} demonstrations…")
        self._vectors = embedder.embed(documents, task_type=TASK_DOCUMENT)
        self._embedder_name = embedder.name

        if len(self._vectors) != len(self.demonstrations):
            raise RuntimeError(
                f"Embedder returned {len(self._vectors)} vectors for "
                f"{len(self.demonstrations)} demonstrations."
            )

        if cache:
            self._write_cache(cache, fingerprint)
        emit("retrieve", f"Indexed {len(self._vectors)} demonstrations.")

    # -- retrieval ---------------------------------------------------------

    def retrieve(
        self,
        query: str,
        embedder: "Embedder",
        k: int = DEFAULT_TOP_K,
        exclude_ids: Iterable[str] = (),
    ) -> list[RetrievalHit]:
        """Return the *k* most similar demonstrations, best first."""
        if not self.is_indexed:
            raise RuntimeError("KnowledgeBase.index() must be called before retrieve().")
        if not query.strip() or k <= 0:
            return []

        query_vector = embedder.embed([query], task_type=TASK_QUERY)[0]
        blocked = set(exclude_ids)

        scored = [
            RetrievalHit(demonstration=demo, score=cosine(query_vector, vector))
            for demo, vector in zip(self.demonstrations, self._vectors)
            if demo.id not in blocked
        ]
        scored.sort(key=lambda hit: hit.score, reverse=True)
        return scored[:k]

    # -- cache -------------------------------------------------------------

    def _fingerprint(self, embedder_name: str) -> str:
        """Invalidates the cache when the corpus or the embedder changes."""
        import hashlib

        digest = hashlib.sha256()
        digest.update(embedder_name.encode())
        for demo in self.demonstrations:
            digest.update(demo.id.encode())
        digest.update(str(len(self.demonstrations)).encode())
        return digest.hexdigest()[:16]

    def _load_cache(self, cache: Path, fingerprint: str) -> bool:
        if not cache.is_file():
            return False
        try:
            payload = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if payload.get("fingerprint") != fingerprint:
            return False
        vectors = payload.get("vectors") or []
        if len(vectors) != len(self.demonstrations):
            return False
        self._vectors = [[float(x) for x in v] for v in vectors]
        self._embedder_name = str(payload.get("embedder", ""))
        return True

    def _write_cache(self, cache: Path, fingerprint: str) -> None:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(cache.suffix + ".tmp")
            tmp.write_text(
                json.dumps({
                    "fingerprint": fingerprint,
                    "embedder": self._embedder_name,
                    # 6 decimals keeps the file a third of the size with no
                    # measurable effect on cosine ordering.
                    "vectors": [[round(x, 6) for x in v] for v in self._vectors],
                }),
                encoding="utf-8",
            )
            os.replace(tmp, cache)
        except OSError:
            pass   # a cache miss is not worth failing a run over
