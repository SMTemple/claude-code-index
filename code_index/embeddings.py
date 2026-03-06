"""Sentence-transformers embedding wrapper with singleton model loading."""

from typing import List

_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer('all-MiniLM-L6-v2')
    return _model


def embed_text(text: str) -> List[float]:
    model = _get_model()
    embedding = model.encode([text], show_progress_bar=False)
    return embedding[0].tolist()


def embed_batch(texts: List[str]) -> List[List[float]]:
    if not texts:
        return []
    model = _get_model()
    embeddings = model.encode(texts, show_progress_bar=False, batch_size=64)
    return [e.tolist() for e in embeddings]


def prewarm_model():
    """Load the model into memory now so the first reindex doesn't pay the load cost."""
    _get_model()
