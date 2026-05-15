"""
Retrieve the most relevant JD sections for a given CV chunk.

Strategy: embed the CV text, query Pinecone, return top-k chunk texts.
Falls back to returning the full raw JD if Pinecone is unreachable.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from tenacity import retry, stop_after_attempt, wait_exponential

from .ingestion import _embed_chunks, _get_index, _role_to_namespace

logger = logging.getLogger(__name__)

_TOP_K = 5

# Below this character count we just feed the whole JD to the model — chunking
# and Pinecone lookup add latency and cost without benefit on short JDs.
# ~6000 chars ≈ 1500 tokens, well within Claude's context window.
_DIRECT_JD_THRESHOLD = int(os.getenv("JD_RAG_THRESHOLD_CHARS", "6000"))


def retrieve_jd_context(
    role_name: str,
    cv_text: str,
    fallback_jd: Optional[str] = None,
) -> str:
    """
    Return a string containing the most relevant JD sections for this CV.
    Short JDs bypass Pinecone entirely (the full text fits in the prompt).
    If Pinecone fails on a long JD, falls back to a truncated raw JD.
    """
    if fallback_jd and len(fallback_jd) <= _DIRECT_JD_THRESHOLD:
        logger.debug(
            "JD for role '%s' is %d chars (<= %d) — skipping RAG",
            role_name, len(fallback_jd), _DIRECT_JD_THRESHOLD,
        )
        return fallback_jd

    try:
        return _query_pinecone(role_name, cv_text)
    except Exception as exc:
        logger.warning(
            "Pinecone retrieval failed for role '%s': %s — using fallback JD",
            role_name, exc,
        )
        if fallback_jd:
            return fallback_jd[:4000]
        return "[JD context unavailable — Pinecone connection failed]"


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _query_pinecone(role_name: str, cv_text: str) -> str:
    namespace = _role_to_namespace(role_name)

    # Embed only the first 1000 chars of the CV (enough signal, cheaper)
    query_text = cv_text[:1000]
    vectors = _embed_chunks([query_text], namespace)
    query_vector = vectors[0]["values"]

    index = _get_index()
    result = index.query(
        vector=query_vector,
        top_k=_TOP_K,
        namespace=namespace,
        include_metadata=True,
    )

    chunks = [
        match["metadata"]["text"]
        for match in result.get("matches", [])
        if match.get("metadata", {}).get("text")
    ]

    if not chunks:
        raise ValueError(f"No vectors found in namespace '{namespace}'")

    return "\n\n---\n\n".join(chunks)
