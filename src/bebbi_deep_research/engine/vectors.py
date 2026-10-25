"""Embedding vectors as numpy float32 matrices and cosine scores (passage selection of pages and own sources).

A 2560-dimensional embedding takes 10 KB as float32 and about 80 KB as a Python list of floats. Scoring is a matrix
product; callers run it with `asyncio.to_thread`, so a long file does not block the event loop.
"""

from collections.abc import Sequence

import numpy as np

Matrix = np.ndarray


def unit_rows(vectors: Matrix | Sequence[Sequence[float]]) -> Matrix:
    """Return the vectors as a float32 matrix with rows scaled to length 1 (zero rows stay zero)."""
    m = np.array(vectors, dtype=np.float32, ndmin=2)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1
    m /= norms
    return m


def best_scores(chunks: Matrix, queries: Matrix) -> Matrix:
    """Return per chunk the highest cosine similarity to any query (both matrices from `unit_rows`)."""
    if not len(chunks) or not len(queries):
        return np.zeros(len(chunks), dtype=np.float32)
    return (chunks @ queries.T).max(axis=1)


def scores(vectors: Matrix | Sequence[Sequence[float]], n_queries: int) -> Matrix:
    """Score embeddings laid out as ``queries + chunks``: best cosine of each chunk to the first `n_queries` rows."""
    m = unit_rows(vectors)
    return best_scores(m[n_queries:], m[:n_queries])
