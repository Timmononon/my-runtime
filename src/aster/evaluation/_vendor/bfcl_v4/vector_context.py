"""Explicit, per-context encoder admission; this module never loads a model."""
from contextlib import contextmanager
from contextvars import ContextVar

_ENCODER = ContextVar("aster_bfcl_v4_vector_encoder", default=None)
MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
DIMENSION = 384


def validate_vector_encoder(encoder):
    if (encoder is None or getattr(encoder, "model_id", None) != MODEL_ID
            or getattr(encoder, "local_files_only", None) is not True
            or getattr(encoder, "dimension", None) != DIMENSION
            or not callable(getattr(encoder, "encode", None))):
        raise ValueError("Vector memory requires an explicit matching offline encoder")
    return encoder


def current_vector_encoder():
    encoder = _ENCODER.get()
    if encoder is None:
        raise ValueError("Vector memory has no admitted encoder in this context")
    return validate_vector_encoder(encoder)


@contextmanager
def admitted_vector_encoder(encoder):
    """Validate the trusted caller's adapter without loading or calling it."""
    token = _ENCODER.set(validate_vector_encoder(encoder))
    try:
        yield
    finally:
        _ENCODER.reset(token)


class _EncoderProxy:
    def encode(self, texts, *, normalize_embeddings=True):
        encoder = current_vector_encoder()
        if normalize_embeddings is not True or not isinstance(texts, list) or any(not isinstance(text, str) for text in texts):
            raise ValueError("Official vector encoding requires a normalized text-list request")
        result = encoder.encode(texts, normalize_embeddings=True)
        # Validate before the unchanged upstream VectorStore passes data to FAISS.
        import numpy as np

        vectors = np.asarray(result)
        if (vectors.shape != (len(texts), DIMENSION) or vectors.dtype.kind != "f"
                or not np.isfinite(vectors).all()):
            raise ValueError("Offline encoder returned an invalid vector matrix")
        return result


ENCODER = _EncoderProxy()
