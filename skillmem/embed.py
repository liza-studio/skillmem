"""Local sentence-embedding layer for semantic recall.

Sovereign by design: runs a multilingual ONNX model on CPU via
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
from functools import lru_cache

log = logging.getLogger("skillmem.embed")

# Multilingual Granite, CLS-pooled, official IBM INT8 ONNX weights.
MODEL_NAME = "ibm-granite/granite-embedding-97m-multilingual-r2"
EMBED_CONTEXT = 512  # Including special tokens.
DIM = 384


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
    dir instead so the model download survives reboots.
    """
    override = os.environ.get("SKILLMEM_MODEL_CACHE")
    if override:
        return override
    from platformdirs import user_cache_dir

    return os.path.join(user_cache_dir("skillmem"), "models")


# Hooks and the MCP server load the model from the cache only. A cold cache
# used to download weights inside a 10 s hook: every prompt hung for the full
# timeout and recalled nothing, and each kill left a partial blob behind.
# `skillmem doctor`, `reindex-embeddings` and the installer fetch it.
_DOWNLOAD = False


def allow_download() -> None:
    """Let this process fetch the model if the cache lacks it."""
    global _DOWNLOAD
    _DOWNLOAD = True
    _model.cache_clear()


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
        from fastembed.common.model_description import ModelSource, PoolingType

        if not any(m["model"] == MODEL_NAME for m in TextEmbedding.list_supported_models()):
            TextEmbedding.add_custom_model(
                model=MODEL_NAME, pooling=PoolingType.CLS, normalization=True,
                sources=ModelSource(hf=MODEL_NAME), dim=DIM,
                model_file="onnx/model_quint8_avx2.onnx",
            )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model = TextEmbedding(MODEL_NAME, cache_dir=model_cache_dir(),
                                  local_files_only=not _DOWNLOAD)
            model.model.tokenizer.enable_truncation(max_length=EMBED_CONTEXT)
            return model
    except Exception as exc:  # model download/load failure
        log.warning("could not load embedding model %s: %s%s", MODEL_NAME, exc,
                    "" if _DOWNLOAD else " — `skillmem doctor` downloads it")
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

        vec = np.asarray(next(iter(model.embed([text]))), dtype="float32")
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec = vec / norm
        return vec.tobytes()
    except Exception as exc:  # never let embedding break a write/read
        log.warning("embed_text failed: %s", exc)
        return None


def pack_query(text: str) -> bytes | None:
    """Embed a search query (same space as documents for this model)."""
    return embed_text(text)
