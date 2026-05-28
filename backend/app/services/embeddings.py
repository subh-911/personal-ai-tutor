from __future__ import annotations

import asyncio
from threading import Lock
from typing import Protocol

from sentence_transformers import SentenceTransformer

from app.config import settings


class EmbeddingProvider(Protocol):
    dimension: int

    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


class HuggingFaceEmbeddingProvider:
    """Sentence-transformers `all-mpnet-base-v2` (or whatever `settings.embedding_model_name`
    points at). Lazy-loads the model into a class-level singleton on first use so test
    suites and dev servers don't pay the ~420 MB download cost until embedding is needed.

    `model.encode(...)` is synchronous; calls run in a worker thread so the event loop
    stays free.
    """

    _model: SentenceTransformer | None = None
    _load_lock: Lock = Lock()
    dimension: int = settings.embedding_dim

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or settings.embedding_model_name

    def _get_model(self) -> SentenceTransformer:
        if HuggingFaceEmbeddingProvider._model is None:
            with HuggingFaceEmbeddingProvider._load_lock:
                if HuggingFaceEmbeddingProvider._model is None:
                    model = SentenceTransformer(self.model_name)
                    # Newer sentence-transformers renamed the method; fall back to the old one.
                    getter = getattr(model, "get_embedding_dimension", None) or model.get_sentence_embedding_dimension
                    actual = getter()
                    if actual != self.dimension:
                        raise RuntimeError(
                            f"embedding model {self.model_name!r} produces {actual}-d vectors "
                            f"but pgvector column is {self.dimension}-d; either change "
                            "settings.embedding_dim (requires schema migration) or pick a "
                            f"{self.dimension}-d model."
                        )
                    HuggingFaceEmbeddingProvider._model = model
        return HuggingFaceEmbeddingProvider._model

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._get_model()
        vectors = await asyncio.to_thread(
            model.encode,
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [v.tolist() for v in vectors]


_singleton: EmbeddingProvider = HuggingFaceEmbeddingProvider()


def get_embedding_provider() -> EmbeddingProvider:
    return _singleton
