"""Embedding wrapper.

Backend selection (preference order, first-available wins):
  1. fastembed             — ONNX runtime. Loads in ~1.3s vs ~8.7s for PyTorch.
                              Inference is ~70 chunks/sec on x86 CPU. Break-even
                              vs sentence-transformers is ~600 chunks per cold
                              build; below that fastembed wins on total wall time.
                              Supports CUDA via the fastembed-gpu extra.
  2. sentence-transformers — PyTorch with auto-detected CUDA/MPS device. ~8.7s
                              cold load but ~437 chunks/sec inference (MKL/oneDNN
                              on x86). Better for full rebuilds of large repos
                              (>600 chunks). Override with
                              CODE_INDEX_BACKEND=sentence_transformers.

Same MiniLM-L6-v2 weights either way (384-dim output) so cached embedding hashes
remain compatible across backends. Override the order with CODE_INDEX_BACKEND=
fastembed or CODE_INDEX_BACKEND=sentence_transformers. Override the model with
CODE_INDEX_MODEL=<hf-name>. Override batch size with CODE_INDEX_BATCH_SIZE=<n>.
"""

import os
import threading
from typing import List

# ─── Module state ───────────────────────────────────────────────────────────

_model = None
_model_kind = None       # 'fastembed' or 'sentence_transformers' once loaded
_model_device = None     # human-readable device string for diagnostics
_model_lock = threading.Lock()
_model_load_failed = False

# ─── Tunables (env-override) ────────────────────────────────────────────────

# HF-style model name. Both backends understand this exact string.
MODEL_NAME = os.environ.get('CODE_INDEX_MODEL', 'sentence-transformers/all-MiniLM-L6-v2')
# Per-encode batch size. Empirically on a 6-physical-core x86 CPU with MiniLM
# (~80MB), 256 is the sweet spot (~25% faster than 128, ~55% faster than 64);
# 384+ shows diminishing returns. Drop to 64-128 only if memory-constrained.
DEFAULT_BATCH_SIZE = int(os.environ.get('CODE_INDEX_BATCH_SIZE', 256))
# Timeout for model loading (seconds) — covers download + init.
MODEL_LOAD_TIMEOUT = int(os.environ.get('CODE_INDEX_MODEL_TIMEOUT', 180))
# Timeout for an entire embed_batch call (seconds).
EMBED_BATCH_TIMEOUT = int(os.environ.get('CODE_INDEX_EMBED_TIMEOUT', 600))
# Force a specific backend for testing: 'fastembed' or 'sentence_transformers'.
FORCE_BACKEND = os.environ.get('CODE_INDEX_BACKEND', '').strip().lower() or None
# Persistent on-disk cache for the fastembed ONNX model. fastembed defaults to
# {tempfile.gettempdir()}/fastembed_cache — under %TEMP% on Windows, which temp
# cleanup periodically wipes. A wiped/partial model.onnx makes _load_fastembed
# fail, forcing the ~7x-slower sentence-transformers fallback (~10s vs ~1.5s) on
# every cold start. Pin the cache next to the tool (survives reboots/cleanups),
# or honor an explicit FASTEMBED_CACHE_PATH override.
FASTEMBED_CACHE_DIR = os.environ.get('FASTEMBED_CACHE_PATH') or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.model_cache')


class EmbeddingError(Exception):
    """Raised when embedding operations fail or time out."""
    pass


# ─── Backend loaders ────────────────────────────────────────────────────────

def _detect_torch_device() -> str:
    """Return the best torch device available: 'cuda' > 'mps' > 'cpu'."""
    try:
        import torch
    except ImportError:
        return 'cpu'
    try:
        if torch.cuda.is_available():
            return 'cuda'
        if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            return 'mps'
    except Exception:
        pass
    return 'cpu'


def _load_fastembed():
    """Try to load fastembed. Returns (model, device_string).

    fastembed auto-selects ONNX execution providers; CUDA is used when the
    fastembed-gpu extra is installed (`uv pip install fastembed-gpu`). On a
    machine without it, fastembed runs on optimized CPU ONNX.
    """
    from fastembed import TextEmbedding
    os.makedirs(FASTEMBED_CACHE_DIR, exist_ok=True)
    model = TextEmbedding(model_name=MODEL_NAME, cache_dir=FASTEMBED_CACHE_DIR)
    # Best-effort device detection from the loaded onnxruntime session
    device = 'cpu'
    try:
        # fastembed >= 0.3 exposes the model object with a `.model` attribute
        # that wraps an InferenceSession; providers reveal CUDA usage.
        sess = getattr(model, 'model', None) or getattr(model, '_model', None)
        if sess is not None and hasattr(sess, 'get_providers'):
            providers = sess.get_providers()
            if any('CUDA' in p for p in providers):
                device = 'cuda'
    except Exception:
        pass
    return model, f'onnx-{device}'


def _load_sentence_transformers():
    """Fallback to PyTorch sentence-transformers with GPU autodetect."""
    from sentence_transformers import SentenceTransformer
    device = _detect_torch_device()
    model = SentenceTransformer(MODEL_NAME, device=device)
    return model, device


