"""Embedding backends for demonstration retrieval.

FLAKIDOCK embeds the (Dockerfile, build output) query with OpenAI's
text-embedding-ada-002 (8191 token budget) and retrieves by cosine similarity.
The Gemini equivalent with a comparable budget is gemini-embedding-2 (8192),
which the already-installed google-genai SDK provides — no new dependency.

A deterministic lexical backend is included so retrieval still works, and can
be tested, without an API key or network access.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol, runtime_checkable

# Task types improve retrieval quality by embedding queries and documents
# into complementary spaces.
TASK_DOCUMENT = "RETRIEVAL_DOCUMENT"
TASK_QUERY = "RETRIEVAL_QUERY"


@runtime_checkable
class Embedder(Protocol):
    @property
    def name(self) -> str:
        """Identifies the backend, so a cached index is not reused across models."""
        ...

    def embed(self, texts: list[str], task_type: str = TASK_DOCUMENT) -> list[list[float]]:
        """Return one vector per input text, in the same order."""
        ...


class GeminiEmbedder:
    """gemini-embedding-2 via the google-genai SDK."""

    MODEL = "gemini-embedding-2"
    #: Documents per request. The SDK accepts a list of Content objects; passing
    #: a plain list of strings silently concatenates them into ONE document.
    BATCH_SIZE = 16
    #: ~8192 tokens. Corpus records are well inside this, but guard anyway.
    MAX_CHARS = 24000

    def __init__(self, model: str | None = None) -> None:
        import os

        from google import genai

        self._model = model or self.MODEL
        self._client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    @property
    def name(self) -> str:
        return f"gemini:{self._model}"

    def embed(self, texts: list[str], task_type: str = TASK_DOCUMENT) -> list[list[float]]:
        from google.genai import types

        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.BATCH_SIZE):
            chunk = texts[start:start + self.BATCH_SIZE]
            # One Content per document — a bare list[str] would be treated as
            # a single document split into parts.
            contents = [
                types.Content(parts=[types.Part(text=t[: self.MAX_CHARS] or " ")])
                for t in chunk
            ]
            response = self._client.models.embed_content(
                model=self._model,
                contents=contents,
                config=types.EmbedContentConfig(task_type=task_type),
            )
            embeddings = response.embeddings or []
            if len(embeddings) != len(chunk):
                raise RuntimeError(
                    f"Embedding backend returned {len(embeddings)} vectors for "
                    f"{len(chunk)} documents."
                )
            vectors.extend(list(e.values or []) for e in embeddings)
        return vectors


class LexicalEmbedder:
    """Hashed bag-of-words with L2 normalisation — offline fallback.

    Uses hashlib rather than the built-in hash(), which is salted per process
    and would make a cached index unusable on the next run.
    """

    def __init__(self, dimensions: int = 1024) -> None:
        self._dim = dimensions

    @property
    def name(self) -> str:
        return f"lexical:{self._dim}"

    def embed(self, texts: list[str], task_type: str = TASK_DOCUMENT) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for token in _tokenize(text):
            vec[_bucket(token, self._dim)] += 1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm:
            vec = [v / norm for v in vec]
        return vec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._\-/]*")


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if len(t) > 1]


def _bucket(token: str, dimensions: int) -> int:
    digest = hashlib.md5(token.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % dimensions


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. Returns 0.0 for a zero vector or a length mismatch."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def default_embedder() -> Embedder:
    """Gemini when an API key is present, otherwise the lexical fallback."""
    import os

    if os.environ.get("GEMINI_API_KEY", "").strip():
        return GeminiEmbedder()
    return LexicalEmbedder()
