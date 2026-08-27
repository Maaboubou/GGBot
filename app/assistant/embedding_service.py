"""Lazy CPU-only embeddings for Assistant conversational memory."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Iterable, List, Optional

import numpy as np

logger = logging.getLogger(__name__)


class LocalEmbeddingService:
    """Load FastEmbed once and keep query inference off the network path."""

    def __init__(
        self,
        *,
        model_name: str = "BAAI/bge-small-zh-v1.5",
        cache_dir: str | Path = "data/models/fastembed",
        threads: int = 4,
    ) -> None:
        self.model_name = str(model_name)
        self.cache_dir = Path(cache_dir)
        self.threads = max(1, min(8, int(threads or 4)))
        self._model = None
        self._load_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self._load_attempted = False
        self._last_attempt_monotonic = 0.0
        self._last_error = ""

    @property
    def ready(self) -> bool:
        return self._model is not None

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def can_attempt_load(self) -> bool:
        """Avoid retrying a missing model on every incoming chat message."""
        return (
            self._model is None
            and (
                not self._load_attempted
                or time.monotonic() - self._last_attempt_monotonic >= 600
            )
        )

    def warmup(self) -> bool:
        if self._model is not None:
            return True
        with self._load_lock:
            if self._model is not None:
                return True
            self._load_attempted = True
            self._last_attempt_monotonic = time.monotonic()
            try:
                from fastembed import TextEmbedding

                self.cache_dir.mkdir(parents=True, exist_ok=True)
                self._model = TextEmbedding(
                    model_name=self.model_name,
                    cache_dir=str(self.cache_dir),
                    threads=self.threads,
                    providers=["CPUExecutionProvider"],
                )
                # Force lazy implementations to initialize all runtime state now.
                list(self._model.query_embed("记忆检索预热"))
                logger.info(
                    "🧭 Local embedding model ready: %s threads=%s",
                    self.model_name,
                    self.threads,
                )
                self._last_error = ""
                return True
            except Exception as exc:
                self._model = None
                self._last_error = str(exc)
                logger.warning(
                    "⚠️ Local embedding model unavailable; keyword retrieval remains active: %s",
                    exc,
                )
                return False

    @staticmethod
    def _normalize(vector: np.ndarray) -> np.ndarray:
        value = np.asarray(vector, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(value))
        return value / norm if norm > 0 else value

    def embed_query(self, text: str) -> Optional[np.ndarray]:
        """Embed a query only when the model is already warm."""
        if self._model is None or not str(text or "").strip():
            return None
        with self._inference_lock:
            try:
                values = list(self._model.query_embed(str(text)))
                return self._normalize(values[0]) if values else None
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning("⚠️ Query embedding failed: %s", exc)
                return None

    def embed_passages(self, texts: Iterable[str]) -> List[Optional[np.ndarray]]:
        values = [str(text or "").strip() for text in texts]
        if not values:
            return []
        if self._model is None and not self.warmup():
            return [None] * len(values)
        with self._inference_lock:
            try:
                vectors = list(self._model.passage_embed(values))
                return [self._normalize(vector) for vector in vectors]
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning("⚠️ Passage embedding failed: %s", exc)
                return [None] * len(values)
