"""Embedding generation and canonical text for durable Vellum memories."""

from __future__ import annotations

import hashlib
import json
import math
import os
from typing import Iterable, Protocol

import requests

EMBEDDING_MODEL = os.getenv("VELLUM_EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIMENSIONS = 1536


class EmbeddingUnavailable(RuntimeError):
    """Raised when semantic retrieval cannot safely generate an embedding."""


class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbeddingProvider:
    """Small HTTP client to avoid coupling retrieval to a particular SDK version."""

    def __init__(self, api_key: str | None = None, model: str = EMBEDDING_MODEL):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.api_key:
            raise EmbeddingUnavailable("OPENAI_API_KEY is not configured")
        if not texts:
            return []
        try:
            response = requests.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "input": texts},
                timeout=20,
            )
            response.raise_for_status()
            vectors = [item["embedding"] for item in response.json()["data"]]
        except requests.RequestException as exc:
            raise EmbeddingUnavailable(f"embedding request failed: {exc}") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingUnavailable("embedding provider returned an invalid payload") from exc

        for vector in vectors:
            if len(vector) != EMBEDDING_DIMENSIONS or not all(math.isfinite(value) for value in vector):
                raise EmbeddingUnavailable("embedding has an invalid dimension or value")
        return vectors


def get_embedding_provider() -> EmbeddingProvider:
    return OpenAIEmbeddingProvider()


def embedding_literal(vector: list[float]) -> str:
    """Return pgvector's text input format after dimension validation."""
    if len(vector) != EMBEDDING_DIMENSIONS:
        raise ValueError(f"expected {EMBEDDING_DIMENSIONS} embedding dimensions")
    return "[" + ",".join(format(float(value), ".9g") for value in vector) + "]"


def source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def episode_embedding_text(episode: dict) -> str:
    """Embed distilled episode semantics, not database IDs or retrieval scores."""
    summary = episode.get("summary") or episode.get("content") or ""
    entities = episode.get("entities") or []
    tags = episode.get("tags") or []
    return "\n".join((
        f"Episode type: {episode.get('event_type') or 'unknown'}",
        f"Summary: {summary}",
        f"Entities: {', '.join(map(str, entities))}",
        f"Tags: {', '.join(map(str, tags))}",
    ))[:8000]


def belief_embedding_text(belief: dict) -> str:
    """Embed a canonical proposition so retrieval matches its meaning."""
    return "\n".join((
        f"Namespace: {belief.get('namespace') or 'general'}",
        f"Subject: {belief.get('subject') or ''}",
        f"Predicate: {belief.get('predicate') or ''}",
        f"Object: {belief.get('object_value') or ''}",
    ))[:8000]
