import hashlib
import json
import os
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
import faiss
import numpy as np
from filelock import FileLock, Timeout
from langchain_core.vectorstores import VectorStore
from app.rag.errors import RAGError
from app.rag.schemas import Chunk, Document, Hit

class FaissStore(VectorStore):
    """All readers and writers reload under one cross-process lock.

    Immutable generations are published through an atomic CURRENT pointer.
    JSON contains metadata only; no pickle is used. Local files must be trusted.
    Implements the LangChain VectorStore query interface; documents are added
    through DocumentLoader so uploads stay deduplicated and persisted.
    """
    def __init__(self, directory: Path, embeddings, chunk_config: dict):
        self.directory = Path(directory)
        self._embeddings = embeddings
        self.identity, self.chunk_config = embeddings.identity, chunk_config
        self.index = None
        self.documents, self.chunks = {}, []

    @property
    def embeddings(self):
        return self._embeddings

    @classmethod
    def from_texts(cls, texts, embedding, metadatas=None, **kwargs):
        raise NotImplementedError("Ingest documents through DocumentLoader.ingest")

    def similarity_search_with_score_by_vector(self, embedding, k=4, document_ids=None, score_threshold=None, **kwargs):
        with self.transaction():
            hits = self.search(np.asarray([embedding], dtype=np.float32), k, document_ids, score_threshold)
        return [(hit.to_document(), hit.score) for hit in hits]

    def similarity_search_with_score(self, query, k=4, **kwargs):
        return self.similarity_search_with_score_by_vector(self.embeddings.embed_query(query), k, **kwargs)

    def similarity_search_by_vector(self, embedding, k=4, **kwargs):
        return [document for document, _ in self.similarity_search_with_score_by_vector(embedding, k, **kwargs)]

    def similarity_search(self, query, k=4, **kwargs):
        return [document for document, _ in self.similarity_search_with_score(query, k, **kwargs)]

    def _select_relevance_score_fn(self):
        # Inner product of normalized vectors is cosine similarity in [-1, 1].
        return lambda score: (score + 1) / 2

    @contextmanager
    def transaction(self):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with FileLock(str(self.directory / "store.lock"), timeout=120):
                self._load()
                yield self
        except RAGError:
            raise
        except Timeout as exc:
            raise RAGError("Vector storage is busy; retry later") from exc
        except Exception as exc:
            raise RAGError("Vector storage unavailable; check permissions or rebuild the index") from exc

    def _load(self):
        self.index, self.documents, self.chunks = None, {}, []
        pointer = self.directory / "CURRENT"
        if not pointer.exists():
            return
        generation = pointer.read_text().strip()
        if not generation.startswith("gen_") or len(generation) != 36 or any(c not in "0123456789abcdef" for c in generation[4:]):
            raise RAGError("Invalid index manifest; rebuild the index")
        folder = self.directory / generation
        metadata = json.loads((folder / "metadata.json").read_text())
        if metadata.get("version") != 1 or metadata.get("model") != self.identity or metadata.get("chunk_config") != self.chunk_config:
            raise RAGError("Index configuration is incompatible; restore the original configuration or rebuild")
        index_path = folder / "vectors.faiss"
        if hashlib.sha256(index_path.read_bytes()).hexdigest() != metadata['index_sha256']:
            raise RAGError("Index checksum mismatch; restore a backup or rebuild")
        self.index = faiss.read_index(str(index_path))
        self.documents = {d.document_id: d for d in map(Document.model_validate, metadata['documents'])}
        self.chunks = [Chunk.model_validate(c) for c in metadata['chunks']]
        if self.index.ntotal != len(self.chunks) or self.index.d != metadata['dimension'] or len({c.chunk_id for c in self.chunks}) != len(self.chunks) or any(c.document_id not in self.documents for c in self.chunks):
            raise RAGError("Index metadata mismatch; rebuild the index")

    def _save(self):
        generation = "gen_" + uuid.uuid4().hex
        folder = self.directory / generation
        folder.mkdir()
        index_path = folder / "vectors.faiss"
        faiss.write_index(self.index, str(index_path))
        metadata = dict(version=1, model=self.identity, chunk_config=self.chunk_config, dimension=self.index.d, index_sha256=hashlib.sha256(index_path.read_bytes()).hexdigest(), documents=[d.model_dump() for d in self.documents.values()], chunks=[c.model_dump() for c in self.chunks])
        (folder / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False))
        for name in ("vectors.faiss", "metadata.json"):
            with (folder / name).open("rb") as stream:
                os.fsync(stream.fileno())
        self._sync_directory(folder)
        temporary = self.directory / "CURRENT.tmp"
        with temporary.open("w") as stream:
            stream.write(generation)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.directory / "CURRENT")
        self._sync_directory(self.directory)
        # Readers share the lock, so obsolete snapshots are no longer in use.
        for old in self.directory.glob("gen_*"):
            if old != folder and old.is_dir():
                shutil.rmtree(old, ignore_errors=True)

    @staticmethod
    def _sync_directory(folder):
        if os.name == "posix":
            fd = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    @staticmethod
    def normalize(vectors):
        values = np.asarray(vectors, dtype=np.float32)
        if values.ndim != 2 or values.shape[1] < 1 or not np.isfinite(values).all():
            raise RAGError("Invalid embedding shape or values")
        norms = np.linalg.norm(values, axis=1, keepdims=True)
        if np.any(norms <= 0):
            raise RAGError("Embedding provider returned zero vectors")
        return np.ascontiguousarray(values / norms)

    def insert(self, document, chunks, vectors):
        values = self.normalize(vectors)
        if len(chunks) != len(values) or not chunks:
            raise RAGError("Embedding count does not match chunks")
        if self.index is None or (self.index.ntotal == 0 and not self.documents):
            self.index = faiss.IndexFlatIP(values.shape[1])
        if values.shape[1] != self.index.d:
            raise RAGError("Embedding dimension is incompatible; rebuild the index")
        self.index.add(values)
        self.documents[document.document_id] = document
        self.chunks.extend(chunks)
        self._save()

    def search(self, vector, top_k, document_ids=None, threshold=None):
        if self.index is None or not self.chunks:
            return []
        vector = self.normalize(vector)
        if vector.shape != (1, self.index.d):
            raise RAGError("Query embedding dimension is incompatible with the index")
        scores, ids = self.index.search(vector, len(self.chunks) if document_ids is not None else min(top_k, len(self.chunks)))
        results = []
        for score, position in zip(scores[0], ids[0]):
            if position < 0:
                continue
            chunk = self.chunks[int(position)]
            if document_ids is not None and chunk.document_id not in document_ids:
                continue
            score = float(np.clip(score, -1, 1))
            if threshold is not None and score < threshold:
                continue
            results.append(Hit(**chunk.model_dump(), score=score, rank=len(results)+1))
            if len(results) == top_k:
                break
        return results

    def delete(self, document_id):
        if document_id not in self.documents:
            raise RAGError("Document not found", 404)
        keep = [i for i,c in enumerate(self.chunks) if c.document_id != document_id]
        replacement = faiss.IndexFlatIP(self.index.d)
        if keep:
            replacement.add(np.stack([self.index.reconstruct(i) for i in keep]))
        self.index = replacement
        self.chunks = [self.chunks[i] for i in keep]
        del self.documents[document_id]
        self._save()
