"""The corpus embedding snapshot: every stored chunk, loaded once and scored exactly.

Chroma's ``query(..., n_results=N)`` is an approximate top-K and is *not* used here. An
inventory that silently dropped a document because an ANN index did not surface it would
be exactly the failure the feature exists to prevent. At the measured corpus size a full
scan costs about a second and a few hundred megabytes, so exactness is affordable.

See docs/INVENTORY_SEARCH_PLAN.md sections 6.3 and 11.
"""

import threading
from dataclasses import dataclass, field

import numpy as np

from .schemas import VectorStoreUnavailable

PAGE_SIZE = 5000
CHUNK_ID_SEPARATOR = "__chunk_"


@dataclass
class CorpusEmbeddingSnapshot:
    """An immutable view of the vector store, keyed by ledger snapshot id."""

    snapshot_id: str
    chunk_ids: list[str] = field(default_factory=list)
    matrix: np.ndarray | None = None
    metadatas: list[dict] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)
    # Chunks present in the store but not vouched for by the ledger. Excluded from the
    # matrix entirely, and surfaced so coverage can report them.
    excluded_orphans: int = 0

    @property
    def dimension(self) -> int:
        return 0 if self.matrix is None else int(self.matrix.shape[1])

    @property
    def size(self) -> int:
        return len(self.chunk_ids)

    def logical_keys(self) -> list[tuple[str, str]]:
        """``(logical_kind, logical_document_id)`` for each chunk, in matrix order."""
        keys: list[tuple[str, str]] = []
        for meta in self.metadatas:
            if meta.get("kind") == "law":
                keys.append(("law", meta.get("document_id") or ""))
            else:
                keys.append(("circular", meta.get("circular_id") or ""))
        return keys

    def scores(self, query_vectors: np.ndarray) -> np.ndarray:
        """Cosine similarity of every chunk against every query vector.

        Returns shape ``(n_queries, n_chunks)``. Both sides are already L2-normalized,
        so a dot product is the cosine.
        """
        if self.matrix is None or not self.size:
            return np.zeros((len(query_vectors), 0), dtype=np.float32)
        return query_vectors @ self.matrix.T


def _normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    # A zero vector would divide to NaN and then poison every comparison against it.
    norms[norms == 0] = 1.0
    return matrix / norms


def load_snapshot(
    collection, snapshot_id: str, valid_source_ids: set[str] | None = None
) -> CorpusEmbeddingSnapshot:
    """Page the whole collection into a normalized matrix, ordered by chunk id.

    Sorting before building the matrix makes repeated runs produce identical scores in
    identical order, which is what lets two runs be compared at all.

    ``valid_source_ids`` is the ledger's set of fully indexed sources. Chunks outside it
    are dropped before scoring, so a document the database no longer considers current
    cannot surface as a semantic hit (section 10.2). Passing ``None`` scores everything
    and is only appropriate when no ledger exists yet.
    """
    chunk_ids: list[str] = []
    embeddings: list[list[float]] = []
    metadatas: list[dict] = []
    documents: list[str] = []
    orphans = 0

    offset = 0
    try:
        while True:
            page = collection.get(
                limit=PAGE_SIZE,
                offset=offset,
                include=["metadatas", "documents", "embeddings"],
            )
            ids = page.get("ids") or []
            if not ids:
                break
            page_metas = page.get("metadatas") or [{}] * len(ids)
            page_docs = page.get("documents") or [""] * len(ids)
            page_embeddings = page.get("embeddings")
            for position, chunk_id in enumerate(ids):
                if valid_source_ids is not None:
                    source_id = chunk_id.rsplit(CHUNK_ID_SEPARATOR, 1)[0]
                    if source_id not in valid_source_ids:
                        orphans += 1
                        continue
                chunk_ids.append(chunk_id)
                metadatas.append(page_metas[position])
                documents.append(page_docs[position])
                embeddings.append(page_embeddings[position])
            offset += len(ids)
    except Exception as exc:  # noqa: BLE001 - never fall back to top-K search
        raise VectorStoreUnavailable(str(exc)) from exc

    if not chunk_ids:
        return CorpusEmbeddingSnapshot(
            snapshot_id=snapshot_id, excluded_orphans=orphans
        )

    matrix = np.asarray(embeddings, dtype=np.float32)
    order = np.argsort(np.array(chunk_ids, dtype=object), kind="stable")
    return CorpusEmbeddingSnapshot(
        snapshot_id=snapshot_id,
        chunk_ids=[chunk_ids[i] for i in order],
        matrix=_normalize(matrix[order]),
        metadatas=[metadatas[i] for i in order],
        documents=[documents[i] for i in order],
        excluded_orphans=orphans,
    )


class SnapshotCache:
    """One snapshot in process, replaced when the ledger identity changes.

    Readers share a completed snapshot; only the rebuild is serialized. A stale snapshot
    is never handed out, because the cache key *is* the corpus identity.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._snapshot: CorpusEmbeddingSnapshot | None = None

    def get(
        self,
        collection,
        snapshot_id: str,
        valid_source_ids: set[str] | None = None,
    ) -> CorpusEmbeddingSnapshot:
        current = self._snapshot
        if current is not None and current.snapshot_id == snapshot_id:
            return current
        with self._lock:
            if self._snapshot is not None and self._snapshot.snapshot_id == snapshot_id:
                return self._snapshot
            self._snapshot = load_snapshot(collection, snapshot_id, valid_source_ids)
            return self._snapshot

    def clear(self) -> None:
        with self._lock:
            self._snapshot = None


SNAPSHOT_CACHE = SnapshotCache()
