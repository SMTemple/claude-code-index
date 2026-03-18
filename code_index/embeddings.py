"""Sentence-transformers embedding wrapper with singleton model loading and timeout protection."""

import os
import sys
import threading
from typing import List, Optional

_model = None
_model_lock = threading.Lock()
_model_load_failed = False

# Timeout for model loading (seconds) — covers download + init
MODEL_LOAD_TIMEOUT = int(os.environ.get('CODE_INDEX_MODEL_TIMEOUT', 120))
# Timeout for a single embedding batch (seconds)
EMBED_BATCH_TIMEOUT = int(os.environ.get('CODE_INDEX_EMBED_TIMEOUT', 300))


class EmbeddingError(Exception):
    """Raised when embedding operations fail or time out."""
    pass


def _get_model(timeout=None):
    """Get the singleton model instance.

    Args:
        timeout: Max seconds to wait for the lock (None = wait forever).
                 Use a positive value so callers fail fast instead of hanging
                 when the prewarm thread is still loading the model.
    """
    global _model, _model_load_failed
    if _model_load_failed:
        raise EmbeddingError(
            "Model loading previously failed. Restart the server to retry."
        )
    # Fast path — model already loaded, no lock needed
    if _model is not None:
        return _model
    # Acquire lock with optional timeout so searches don't hang during prewarm
    if timeout is not None:
        acquired = _model_lock.acquire(timeout=timeout)
    else:
        acquired = _model_lock.acquire()
    if not acquired:
        raise EmbeddingError(
            "Embedding model is still loading (prewarm in progress). "
            "Try again in a few seconds, or run reindex via Bash to trigger model load."
        )
    try:
        if _model is None:
            try:
                from sentence_transformers import SentenceTransformer
                _model = SentenceTransformer('all-MiniLM-L6-v2')
            except ImportError:
                _model_load_failed = True
                raise EmbeddingError(
                    "sentence-transformers is not installed. "
                    "Run: uv pip install sentence-transformers"
                )
            except Exception as e:
                _model_load_failed = True
                raise EmbeddingError(f"Failed to load embedding model: {e}")
    finally:
        _model_lock.release()
    return _model


def _load_model_with_timeout(timeout: int) -> bool:
    """Attempt to load the model with a timeout. Returns True on success."""
    result = {'success': False, 'error': None}

    def _load():
        try:
            _get_model()
            result['success'] = True
        except Exception as e:
            result['error'] = e

    t = threading.Thread(target=_load, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        global _model_load_failed
        _model_load_failed = True
        raise EmbeddingError(
            f"Model loading timed out after {timeout}s. "
            "This usually means a network issue downloading the model. "
            "Try again or pre-download the model."
        )
    if result['error']:
        raise result['error']
    return result['success']


def embed_text(text: str, timeout: int = 10) -> List[float]:
    """Embed a single text string. Raises EmbeddingError on failure.

    Args:
        timeout: Max seconds to wait for the model lock (prevents hanging
                 if prewarm is still in progress). Default 10s.
    """
    try:
        model = _get_model(timeout=timeout)
        embedding = model.encode([text], show_progress_bar=False)
        return embedding[0].tolist()
    except EmbeddingError:
        raise
    except Exception as e:
        raise EmbeddingError(f"Failed to embed text: {e}")


def embed_batch(texts: List[str], timeout: int = EMBED_BATCH_TIMEOUT,
                 progress_callback=None, batch_size: int = 64) -> List[List[float]]:
    """Embed a batch of texts. Raises EmbeddingError on failure or timeout.

    Args:
        progress_callback: Optional callable(current, total) called after each
                           internal batch so callers can report progress.
        batch_size: Number of texts per internal encode call (default 64).
    """
    if not texts:
        return []

    result = {'embeddings': None, 'error': None}

    def _do_embed():
        try:
            model = _get_model()
            all_embs = []
            total = len(texts)
            for start in range(0, total, batch_size):
                chunk = texts[start:start + batch_size]
                embs = model.encode(chunk, show_progress_bar=False, batch_size=batch_size)
                all_embs.extend(e.tolist() for e in embs)
                if progress_callback:
                    progress_callback(min(start + len(chunk), total), total)
            result['embeddings'] = all_embs
        except Exception as e:
            result['error'] = e

    t = threading.Thread(target=_do_embed, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        raise EmbeddingError(
            f"Embedding {len(texts)} chunks timed out after {timeout}s. "
            "Try running a full reindex with fewer files or increase "
            "CODE_INDEX_EMBED_TIMEOUT."
        )
    if result['error']:
        raise EmbeddingError(f"Embedding failed: {result['error']}")
    return result['embeddings']


def prewarm_model():
    """Load the model into memory now so the first reindex doesn't pay the load cost.
    Silently fails — the model will be loaded on first use if prewarm fails."""
    try:
        _load_model_with_timeout(MODEL_LOAD_TIMEOUT)
    except Exception:
        # Prewarm is best-effort; reset the failure flag so first real use can retry
        global _model_load_failed
        _model_load_failed = False
