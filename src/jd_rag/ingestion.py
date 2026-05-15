"""
Job Description ingestion into Pinecone.

Each role gets its own namespace inside a shared index.
Embeddings use OpenAI text-embedding-3-small (1536 dimensions, cheap and accurate).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 400       # characters per chunk
_CHUNK_OVERLAP = 80     # overlap to preserve context across boundaries
_EMBED_MODEL = "text-embedding-3-small"
_EMBED_DIM = 1536


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────


def ingest_job_description(role_name: str, jd_text: str) -> int:
    """
    Chunk the JD, embed each chunk, and upsert into Pinecone under the role's
    namespace.  Returns the number of vectors upserted.
    """
    chunks = _chunk_text(jd_text)
    if not chunks:
        raise ValueError(f"JD for role '{role_name}' produced no chunks.")

    namespace = _role_to_namespace(role_name)
    vectors = _embed_chunks(chunks, namespace)
    _upsert_to_pinecone(namespace, vectors)

    logger.info("Ingested %d chunks for role '%s' into namespace '%s'", len(chunks), role_name, namespace)
    return len(chunks)


def delete_role_vectors(role_name: str) -> None:
    """Remove all vectors for a role (called by GDPR purge if role is retired)."""
    namespace = _role_to_namespace(role_name)
    index = _get_index()
    index.delete(delete_all=True, namespace=namespace)
    logger.info("Deleted all vectors for namespace '%s'", namespace)


# ──────────────────────────────────────────────────────────────────────────────
# Chunking
# ──────────────────────────────────────────────────────────────────────────────


def _chunk_text(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + _CHUNK_SIZE, len(text))
        # Don't break in the middle of a word
        if end < len(text):
            boundary = text.rfind(" ", start, end)
            if boundary > start:
                end = boundary
        chunks.append(text[start:end].strip())
        start = end - _CHUNK_OVERLAP
    return [c for c in chunks if c]


# ──────────────────────────────────────────────────────────────────────────────
# Embedding
# ──────────────────────────────────────────────────────────────────────────────


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _embed_chunks(chunks: list[str], namespace: str) -> list[dict]:
    from openai import OpenAI  # noqa: PLC0415

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    response = client.embeddings.create(model=_EMBED_MODEL, input=chunks)
    return [
        {
            "id": f"{namespace}-{i}",
            "values": item.embedding,
            "metadata": {"text": chunks[i], "chunk_index": i},
        }
        for i, item in enumerate(response.data)
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Pinecone
# ──────────────────────────────────────────────────────────────────────────────


def _get_index():
    from pinecone import Pinecone, ServerlessSpec  # noqa: PLC0415

    pc = Pinecone(api_key=os.environ["PINECONE_API_KEY"])
    index_name = os.environ.get("PINECONE_INDEX_NAME", "buildright-jd-index")

    existing_names = [idx.name for idx in pc.list_indexes()]
    if index_name not in existing_names:
        logger.info("Creating Pinecone index '%s'", index_name)
        pc.create_index(
            name=index_name,
            dimension=_EMBED_DIM,
            metric="cosine",
            spec=ServerlessSpec(cloud="aws", region="us-east-1"),
        )
    return pc.Index(index_name)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _upsert_to_pinecone(namespace: str, vectors: list[dict]) -> None:
    index = _get_index()
    batch_size = 100
    for i in range(0, len(vectors), batch_size):
        index.upsert(vectors=vectors[i : i + batch_size], namespace=namespace)


def _role_to_namespace(role_name: str) -> str:
    return re.sub(r"[^a-z0-9\-]", "-", role_name.lower()).strip("-")
