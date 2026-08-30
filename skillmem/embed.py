"""Local sentence-embedding layer for semantic recall.

Sovereign by design: runs a small multilingual ONNX model on CPU via
``fastembed`` — no external API, no key, $0. Used to add a vector-similarity
signal alongside the existing BM25/FTS5 lexical search (fused via RRF in
storage.search_hybrid). Cross-lingual: a Russian query matches an English
skill and vice-versa, which plain Snowball-BM25 cannot do.

Everything here degrades gracefully: if ``fastembed`` is not installed or the
model cannot load, ``embed_text`` returns ``None`` and recall silently falls
back to pure BM25. Set ``MEM_SEMANTIC=0`` to hard-disable the vector path.
"""

from __future__ import annotations

import logging
import os
import struct
from functools import lru_cache

log = logging.getLogger("skillmem.embed")

# 384-dim multilingual model, ~220 MB, mean-pooled. Chosen over e5-large
# (1024-dim/2.24 GB) for footprint; validated on an internal cross-lingual
# retrieval bench (RU query -> EN doc) before adoption.
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DIM = 384

# Cap embedded text — these models truncate ~512 tokens anyway, and our bodies
# can be multi-KB. Title+lead carries the semantic signal.
_MAX_CHARS = 1600


def semantic_enabled() -> bool:
    """False when the operator hard-disabled the vector path."""
    return os.environ.get("MEM_SEMANTIC", "1") != "0"


def doc_text(title: str, body: str) -> str:
    """Build the text that represents a memory item for embedding.

    The title is the distilled meaning of a skill/fact; the body is supporting
    detail that dilutes it. Weighting the title ~3x measurably lifts the right
    item to rank 1 on cross-lingual queries (deploy bench, 2026-06-09: rank
    8→1) without needing a heavier model.
    """
    title = (title or "").strip()
    head = f"{title}. {title}. {title}.\n" if title else ""
    return head + (body or "")


def model_cache_dir() -> str:
    """Persistent home for the ONNX weights.

    fastembed defaults to ``tempfile.gettempdir()`` — on macOS that is a
    per-boot ``/var/folders/...`` path the OS purges, which silently drops
    recall back to BM25 once the weights disappear. Pin it to the user cache
    dir instead so the ~220 MB download survives reboots.
    """
    override = os.environ.get("SKILLMEM_MODEL_CACHE")
    if override:
        return override
    from platformdirs import user_cache_dir

    return os.path.join(user_cache_dir("skillmem"), "models")


@lru_cache(maxsize=1)
def _model():
    """Lazily construct the embedder once per process. Returns None on failure."""
    if not semantic_enabled():
        return None
    import warnings

    try:
        from fastembed import TextEmbedding  # heavy import, deferred
    except Exception as exc:  # pragma: no cover - env without fastembed
        log.info("fastembed unavailable, semantic recall off: %s", exc)
        return None
    try:
        # The mean-pooling notice for this model is expected (it's the correct
        # pooling) — silence it so it doesn't spam the MCP/CLI on every load.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return TextEmbedding(MODEL_NAME, cache_dir=model_cache_dir())
    except Exception as exc:  # model download/load failure
        log.warning("could not load embedding model %s: %s", MODEL_NAME, exc)
        return None


def available() -> bool:
    """True if the model is loadable (does the lazy init)."""
    return _model() is not None


def embed_text(text: str) -> bytes | None:
    """Return an L2-normalized float32 vector as packed bytes, or None.

    Bytes (not a numpy array) so storage can write a BLOB without importing
    numpy. ``search_hybrid`` unpacks the whole column once per query.
    """
    model = _model()
    if model is None or not text:
        return None
    try:
        import numpy as np

        vec = np.asarray(next(iter(model.embed([text[:_MAX_CHARS]]))), dtype="float32")
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec = vec / norm
        return vec.tobytes()
    except Exception as exc:  # never let embedding break a write/read
        log.warning("embed_text failed: %s", exc)
        return None


def unpack(blob: bytes | None):
    """bytes -> numpy float32 array (already normalized). None passes through."""
    if not blob:
        return None
    import numpy as np

    return np.frombuffer(blob, dtype="float32")


def pack_query(text: str) -> bytes | None:
    """Embed a search query (same space as documents for this model)."""
    return embed_text(text)
