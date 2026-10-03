from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

DEFAULT_SEMANTIC_MODEL = "BAAI/bge-small-en-v1.5"
SEMANTIC_TEXT_CHARS = 2000


class SemanticUnavailableError(RuntimeError):
    """Raised when the optional local semantic dependency is unavailable."""


class LocalSemanticEmbedder:
    def __init__(
        self,
        model_name: str = DEFAULT_SEMANTIC_MODEL,
        cache_dir: str | None = None,
        *,
        threads: int = 1,
        query_cache_size: int = 256,
    ):
        try:
            from fastembed import TextEmbedding
        except ImportError as error:
            raise SemanticUnavailableError(
                "semantic retrieval requires the 'semantic' optional dependency"
            ) from error
        kwargs: dict[str, Any] = {
            "model_name": model_name,
            "threads": threads,
            "providers": ["CPUExecutionProvider"],
            "cuda": False,
        }
        if cache_dir:
            kwargs["cache_dir"] = cache_dir
        self.model_name = model_name
        self._model = TextEmbedding(**kwargs)
        self._np = self._load_numpy()
        self._query_cache_size = max(1, query_cache_size)
        self._query_cache: OrderedDict[str, bytes] = OrderedDict()

    @staticmethod
    def _load_numpy():
        try:
            import numpy as np
        except ImportError as error:
            raise SemanticUnavailableError("semantic retrieval requires numpy") from error
        return np

    def embed_text(self, text: str) -> bytes:
        key = text
        cached = self._query_cache.get(key)
        if cached is not None:
            self._query_cache.move_to_end(key)
            return cached
        embedding = self.embed_many([text])[0]
        self._query_cache[key] = embedding
        self._query_cache.move_to_end(key)
        while len(self._query_cache) > self._query_cache_size:
            self._query_cache.popitem(last=False)
        return embedding

    def embed_many(self, texts: list[str]) -> list[bytes]:
        if not texts:
            return []
        texts = [text[:SEMANTIC_TEXT_CHARS] for text in texts]
        vectors = self._np.asarray(
            list(self._model.embed(texts, batch_size=8)),
            dtype=self._np.float32,
        )
        norms = self._np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1
        vectors = vectors / norms
        return [vector.tobytes() for vector in vectors]

    def rank(
        self,
        query: str,
        candidates: Iterable[tuple[str, bytes]],
        *,
        limit: int | None = None,
    ) -> list[tuple[str, float]]:
        if limit is not None and limit <= 0:
            return []
        query_vector = self._np.frombuffer(self.embed_text(query), dtype=self._np.float32)
        ranked: list[tuple[str, float]] = []
        chunk: list[tuple[str, bytes]] = []
        chunk_size = 256

        def score_chunk() -> None:
            if not chunk:
                return
            matrix = self._np.vstack(
                [self._np.frombuffer(blob, dtype=self._np.float32) for _, blob in chunk]
            )
            scores = matrix @ query_vector
            ranked.extend(
                (candidate_id, float(score))
                for (candidate_id, _), score in zip(chunk, scores, strict=True)
            )
            chunk.clear()
            if limit is not None:
                ranked.sort(key=lambda item: (-item[1], item[0]))
                del ranked[max(1, limit):]

        for candidate in candidates:
            chunk.append(candidate)
            if len(chunk) >= chunk_size:
                score_chunk()
        score_chunk()
        if limit is None:
            ranked.sort(key=lambda item: (-item[1], item[0]))
        return ranked if limit is None else ranked[:limit]