def _try_loaders_in_order():
    """Iterate (kind, loader) pairs in the configured preference order.

    Default: fastembed first (1.3s load vs 8.7s for PyTorch). sentence-transformers
    fallback is faster for full rebuilds of large repos (>600 new chunks per pass)
    — force it with CODE_INDEX_BACKEND=sentence_transformers.
    """
    if FORCE_BACKEND == 'fastembed':
        yield 'fastembed', _load_fastembed
    elif FORCE_BACKEND == 'sentence_transformers':
        yield 'sentence_transformers', _load_sentence_transformers
    else:
        yield 'fastembed', _load_fastembed
        yield 'sentence_transformers', _load_sentence_transformers


def _get_model(timeout=None):
    """Get the singleton model instance. Returns the model object."""
    global _model, _model_kind, _model_device, _model_load_failed
    if _model_load_failed:
        raise EmbeddingError(
            "Model loading previously failed. Restart the server to retry."
        )
    if _model is not None:
        return _model
    if timeout is not None:
        acquired = _model_lock.acquire(timeout=timeout)
    else:
        acquired = _model_lock.acquire()
    if not acquired:
        raise EmbeddingError(
            "Embedding model is still loading (prewarm in progress). "
            "Try again in a few seconds."
        )
    try:
        if _model is None:
            errors = []
            for kind, loader in _try_loaders_in_order():
                try:
                    _model, _model_device = loader()
                    _model_kind = kind
                    break
                except ImportError as e:
                    errors.append(f"{kind}: not installed ({e})")
                except Exception as e:
                    errors.append(f"{kind}: load failed ({e})")
            if _model is None:
                _model_load_failed = True
                raise EmbeddingError(
                    "No embedding backend could be loaded.\n"
                    "  Install one (preferred first on x86 CPU):\n"
                    "    uv pip install sentence-transformers  # default; auto-uses CUDA/MPS if available\n"
                    "    uv pip install fastembed              # ONNX fallback; no torch dep\n"
                    f"  Errors: {' | '.join(errors)}"
                )
    finally:
        _model_lock.release()
    return _model


def _encode(texts: List[str], batch_size: int) -> List[List[float]]:
    """Single encode pass. Dispatches by backend kind."""
    model = _model  # already loaded by caller
    if _model_kind == 'fastembed':
        # fastembed.embed returns a generator of numpy arrays
        embs = list(model.embed(texts, batch_size=batch_size))
    else:  # sentence_transformers
        embs = model.encode(
            texts, show_progress_bar=False, batch_size=batch_size,
            convert_to_numpy=True,
        )
    return [e.tolist() for e in embs]


# ─── Public API ─────────────────────────────────────────────────────────────

def embed_text(text: str, timeout: int = 10) -> List[float]:
    """Embed a single text string."""
    try:
        _get_model(timeout=timeout)
        return _encode([text], batch_size=1)[0]
    except EmbeddingError:
        raise
    except Exception as e:
        raise EmbeddingError(f"Failed to embed text: {e}")


def embed_batch(texts: List[str], timeout: int = EMBED_BATCH_TIMEOUT,
                progress_callback=None, batch_size: int = None) -> List[List[float]]:
    """Embed a batch of texts. Raises EmbeddingError on failure or timeout.

    Args:
        progress_callback: Optional callable(current, total) called after each
                           internal batch so callers can report progress.
        batch_size: Number of texts per internal encode call. Defaults to
                    DEFAULT_BATCH_SIZE (256).
    """
    if not texts:
        return []
    if batch_size is None:
        batch_size = DEFAULT_BATCH_SIZE

    result = {'embeddings': None, 'error': None}

    def _do_embed():
        try:
            _get_model(timeout=60)
            all_embs = []
            total = len(texts)
            for start in range(0, total, batch_size):
                chunk = texts[start:start + batch_size]
                all_embs.extend(_encode(chunk, batch_size=batch_size))
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
            "Increase CODE_INDEX_EMBED_TIMEOUT or reduce batch size."
        )
    if result['error']:
        raise EmbeddingError(f"Embedding failed: {result['error']}")
    return result['embeddings']


def prewarm_model():
    """Load the model into memory now so the first reindex doesn't pay the load
    cost. Silently fails — the model will be loaded on first use if prewarm fails."""
    result = {'error': None}

    def _load():
        try:
            _get_model()
        except Exception as e:
            result['error'] = e

    t = threading.Thread(target=_load, daemon=True)
    t.start()
    t.join(timeout=MODEL_LOAD_TIMEOUT)

    if t.is_alive():
        # Prewarm timed out — the loader thread is still running in the background.
        # _model_load_failed stays False so the first real embed call will wait
        # on the lock (up to 60s in _get_model) for the loader to complete,
        # rather than failing immediately or starting a duplicate load.
        pass


def get_backend_info() -> str:
    """Diagnostic helper. Returns string like 'fastembed (onnx-cuda)' once loaded."""
    if _model is None:
        return 'not loaded'
    return f'{_model_kind} ({_model_device})'
